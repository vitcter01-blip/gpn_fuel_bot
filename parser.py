"""
Парсер карты АЗС «Газпромнефть» (gpnbonus.ru/fuel/refuel-map).

Логика устойчива к смене схемы ответа: вместо жёсткой привязки к полям
JSON рекурсивно обходится и из него достаются объекты, похожие на АЗС
(есть координаты/адрес) и вложенные списки, похожие на топливо
(есть цена и/или статус наличия).

Источник данных ищется в таком порядке:
  1. переменная окружения GPN_API_URL
  2. адрес, автоматически найденный ранее в текущем процессе
  3. список эвристических кандидатов CANDIDATE_ENDPOINTS
  4. HTML страницы карты — поиск встроенного состояния (__NUXT__/__INITIAL_STATE__)
  5. автоматическое обнаружение сетевого запроса через Playwright
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from maps import is_yandex_url
from models import Fuel, Station

log = logging.getLogger("gpn.parser")

MAP_URL = "https://gpnbonus.ru/fuel/refuel-map"
_MEMORY_ENDPOINT: str | None = None

# Эвристические кандидаты. Реальный адрес лучше получить через discover.py.
CANDIDATE_ENDPOINTS = [
    "https://gpnbonus.ru/api/v1/azs/",
    "https://gpnbonus.ru/api/azs/",
    "https://gpnbonus.ru/api/v1/stations/",
    "https://gpnbonus.ru/api/stations/",
    "https://gpnbonus.ru/api/v1/map/stations/",
    "https://gpnbonus.ru/fuel/refuel-map/data/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": MAP_URL,
}

# ---------------------------------------------------------------- словари полей

LAT_KEYS = {"lat", "latitude", "широта", "y"}
LON_KEYS = {"lon", "lng", "long", "longitude", "долгота", "x"}
ADDR_KEYS = {"address", "addr", "adres", "адрес", "fulladdress", "location", "street"}
NAME_KEYS = {"name", "title", "caption", "название", "number", "num", "code"}
ID_KEYS = {"id", "stationid", "azsid", "guid", "uuid", "externalid", "number", "code"}
SCHEDULE_KEYS = {"schedule", "worktime", "workinghours", "hours", "режимработы", "graphik"}
NOTICE_KEYS = {"notice", "message", "warning", "info", "announcement", "comment"}
# Идентификатор карточки организации берём ТОЛЬКО из явно яндексовых полей.
# Обобщённые 'oid'/'orgid' — обычно внутренние id самого сайта, и подстановка
# их в /maps/org/{id} ведёт на постороннюю организацию.
ORG_KEYS = {"yandexorgid", "yandexid", "ymapsid", "yandexoid", "yandexmapsid",
            "ymapsorgid", "yandexcompanyid"}
LINK_KEYS = {"url", "link", "maplink", "mapurl", "yandexurl", "weburl", "href"}

FUEL_LIST_KEYS = {"fuel", "fuels", "fueltypes", "fuellist", "products", "goods",
                  "items", "prices", "petrol", "топливо"}
FUEL_CODE_KEYS = {"code", "name", "title", "shortname", "fuelname", "fueltype",
                  "type", "marka", "марка", "label"}
PRICE_KEYS = {"price", "cost", "value", "amount", "цена"}
STATUS_KEYS = {"status", "available", "availability", "instock", "isavailable",
                "state", "наличие", "statustext", "presence"}
TIME_KEYS = {"updated", "updatedat", "time", "datetime", "actualat", "timestamp", "date"}
DELIVERY_KEYS = {"delivery", "shipment", "eta", "intransit", "supply", "доставка"}

# Короткие токены сравниваем целиком, иначе '10:05' поймает подстроку '0',
# а 'Now available' — подстроку 'no'. Фразы ищем вхождением.
TRUE_EXACT = {"true", "1", "yes", "y", "да", "есть", "ok", "active", "instock", "in_stock"}
FALSE_EXACT = {"false", "0", "no", "n", "нет", "empty", "inactive",
               "out_of_stock", "outofstock"}
FALSE_PHRASES = ("отсутству", "нет в наличии", "не в наличии", "недоступ", "не доступ",
                 "закончил", "временно нет", "out of stock", "unavailable", "not available")
TRUE_PHRASES = ("в наличии", "есть в наличии", "доступн", "available", "in stock")

# Краснодарский край (грубая рамка) + текстовые признаки в адресе
KRASNODAR_BBOX = (43.30, 46.85, 36.50, 41.90)  # lat_min, lat_max, lon_min, lon_max
KRASNODAR_RE = re.compile(
    r"краснодар|кубан|сочи|анапа|геленджик|новороссийск|армавир|ейск|туапсе|"
    r"кропоткин|тихорецк|славянск-на-кубани|темрюк|лабинск|апшеронск|усть-лабинск|"
    r"крымск|белореченск|тимашевск|курганинск|каневск|динск|горячий ключ|адлер|"
    r"абинск|приморско-ахтарск|кореновск|гулькевич|павловск",
    re.I,
)
# Соседние регионы, попадающие в ту же рамку. Проверяются только если в адресе
# нет кубанского признака: адрес «Краснодар — … — граница Ставропольского края»
# относится к Краснодарскому краю и отсеян быть не должен.
OTHER_REGION_RE = re.compile(
    r"адыге|майкоп|яблоновск|энем|тахтамукай|ставропол|ростов|батайск|азов|"
    r"крым(?!ск)|севастопол|симферопол|керч|карачаев|черкесск|нальчик|кабардин|"
    r"калмык|элист|абхаз",
    re.I,
)


# ---------------------------------------------------------------- время

MSK = timezone(timedelta(hours=3))          # сайт отдаёт время в МСК
STALE_MINUTES = 60                          # после этого данные считаем устаревшими


def now_msk() -> datetime:
    return datetime.now(MSK)


def parse_site_time(value: Any, ref: Optional[datetime] = None) -> Optional[datetime]:
    """Приводит отметку времени сайта к datetime в МСК.

    Понимает unix-таймстемп, ISO-строку и голое время вида 'на 12:46 МСК'.
    Для голого времени дата берётся текущая; если получилось заметно «в будущем»,
    значит отметка со вчерашнего дня (случай около полуночи).
    """
    if value in (None, "", [], {}):
        return None
    ref = ref or now_msk()

    if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.isdigit() and len(value) >= 10):
        ts = float(value)
        if ts > 1e11:                        # миллисекунды
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, MSK)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(MSK) if dt.tzinfo else dt.replace(tzinfo=MSK)
    except ValueError:
        pass

    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not m:
        return None
    hour, minute, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if hour > 23 or minute > 59:
        return None
    dt = ref.replace(hour=hour, minute=minute, second=sec, microsecond=0)
    if dt - ref > timedelta(minutes=15):     # «12:46» уже наступившего завтра не бывает
        dt -= timedelta(days=1)
    return dt


def parse_duration(text: str) -> Optional[timedelta]:
    """'В пути (еще ~2ч)' -> timedelta(hours=2); '30 мин' -> timedelta(minutes=30)."""
    if not text:
        return None
    low = str(text).lower()
    hours = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ч|час|h\b|hour)", low)
    mins = re.search(r"(\d+)\s*(?:мин|min|м\b)", low)
    total = timedelta()
    if hours:
        total += timedelta(hours=float(hours.group(1).replace(",", ".")))
    if mins:
        total += timedelta(minutes=int(mins.group(1)))
    return total or None


def fmt_delta(seconds: float) -> str:
    """Длительность словами: '3 ч 20 мин', '2 дн 4 ч'."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


