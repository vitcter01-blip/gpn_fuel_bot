"""
Определение реального API-адреса карты АЗС.

Открывает https://gpnbonus.ru/fuel/refuel-map в браузере, слушает сетевые ответы
и находит тот, из которого распознаются АЗС. Найденный адрес сохраняется
в endpoint.json — дальше parser.py берёт его оттуда.

Установка:
    pip install playwright && playwright install chromium

Запуск:
    python discover.py              обычный режим
    python discover.py --show       видимое окно браузера, можно двигать карту руками
    python discover.py --verbose    показать ВСЕ сетевые ответы (диагностика)
    python discover.py --dump       сохранить подходящие ответы в sample_*.json
    python discover.py --wait 60    ждать дольше (секунд)
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from parser import ENDPOINT_CACHE, MAP_URL, parse_payload

# признаки того, что страница нас не пустила
BLOCK_MARKERS = ("доступ ограничен", "access denied", "403 forbidden", "captcha",
                 "проверка браузера", "cloudflare", "attention required",
                 "запрос заблокирован", "недоступен в вашем регионе")


async def discover(headless: bool = True, dump: bool = False,
                   verbose: bool = False, wait: int = 25) -> str | None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Нужен playwright: pip install playwright && playwright install chromium")
        return None

    hits: list[tuple[str, int, object]] = []
    seen: list[tuple[str, int, str, int]] = []   # url, статус, тип, размер

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 414, "height": 896},
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148"),
        )
        page = await context.new_page()

        async def on_response(response) -> None:
            ctype = response.headers.get("content-type", "")
            try:
                body = await response.body()
            except Exception:
                return
            seen.append((response.url, response.status, ctype.split(";")[0], len(body)))

            # НЕ отсеиваем по content-type: некоторые сервисы отдают JSON
            # как text/plain или application/octet-stream
            head = body[:64].lstrip()[:1]
            if head not in (b"{", b"["):
                return
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                return
            stations = parse_payload(payload)
            if stations:
                hits.append((response.url, len(stations), payload))
                print(f"  ✓ {len(stations):>5} АЗС  ←  {response.url}")
                if dump:
                    name = f"sample_{len(hits)}.json"
                    Path(name).write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2)[:2_000_000], "utf-8")
                    print(f"      сохранено в {name}")
            elif verbose and len(body) > 200:
                print(f"    JSON без АЗС ({len(body)} б): {response.url[:90]}")

        pending: set[asyncio.Task] = set()

        def schedule(response) -> None:
            task = asyncio.create_task(on_response(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        page.on("response", schedule)
        page.on("websocket", lambda ws: print(f"    WebSocket: {ws.url[:90]}"))

        print(f"Открываю {MAP_URL} …")
        try:
            await page.goto(MAP_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"\n✗ Страница не открылась: {type(exc).__name__}")
            print("  Похоже на блокировку доступа. Проверьте, открывается ли сайт")
            print("  в обычном браузере; при необходимости используйте VPN.")
            await browser.close()
            return None

        # даём карте прогрузиться и слегка «шевелим» её: часть сайтов
        # запрашивает точки только после взаимодействия
        await page.wait_for_timeout(5_000)
        try:
            await page.mouse.move(200, 400)
            await page.mouse.wheel(0, 600)
            await page.wait_for_timeout(2_000)
            await page.mouse.wheel(0, -300)
        except Exception:
            pass
        await page.wait_for_timeout(wait * 1000)

        # диагностика страницы
        title = await page.title()
        text = (await page.inner_text("body"))[:4000].lower()
        blocked = [m for m in BLOCK_MARKERS if m in text]
        await browser.close()

    print(f"\nЗаголовок страницы: {title!r}")
    print(f"Сетевых ответов перехвачено: {len(seen)}")

    if blocked:
        print(f"\n✗ Похоже, доступ ограничен: на странице встретилось {blocked[0]!r}")
        print("  Сайт российский — из-за рубежа он может не открываться. Попробуйте VPN")
        print("  с российским IP или запустите discover.py на сервере в России.")
        return None

    if not hits:
        if verbose:
            print("\nВсе ответы:")
            for url, status, ctype, size in seen:
                print(f"  {status} {ctype:28} {size:>8} б  {url[:80]}")
        else:
            print("\nНичего похожего на список АЗС не пришло.")
            print("Запустите с диагностикой: python discover.py --show --verbose")
        return None

    best_url, count, _ = max(hits, key=lambda h: h[1])
    ENDPOINT_CACHE.write_text(json.dumps({"url": best_url}, ensure_ascii=False, indent=2), "utf-8")
    print(f"\nСохранено в {ENDPOINT_CACHE.name}: {best_url}  ({count} АЗС)")
    return best_url


def main() -> None:
    ap = argparse.ArgumentParser(description="Поиск API-адреса карты АЗС")
    ap.add_argument("--show", action="store_true", help="показать окно браузера")
    ap.add_argument("--verbose", action="store_true", help="показать все сетевые ответы")
    ap.add_argument("--dump", action="store_true", help="сохранить ответы в sample_*.json")
    ap.add_argument("--wait", type=int, default=25, help="сколько секунд ждать")
    args = ap.parse_args()
    asyncio.run(discover(headless=not args.show, dump=args.dump,
                         verbose=args.verbose, wait=args.wait))


if __name__ == "__main__":
    main()
