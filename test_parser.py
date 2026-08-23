"""Проверка нормализации на синтетических ответах разной формы: python test_parser.py"""
import asyncio
import sys

from parser import GpnClient, is_krasnodar, parse_payload, to_bool


# Windows PowerShell может запускать Python с однобайтной кодировкой консоли.
# В выводе тестов есть символы ₽ и ✅, поэтому явно используем UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Форма A — плоский список, статус строкой (как отображается на сайте)
PAYLOAD_A = {"data": {"stations": [{
    "id": "26001",
    "address": "Краснодар - Кропоткин - граница Ставропольского края (3 км слева),1",
    "lat": 45.01821, "lon": 39.15017,
    "workTime": "круглосуточно",
    "message": "Временно изменен порядок и объем заправки",
    "fuels": [
        {"code": "95", "price": "71.31 ₽", "status": "Отсутствует",
         "updated": "12:46", "delivery": "В пути (еще ~2ч)"},
        {"code": "92", "price": 66.1, "status": "В наличии", "updated": "12:47"},
        {"code": "G-100", "price": 96.99, "status": "Отсутствует"},
        {"code": "ДТл", "price": 74.86, "status": "Отсутствует"},
        {"code": "G-95", "price": 73.31, "status": "Отсутствует"},
    ]}]}}

# Форма B — GeoJSON, наличие булевым полем, координаты [lon, lat]
PAYLOAD_B = {"features": [{
    "properties": {"stationId": "23117", "name": "АЗС №117",
                   "fullAddress": "г. Сочи, ул. Донская, 10a",
                   "products": [{"fuelName": "92", "cost": 65.9, "isAvailable": False},
                                {"fuelName": "ДТ", "cost": 72.4, "isAvailable": True}]},
    "geometry": {"coordinates": [39.7233, 43.5992]}}]}

# Форма C — другой регион, должен отсеяться фильтром
PAYLOAD_C = {"items": [{"id": "77042", "address": "г. Москва, Ленинский пр-т, 90",
                        "latitude": 55.6785, "longitude": 37.5620,
                        "fuel": [{"name": "95", "price": 62.5, "available": True}]}]}