def fmt_age(dt: Optional[datetime], ref: Optional[datetime] = None) -> str:
    """'5 мин назад' / '2 ч 10 мин назад'."""
    if dt is None:
        return ""
    seconds = ((ref or now_msk()) - dt).total_seconds()
    if seconds < 60:
        return "только что"
    return f"{fmt_delta(seconds)} назад"


# ---------------------------------------------------------------- утилиты обхода

def _k(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "").replace(" ", "")


def _iter_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _pick(d: dict, keys: set[str]) -> Any:
    for key, val in d.items():
        if _k(key) in keys and val not in (None, "", [], {}):
            return val
    return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def to_bool(value: Any) -> Optional[bool]:
    """Распознаёт 'В наличии' / 'Отсутствует' / True / 1 / 'out_of_stock' и т.п.

    Порядок важен: сначала точное совпадение коротких токенов, затем фразы,
    причём отрицательные раньше положительных ('unavailable' содержит 'available',
    'нет в наличии' содержит 'в наличии').
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, dict):
        value = status_text_of(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in FALSE_EXACT:
        return False
    if text in TRUE_EXACT:
        return True
    if any(p in text for p in FALSE_PHRASES):
        return False
    if any(p in text for p in TRUE_PHRASES):
        return True
    return None


def status_text_of(value: Any) -> str:
    if isinstance(value, bool):
        return "В наличии" if value else "Отсутствует"
    if isinstance(value, dict):
        for key in ("text", "name", "title", "label"):
            if key in value:
                return str(value[key])
    return str(value) if value is not None else ""


# ---------------------------------------------------------------- разбор топлива

def _looks_like_fuel(d: dict) -> bool:
    keys = {_k(k) for k in d}
    has_code = bool(keys & FUEL_CODE_KEYS)
    has_meta = bool(keys & PRICE_KEYS) or bool(keys & STATUS_KEYS)
    return has_code and has_meta


def parse_fuel(d: dict, ref: Optional[datetime] = None) -> Optional[Fuel]:
    raw_code = _pick(d, FUEL_CODE_KEYS)
    if isinstance(raw_code, dict):
        raw_code = _pick(raw_code, {"name", "title", "code", "shortname"})
    if raw_code is None:
        return None

    status_raw = _pick(d, STATUS_KEYS)
    delivery_raw = _pick(d, DELIVERY_KEYS)
    if isinstance(delivery_raw, dict):
        delivery_raw = _pick(delivery_raw, {"text", "eta", "time", "name"}) or ""
    delivery = str(delivery_raw or "").strip()

    updated_raw = _pick(d, TIME_KEYS)
    updated_at = parse_site_time(updated_raw, ref)

    # ориентировочное прибытие бензовоза: время статуса + «еще ~2ч»
    eta = None
    duration = parse_duration(delivery)
    if duration:
        eta = (updated_at or ref or now_msk()) + duration

    return Fuel(
        code=str(raw_code).strip(),
        price=to_float(_pick(d, PRICE_KEYS)),
        available=to_bool(status_raw),
        status_text=status_text_of(status_raw),
        updated_raw=str(updated_raw or ""),
        updated_at=updated_at,
        delivery=delivery,
        eta=eta,
    )


def _extract_fuels(station: dict, ref: Optional[datetime] = None) -> list[Fuel]:
    fuels: list[Fuel] = []
    seen: set[str] = set()

    def consider(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and _looks_like_fuel(item):
                fuel = parse_fuel(item, ref)
                if fuel and fuel.key and fuel.key not in seen:
                    seen.add(fuel.key)
                    fuels.append(fuel)

    for key, val in station.items():
        if _k(key) in FUEL_LIST_KEYS:
            consider(val)
            if isinstance(val, dict):
                for sub in val.values():
                    consider(sub)

    if not fuels:  # запасной вариант — любой вложенный список, похожий на топливо
        for val in station.values():
            consider(val)
    return fuels


# ---------------------------------------------------------------- разбор АЗС

RUSSIA_BBOX = (41.0, 82.0, 19.0, 180.0)  # lat_min, lat_max, lon_min, lon_max


def _plausible(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = RUSSIA_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def _pair_to_latlon(a: Optional[float], b: Optional[float],
                    geojson: bool) -> tuple[Optional[float], Optional[float]]:
    """Пара чисел -> (lat, lon). Яндекс отдаёт [lat, lon], GeoJSON — [lon, lat],
    поэтому порядок выбирается по попаданию точки в границы России."""
    if a is None or b is None:
        return None, None
    direct, swapped = (a, b), (b, a)
    ok_direct = abs(a) <= 90 and _plausible(*direct)
    ok_swapped = abs(b) <= 90 and _plausible(*swapped)
    if ok_direct and not ok_swapped:
        return direct
    if ok_swapped and not ok_direct:
        return swapped
    if abs(a) > 90:          # первое число не может быть широтой
        return swapped
    return swapped if geojson else direct


GEO_CONTAINER_KEYS = {"coords", "coordinates", "point", "position", "geo", "location", "geometry"}


def _coords(d: dict) -> tuple[Optional[float], Optional[float]]:
    lat = to_float(_pick(d, LAT_KEYS))
    lon = to_float(_pick(d, LON_KEYS))
    if lat is None or lon is None:
        geojson = False
        coords = None
        for key, val in d.items():
            if _k(key) in GEO_CONTAINER_KEYS and val not in (None, "", [], {}):
                coords, geojson = val, _k(key) == "coordinates"
                break
        if isinstance(coords, dict):
            inner = coords.get("coordinates")
            if inner is not None:
                coords, geojson = inner, True
            else:
                lat = lat if lat is not None else to_float(_pick(coords, LAT_KEYS))
                lon = lon if lon is not None else to_float(_pick(coords, LON_KEYS))
                coords = None
        if isinstance(coords, str):
            parts = re.findall(r"-?\d+(?:[.,]\d+)?", coords)
            coords = parts[:2] if len(parts) >= 2 else None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lat, lon = _pair_to_latlon(to_float(coords[0]), to_float(coords[1]), geojson)
    if lat is not None and not (-90 <= lat <= 90):
        lat = None
    if lon is not None and not (-180 <= lon <= 180):
        lon = None
    return lat, lon


def flatten_geojson(obj: Any) -> Any:
    """Склеивает GeoJSON-фичи {properties, geometry} в один плоский объект,
    чтобы адрес, топливо и координаты попали в одну запись."""
    if isinstance(obj, list):
        return [flatten_geojson(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    keys = {_k(k) for k in obj}
    if "properties" in keys and "geometry" in keys:
        props = next((v for k, v in obj.items() if _k(k) == "properties"), None) or {}
        geom = next((v for k, v in obj.items() if _k(k) == "geometry"), None) or {}
        merged: dict = dict(props) if isinstance(props, dict) else {}
        if isinstance(geom, dict) and geom.get("coordinates") is not None:
            merged["coordinates"] = geom["coordinates"]
        for key, val in obj.items():
            if _k(key) not in {"properties", "geometry"}:
                merged.setdefault(key, val)
        return flatten_geojson(merged)
    return {k: flatten_geojson(v) for k, v in obj.items()}


def _looks_like_station(d: dict) -> bool:
    keys = {_k(k) for k in d}
    has_geo = bool(keys & LAT_KEYS and keys & LON_KEYS) or bool(keys & GEO_CONTAINER_KEYS)
    has_addr = bool(keys & ADDR_KEYS)
    has_name = bool(keys & NAME_KEYS)
    has_fuel = bool(keys & FUEL_LIST_KEYS)
    # голый объект геометрии ({type, coordinates}) за АЗС не считаем
    return has_fuel or (has_geo and (has_addr or has_name))


def parse_station(d: dict, ref: Optional[datetime] = None) -> Optional[Station]:
    ref = ref or now_msk()
    lat, lon = _coords(d)
    address = _pick(d, ADDR_KEYS)
    if isinstance(address, dict):
        address = _pick(address, {"full", "text", "name", "value"}) or ""
    name = _pick(d, NAME_KEYS)
    fuels = _extract_fuels(d, ref)

    if not fuels and lat is None and not address:
        return None

    # если у марки нет своей отметки времени, берём общую по АЗС
    station_time = parse_site_time(_pick(d, TIME_KEYS), ref)
    if station_time:
        for fuel in fuels:
            if fuel.updated_at is None:
                fuel.updated_at = station_time
                duration = parse_duration(fuel.delivery)
                if duration and fuel.eta is None:
                    fuel.eta = station_time + duration

    sid = _pick(d, ID_KEYS)
    if sid is None:
        sid = f"{lat},{lon}" if lat is not None else str(address)[:40]

    notice = _pick(d, NOTICE_KEYS)
    if isinstance(notice, (dict, list)):
        notice = json.dumps(notice, ensure_ascii=False)[:200]

    # ссылка с сайта берётся только если она действительно яндексовая
    raw_link = _pick(d, LINK_KEYS)
    map_url = str(raw_link).strip() if is_yandex_url(raw_link) else ""
    oid = _pick(d, ORG_KEYS)

    station = Station(
        id=str(sid),
        map_url=map_url,
        yandex_oid=str(oid).strip() if oid is not None else "",
        name=str(name or "").strip(),
        address=str(address or "").strip(),
        lat=lat,
        lon=lon,
        schedule=str(_pick(d, SCHEDULE_KEYS) or "").strip(),
        notice=str(notice or "").strip(),
        fetched_at=ref,
        fuels=fuels,
    )
    station.region = "Краснодарский край" if is_krasnodar(station) else ""
    return station


def parse_payload(payload: Any, ref: Optional[datetime] = None) -> list[Station]:
    """Достаёт список АЗС из произвольного JSON-ответа."""
    ref = ref or now_msk()
    stations: dict[str, Station] = {}
    payload = flatten_geojson(payload)
    for d in _iter_dicts(payload):
        if not _looks_like_station(d):
            continue
        station = parse_station(d, ref)
        if station is None:
            continue
        # при дублях оставляем запись с бо́льшим числом марок топлива
        old = stations.get(station.id)
        if old is None or len(station.fuels) > len(old.fuels):
            stations[station.id] = station
    return list(stations.values())


def is_krasnodar(station: Station) -> bool:
    """Адрес важнее рамки: в bbox попадают Адыгея и запад Ставрополья."""
    text = f"{station.address} {station.name}"
    if KRASNODAR_RE.search(text):
        return True
    if OTHER_REGION_RE.search(text):
        return False
    if station.lat is not None and station.lon is not None:
        lat_min, lat_max, lon_min, lon_max = KRASNODAR_BBOX
        return lat_min <= station.lat <= lat_max and lon_min <= station.lon <= lon_max
    return False


# ---------------------------------------------------------------- HTML-фолбэк

STATE_PATTERNS = [
    re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.S),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S),
    re.compile(r'<script[^>]+type="application/json"[^>]*>(\{.*?\})</script>', re.S),
]


def extract_state_from_html(html: str) -> list[Any]:
    found = []
    for pattern in STATE_PATTERNS:
        for match in pattern.finditer(html):
            try:
                found.append(json.loads(match.group(1)))
            except json.JSONDecodeError:
                continue
    return found


# ---------------------------------------------------------------- клиент

class GpnClient:
    def __init__(self, endpoint: str | None = None, timeout: float = 25.0):
        self.endpoint = endpoint or self._load_endpoint()
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _load_endpoint() -> Optional[str]:
        if os.getenv("GPN_API_URL"):
            return os.environ["GPN_API_URL"]
        return _MEMORY_ENDPOINT

    @staticmethod
    def _remember_endpoint(url: str) -> None:
        global _MEMORY_ENDPOINT
        _MEMORY_ENDPOINT = url

    async def _discover_endpoint(self) -> Optional[str]:
        """Находит API через браузер, не создавая файлов на диске."""
        try:
            from discover import discover

            return await discover(headless=True, dump=False, verbose=False, wait=10)
        except Exception as exc:
            log.warning("Автоматическое обнаружение API не удалось: %s", exc)
            return None

    async def __aenter__(self) -> "GpnClient":
        import httpx  # ленивый импорт: чистый разбор работает без зависимостей

        self._client = httpx.AsyncClient(
            headers=HEADERS, timeout=self.timeout, follow_redirects=True)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    async def _get_json(self, url: str) -> Any:
        assert self._client
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def fetch_stations(self) -> list[Station]:
        """Возвращает список АЗС. Бросает RuntimeError, если источник не найден."""
        assert self._client, "используйте 'async with GpnClient() as c:'"
        errors: list[str] = []

        urls = [self.endpoint] if self.endpoint else []
        urls += [u for u in CANDIDATE_ENDPOINTS if u != self.endpoint]

        for url in urls:
            try:
                payload = await self._get_json(url)
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}")
                continue
            stations = parse_payload(payload)
            if stations:
                if url != self.endpoint:
                    log.info("Рабочий эндпоинт: %s", url)
                    self.endpoint = url
                    self._remember_endpoint(url)
                return stations
            errors.append(f"{url}: JSON получен, но АЗС не распознаны")

        # фолбэк: состояние, встроенное в HTML карты
        try:
            resp = await self._client.get(MAP_URL)
            resp.raise_for_status()
            for state in extract_state_from_html(resp.text):
                stations = parse_payload(state)
                if stations:
                    log.info("Данные получены из HTML карты")
                    return stations
        except Exception as exc:
            errors.append(f"{MAP_URL}: {type(exc).__name__}")

        # Последний fallback: браузер сам наблюдает запросы карты. Найденный URL
        # хранится только в памяти процесса и повторно используется при опросах.
        discovered = await self._discover_endpoint()
        if discovered:
            try:
                payload = await self._get_json(discovered)
                stations = parse_payload(payload)
                if stations:
                    self.endpoint = discovered
                    self._remember_endpoint(discovered)
                    log.info("API автоматически обнаружен: %s", discovered)
                    return stations
                errors.append(f"{discovered}: JSON получен, но АЗС не распознаны")
            except Exception as exc:
                errors.append(f"{discovered}: {type(exc).__name__}")
        else:
            errors.append("Playwright: API не обнаружен")

        raise RuntimeError(
            "Не удалось получить данные АЗС автоматически. При необходимости "
            "задайте GPN_API_URL вручную.\nПопытки:\n  - "
            + "\n  - ".join(errors)
        )

    async def fetch_krasnodar(self) -> list[Station]:
        return [s for s in await self.fetch_stations() if is_krasnodar(s)]


async def get_stations(krasnodar_only: bool = True) -> list[Station]:
    async with GpnClient() as client:
        return await (client.fetch_krasnodar() if krasnodar_only else client.fetch_stations())
