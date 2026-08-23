"""
Ссылки на Яндекс Карты.

ВАЖНО про порядок координат — он в разных параметрах разный, это documented-поведение
Яндекса и главный источник ошибок:

    ll, pt, whatshere[point]   -> долгота, широта   (lon, lat)
    rtext (точки маршрута)     -> широта, долгота   (lat, lon)

Источник: https://yandex.ru/dev/yandex-apps-launch-maps/doc/ru/concepts/yandexmaps-web

Ссылки строятся по цепочке приоритетов:
    1. карточка организации (oid), если сайт её отдал
    2. готовая ссылка с сайта, если она яндексовая
    3. метка по координатам
    4. поиск по адресу — когда координат нет вовсе
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote, urlsplit

BASE = "https://yandex.ru/maps/"
ROUTE_MODES = {"auto", "mt", "pd", "bc"}   # авто, транспорт, пешком, велосипед


def _clamp_zoom(zoom: int) -> int:
    """Яндекс принимает масштаб 1..19."""
    return max(1, min(19, int(zoom)))


def _valid(lat: Optional[float], lon: Optional[float]) -> bool:
    return (lat is not None and lon is not None
            and -90 <= lat <= 90 and -180 <= lon <= 180)


def point_url(lat: Optional[float], lon: Optional[float], zoom: int = 17) -> Optional[str]:
    """Метка на карте. pt — долгота,широта."""
    if not _valid(lat, lon):
        return None
    return f"{BASE}?pt={lon:.6f},{lat:.6f}&z={_clamp_zoom(zoom)}&l=map"


def route_url(lat: Optional[float], lon: Optional[float],
              origin: Optional[tuple[float, float]] = None,
              mode: str = "auto") -> Optional[str]:
    """Маршрут до точки. rtext — широта,долгота (обратный порядок относительно pt!).

    Если известна точка отправления (пользователь прислал геопозицию), строим
    полный маршрут — это документированный формат. Иначе используем форму с
    ведущей тильдой: Яндекс подставляет текущее местоположение. Эта форма в
    документации не описана, поэтому при сомнениях есть point_url как запасной.
    """
    if not _valid(lat, lon):
        return None
    if mode not in ROUTE_MODES:
        mode = "auto"
    dest = f"{lat:.6f},{lon:.6f}"
    if origin and _valid(origin[0], origin[1]):
        rtext = f"{origin[0]:.6f},{origin[1]:.6f}~{dest}"
    else:
        rtext = f"~{dest}"
    return f"{BASE}?rtext={rtext}&rtt={mode}"


def search_url(text: str, lat: Optional[float] = None,
               lon: Optional[float] = None, zoom: int = 15) -> Optional[str]:
    """Поиск по адресу — запасной вариант, когда координат нет."""
    if not text or not str(text).strip():
        return None
    url = f"{BASE}?text={quote(str(text).strip())}"
    if _valid(lat, lon):
        url += f"&ll={lon:.6f},{lat:.6f}&z={_clamp_zoom(zoom)}"
    return url


def org_url(oid: Optional[str]) -> Optional[str]:
    """Карточка организации, если сайт отдал её идентификатор."""
    oid = str(oid or "").strip()
    return f"{BASE}org/{oid}" if oid.isdigit() else None


YANDEX_HOSTS = ("yandex.ru", "yandex.com", "yandex.com.tr", "ya.ru", "maps.yandex.ru")


def is_yandex_url(url: Optional[str]) -> bool:
    """Проверяем именно домен: подстрока 'yandex.' прошла бы и в
    https://evil.example/?ref=yandex.ru, а это чужая ссылка."""
    if not url or not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.split("@")[-1].split(":")[0].lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in YANDEX_HOSTS)


def station_url(station) -> Optional[str]:
    """Лучшая доступная ссылка на АЗС."""
    return (org_url(getattr(station, "yandex_oid", None))
            or (station.map_url if is_yandex_url(getattr(station, "map_url", None)) else None)
            or point_url(station.lat, station.lon)
            or search_url(f"АЗС Газпромнефть {station.address}" if station.address else ""))


def station_route_url(station, origin: Optional[tuple[float, float]] = None) -> Optional[str]:
    return route_url(station.lat, station.lon, origin)