def check(label: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    assert cond, label


def main() -> None:
    print("to_bool:")
    check("'В наличии' -> True", to_bool("В наличии") is True)
    check("'Отсутствует' -> False", to_bool("Отсутствует") is False)
    check("'нет в наличии' -> False", to_bool("нет в наличии") is False)
    check("True -> True", to_bool(True) is True)
    check("'' -> None", to_bool("") is None)

    print("\nформа A (как на скриншоте):")
    a = parse_payload(PAYLOAD_A)
    check("одна АЗС", len(a) == 1)
    st = a[0]
    check("5 марок топлива", len(st.fuels) == 5)
    check("цена 95 = 71.31 из строки '71.31 ₽'", st.get_fuel("95").price == 71.31)
    check("95 отсутствует", st.get_fuel("95").available is False)
    check("92 в наличии", st.get_fuel("92").available is True)
    check("в наличии только 92", st.available_codes == ["92"])
    check("'G-100' находится как 'g100'", st.get_fuel("g100") is not None)
    check("'ДТл' находится как 'дтл'", st.get_fuel("дтл").price == 74.86)
    check("координаты", (round(st.lat, 5), round(st.lon, 5)) == (45.01821, 39.15017))
    check("режим работы", st.schedule == "круглосуточно")
    check("предупреждение считано", "Временно изменен" in st.notice)
    check("доставка считана", "2ч" in st.get_fuel("95").delivery)
    check("регион определён", is_krasnodar(st))

    print("\nформа B (GeoJSON, [lon, lat], bool-статус):")
    b = parse_payload(PAYLOAD_B)
    check("одна АЗС", len(b) == 1)
    check("lat/lon не перепутаны", abs(b[0].lat - 43.5992) < 1e-4)
    check("ДТ в наличии", b[0].get_fuel("ДТ").available is True)
    check("Сочи -> Краснодарский край", is_krasnodar(b[0]))

    print("\nформа C (Москва):")
    c = parse_payload(PAYLOAD_C)
    check("АЗС распознана", len(c) == 1)
    check("отсеивается фильтром региона", not is_krasnodar(c[0]))

    print("\nВсе проверки пройдены ✅")
    print("\nПример вывода:\n")
    print(a[0].pretty())


def test_time_and_status() -> None:
    """Проверки времени статуса и краевых случаев наличия."""
    from datetime import datetime, timedelta
    from parser import (MSK, fmt_age, is_krasnodar, parse_duration,
                        parse_payload, parse_site_time, to_bool)
    from models import Station

    ref = datetime(2026, 8, 20, 12, 49, tzinfo=MSK)

    print("\nстатусы (регресс на подстроки):")
    check("'10:05' не считается отсутствием", to_bool("10:05") is None)
    check("'Обновлено 0' не считается отсутствием", to_bool("Обновлено 0") is None)
    check("'Now available' -> True", to_bool("Now available") is True)
    check("'Not available' -> False", to_bool("Not available") is False)
    check("'unavailable' -> False", to_bool("unavailable") is False)
    check("'0' -> False", to_bool("0") is False)
    check("'нет в наличии' -> False", to_bool("нет в наличии") is False)

    print("\nразбор времени:")
    check("'на 12:46 МСК' -> 12:46", parse_site_time("на 12:46 МСК", ref).hour == 12)
    check("минуты разобраны", parse_site_time("на 12:46 МСК", ref).minute == 46)
    check("часовой пояс МСК", parse_site_time("12:46", ref).utcoffset() == timedelta(hours=3))
    check("ISO-строка", parse_site_time("2026-08-20T09:46:00Z", ref).astimezone(MSK).hour == 12)
    check("unix-таймстемп", parse_site_time(1755683160, ref) is not None)
    check("миллисекунды", parse_site_time(1755683160000, ref).year == 2025)
    check("мусор -> None", parse_site_time("скоро", ref) is None)
    # переход через полночь: в 00:10 отметка 23:55 — это вчера, а не завтра
    midnight = datetime(2026, 8, 21, 0, 10, tzinfo=MSK)
    check("23:55 в 00:10 -> вчера", parse_site_time("23:55", midnight).day == 20)

    print("\nвозраст данных:")
    check("3 мин назад", fmt_age(ref - timedelta(minutes=3), ref) == "3 мин назад")
    check("2 ч 10 мин назад", fmt_age(ref - timedelta(minutes=130), ref) == "2 ч 10 мин назад")
    check("только что", fmt_age(ref - timedelta(seconds=5), ref) == "только что")

    print("\nдоставка:")
    check("'~2ч' -> 2 часа", parse_duration("В пути (еще ~2ч)") == timedelta(hours=2))
    check("'30 мин' -> 30 минут", parse_duration("еще 30 мин") == timedelta(minutes=30))
    check("без срока -> None", parse_duration("В пути") is None)

    print("\nвремя внутри АЗС:")
    st = parse_payload(PAYLOAD_A, ref)[0]
    check("у 95 время 12:46", st.get_fuel("95").updated_at.strftime("%H:%M") == "12:46")
    check("у 92 своё время 12:47", st.get_fuel("92").updated_at.strftime("%H:%M") == "12:47")
    check("as_of = самое свежее", st.as_of.strftime("%H:%M") == "12:47")
    check("ETA бензовоза 12:46 + 2ч = 14:46",
          st.get_fuel("95").eta.strftime("%H:%M") == "14:46")
    check("сводка про наличие", st.summary().startswith("✅ есть: 92"))

    print("\nрегион (bbox не должен захватывать соседей):")
    check("Майкоп (Адыгея) отсеян",
          not is_krasnodar(Station(id="1", address="г. Майкоп, ул. Ленина", lat=44.61, lon=40.10)))
    check("Ростов отсеян",
          not is_krasnodar(Station(id="2", address="г. Ростов-на-Дону", lat=47.2, lon=39.7)))
    check("«граница Ставропольского края» НЕ отсеяна (адрес кубанский)",
          is_krasnodar(st))
    check("Крымск (Кубань) не спутан с Крымом",
          is_krasnodar(Station(id="3", address="г. Крымск, ул. Ленина", lat=44.93, lon=37.99)))

    print("\nвывод для пользователя:")
    print(st.pretty(ref))


def test_maps_links() -> None:
    """Ссылки на Яндекс Карты: порядок координат в pt и rtext разный."""
    from maps import (is_yandex_url, org_url, point_url, route_url,
                      search_url, station_url)
    from models import Station
    from parser import parse_payload


    # Кропоткин со скриншота
    LAT, LON = 45.01821, 39.15017
    print("порядок координат (главная ловушка):")
    pt = point_url(LAT, LON)
    check(f"pt = долгота,широта -> {pt}", "pt=39.150170,45.018210" in pt)
    rt = route_url(LAT, LON)
    check(f"rtext = широта,долгота -> {rt}", "rtext=~45.018210,39.150170" in rt)
    check("порядок в pt и rtext действительно разный",
          pt.split("pt=")[1].split("&")[0] != rt.split("rtext=~")[1].split("&")[0])

    print("\nмаршрут:")
    r2 = route_url(LAT, LON, origin=(45.03, 38.97))
    check("от известной точки: обе пары широта,долгота",
          "rtext=45.030000,38.970000~45.018210,39.150170" in r2)
    check("тип маршрута auto по умолчанию", r2.endswith("rtt=auto"))
    check("неизвестный режим -> auto", route_url(LAT, LON, mode="ракета").endswith("rtt=auto"))
    check("пешком", route_url(LAT, LON, mode="pd").endswith("rtt=pd"))

    print("\nграничные случаи:")
    check("нет координат -> None", point_url(None, None) is None)
    check("широта вне диапазона -> None", point_url(200, 39) is None)
    check("масштаб зажат в 1..19", "z=19" in point_url(LAT, LON, zoom=99))
    check("масштаб снизу", "z=1" in point_url(LAT, LON, zoom=-5))

    print("\nпоиск и организация:")
    su = search_url("Краснодар - Кропоткин, 1")
    check(f"кириллица закодирована -> {su[:60]}...", "%D0%9A" in su and " " not in su)
    check("пустой текст -> None", search_url("  ") is None)
    check("oid -> карточка", org_url("1184371713") == "https://yandex.ru/maps/org/1184371713")
    check("нечисловой oid -> None", org_url("abc") is None)

    print("\nпроверка чужих ссылок:")
    check("яндексовая", is_yandex_url("https://yandex.ru/maps/org/123"))
    check("не яндексовая отбрасывается", not is_yandex_url("https://evil.example/maps"))
    check("не URL", not is_yandex_url("yandex"))

    print("\nцепочка приоритетов:")
    st = Station(id="1", address="Краснодар", lat=LAT, lon=LON)
    check("по координатам", station_url(st).startswith("https://yandex.ru/maps/?pt="))
    st.map_url = "https://yandex.ru/maps/-/CDabc"
    check("готовая ссылка с сайта важнее координат", station_url(st) == st.map_url)
    st.yandex_oid = "1184371713"
    check("карточка организации важнее всего", station_url(st).endswith("/org/1184371713"))
    no_geo = Station(id="2", address="ст. Динская, ул. Ленина 1")
    check("без координат -> поиск по адресу", "text=" in station_url(no_geo))
    check("совсем пусто -> None", station_url(Station(id="3")) is None)

    print("\nподтягивание ссылки из ответа сайта:")
    payload = {"stations":[{"id":"26001","address":"Краснодар, 1","lat":LAT,"lon":LON,
        "yandexOrgId":"1184371713","url":"https://yandex.ru/maps/-/CDx",
        "fuels":[{"code":"92","price":66.1,"status":"В наличии"}]}]}
    s2 = parse_payload(payload)[0]
    check(f"oid подтянут: {s2.yandex_oid}", s2.yandex_oid == "1184371713")
    check("ссылка подтянута", s2.map_url == "https://yandex.ru/maps/-/CDx")
    check("итоговая ссылка = карточка организации", s2.maps_link.endswith("/org/1184371713"))

    payload["stations"][0]["url"] = "https://spam.example/track?u=1"
    del payload["stations"][0]["yandexOrgId"]
    s3 = parse_payload(payload)[0]
    check("посторонняя ссылка отброшена, строим по координатам",
          s3.map_url == "" and s3.maps_link.startswith("https://yandex.ru/maps/?pt="))


def test_regressions() -> None:
    """Баги, найденные аудитом: закреплены, чтобы не вернулись."""
    from parser import parse_payload
    from maps import org_url

    print("\nрегрессии:")
    # Внутренний числовой id сайта не должен становиться ссылкой на организацию Яндекса
    payload = {"stations": [{"id": "26001", "oid": 778899, "orgId": 12345,
                             "address": "Краснодар, 1", "lat": 45.018, "lon": 39.150,
                             "fuels": [{"code": "92", "price": 66.1, "status": "В наличии"}]}]}
    st = parse_payload(payload)[0]
    check("внутренний oid НЕ принят за id Яндекс-организации", st.yandex_oid == "")
    check("ссылка строится по координатам", "/maps/?pt=" in st.maps_link)

    # А явно яндексовое поле — принимается
    payload["stations"][0]["yandexOrgId"] = "1184371713"
    st2 = parse_payload(payload)[0]
    check("явный yandexOrgId принят", st2.maps_link == org_url("1184371713"))

    # Порядок координат в pt и rtext обязан оставаться разным
    pt = st.maps_link.split("pt=")[1].split("&")[0]
    rt = st.route_link().split("rtext=~")[1].split("&")[0]
    check("pt = долгота,широта", pt.startswith("39."))
    check("rtext = широта,долгота", rt.startswith("45."))


def test_automatic_endpoint_discovery() -> None:
    """Клиент сам находит API, если URL не задан и кандидаты устарели."""
    discovered_url = "https://gpnbonus.ru/api/discovered"

    class AutoDiscoverClient(GpnClient):
        def __init__(self):
            super().__init__()
            self.endpoint = None
            self.discovery_calls = 0
            self._client = object()

        async def _get_json(self, url):
            if url == discovered_url:
                return PAYLOAD_A
            raise RuntimeError("устаревший URL")

        async def _discover_endpoint(self):
            self.discovery_calls += 1
            return discovered_url

    async def run():
        client = AutoDiscoverClient()
        stations = await client.fetch_stations()
        check("API обнаружен автоматически", client.endpoint == discovered_url)
        check("обнаружение выполнено один раз", client.discovery_calls == 1)
        check("ответ найденного API разобран", len(stations) == 1)

    print("\nавтоматическое обнаружение API:")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    test_time_and_status()
    test_maps_links()
    test_regressions()
    test_automatic_endpoint_discovery()
    print("\nВсе проверки пройдены ✅")
