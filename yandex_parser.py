"""Консервативный парсер публичных карточек АЗС в Яндекс Картах.

Не использует закрытые API и не обходит CAPTCHA. Перечень в поле «Топливо»
считается ассортиментом; наличие выставляется только по явному тексту карточки.

    python yandex_parser.py --search "Газпромнефть хутор Ленина"
    python yandex_parser.py --url https://yandex.ru/maps/org/.../123456789/
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from datetime import datetime
from urllib.parse import quote

from models import Fuel, Station
from parser import now_msk, parse_site_time


ORG_RE = re.compile(r"/maps/org/(?:[^/]+/)?(\d+)(?:/|$)", re.I)
FUEL_RE = re.compile(
    r"(?<![\w])((?:А|A)И[-\s]?(?:80|92|95|98|100)|G[-\s]?(?:95|100)|"
    r"ДТ(?:[-\s]?(?:Л|З|E|ЭКО))?|LPG|СУГ|МЕТАН)(?![\w])", re.I)
STATUS_LINE_RE = re.compile(
    r"^\s*((?:А|A)И[-\s]?(?:80|92|95|98|100)|G[-\s]?(?:95|100)|"
    r"ДТ(?:[-\s]?(?:Л|З|E|ЭКО))?|LPG|СУГ|МЕТАН)\s*"
    r"(?:—|-|:)?\s*(в наличии|есть в наличии|нет в наличии|отсутствует|"
    r"временно нет|недоступно)\s*$", re.I)
ADDRESS_HINT_RE = re.compile(
    r"(край|область|республика|город|г\.|хутор|станица|пос[её]лок|улица|ул\.|"
    r"шоссе|проспект|пр-т|переулок|трасса|километр|район)", re.I)
BLOCK_MARKERS = ("captcha", "введите символы", "доступ ограничен",
                 "access denied", "temporarily limited", "limited")


def _fuel_code(value: str) -> str:
    text = re.sub(r"\s+", "", value.upper()).replace("AI", "АИ")
    text = re.sub(r"^(АИ|G)(?=\d)", r"\1-", text)
    return text


def _oid(url: str) -> str:
    match = ORG_RE.search(url or "")
    return match.group(1) if match else ""


def is_blocked_text(text: str) -> bool:
    low = (text or "").lower().strip()
    return any(marker in low for marker in BLOCK_MARKERS)


def parse_card_text(text: str, url: str, fetched_at: datetime | None = None) -> Station:
    """Разбирает видимый текст одной карточки организации."""
    fetched_at = fetched_at or now_msk()
    lines = [re.sub(r"\s+", " ", line).strip()
             for line in (text or "").splitlines() if line.strip()]
    oid = _oid(url)
    name = lines[0] if lines else "АЗС из Яндекс Карт"
    address = next((line for line in lines[1:25]
                    if ADDRESS_HINT_RE.search(line) and len(line) < 240), "")

    fuels: dict[str, Fuel] = {}
    for line in lines:
        if line.lower().startswith("топливо:"):
            for match in FUEL_RE.finditer(line.partition(":")[2]):
                code = _fuel_code(match.group(1))
                fuels.setdefault(code, Fuel(
                    code=code, available=None, status_text="есть в ассортименте"))

    explicit: list[tuple[str, bool, str]] = []
    for line in lines:
        match = STATUS_LINE_RE.match(line)
        if not match:
            continue
        code, status = _fuel_code(match.group(1)), match.group(2)
        available = status.lower() in {"в наличии", "есть в наличии"}
        explicit.append((code, available, status))

    updated = None
    for line in lines:
        if "обнов" in line.lower():
            updated = parse_site_time(line, fetched_at)
            if updated:
                break
    for code, available, status in explicit:
        fuels[code] = Fuel(code=code, available=available, status_text=status,
                           updated_raw="" if not updated else f"на {updated:%H:%M} МСК",
                           updated_at=updated)

    fallback_id = hashlib.sha256((url or text).encode("utf-8")).hexdigest()[:16]
    return Station(
        id=f"yandex:{oid}" if oid else f"yandex:url:{fallback_id}",
        name=name,
        address=address,
        region="Краснодарский край" if "краснодар" in text.lower() else "",
        map_url=url,
        yandex_oid=oid,
        fetched_at=fetched_at,
        fuels=list(fuels.values()),
    )


class YandexMapsClient:
    """Низкочастотное чтение видимого интерфейса Яндекс Карт через Chromium."""

    def __init__(self, headless: bool = True, timeout_ms: int = 60_000):
        self.headless = headless
        self.timeout_ms = timeout_ms

    async def _open(self):
        from playwright.async_api import async_playwright

        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch(headless=self.headless)
        context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        return runtime, browser, await context.new_page()

    async def fetch_url(self, url: str) -> Station:
        runtime, browser, page = await self._open()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await page.wait_for_timeout(4_000)
            body = await page.locator("body").inner_text()
            if is_blocked_text(body):
                raise RuntimeError("Яндекс Карты запросили CAPTCHA или ограничили доступ")
            return parse_card_text(body, page.url)
        finally:
            await browser.close()
            await runtime.stop()

    async def search(self, query: str, limit: int = 10) -> list[Station]:
        runtime, browser, page = await self._open()
        try:
            url = f"https://yandex.ru/maps/?text={quote(query)}"
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await page.wait_for_timeout(5_000)
            body = await page.locator("body").inner_text()
            if is_blocked_text(body):
                raise RuntimeError("Яндекс Карты запросили CAPTCHA или ограничили доступ")
            hrefs = await page.locator('a[href*="/maps/org/"]').evaluate_all(
                "els => els.map(e => e.href)")
            urls, seen = [], set()
            for href in hrefs:
                oid = _oid(href)
                if oid and oid not in seen:
                    seen.add(oid)
                    urls.append(href.split("?")[0])
                if len(urls) >= limit:
                    break

            stations = []
            for card_url in urls:
                await page.goto(card_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(2_000)
                stations.append(parse_card_text(
                    await page.locator("body").inner_text(), page.url))
            return stations
        finally:
            await browser.close()
            await runtime.stop()


async def _main(args: argparse.Namespace) -> None:
    client = YandexMapsClient(headless=not args.show)
    stations = ([await client.fetch_url(args.url)] if args.url
                else await client.search(args.search, args.limit))
    for station in stations:
        print(f"\n[{station.id}] {station.title()}")
        for fuel in station.fuels:
            print(f"  {fuel.mark} {fuel.code}: {fuel.status_ru}")
        print(f"  {station.maps_link}")
    print(f"\nНайдено карточек: {len(stations)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер АЗС из Яндекс Карт")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="ссылка на карточку организации")
    group.add_argument("--search", help="поисковый запрос")
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 51))
    parser.add_argument("--show", action="store_true", help="показать окно Chromium")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
