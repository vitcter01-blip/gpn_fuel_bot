"""
Telegram-бот: статус топлива на АЗС «Газпромнефть» в Краснодарском крае.

Управление кнопочное: внизу постоянное меню, всё остальное — inline-кнопки.
Печатать нужно только адрес при поиске, да и то есть кнопки городов и геопозиция.
Команды сохранены как псевдонимы, но пользоваться ими не обязательно.

Запуск:
    export BOT_TOKEN="123456:AA..."
    python bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import config
from models import Station
from parser import MSK, GpnClient, fmt_age, fmt_delta
from storage import ANY_STATION, Change, Storage

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gpn.bot")

BOT_CFG, APP_CFG = config.load()
POLL_INTERVAL = APP_CFG.poll_interval
MAX_SUBS = BOT_CFG.max_subs_per_user
ALERT_ON_GONE = BOT_CFG.alert_on_gone
PAGE_SIZE = APP_CFG.page_size

storage = Storage(APP_CFG.db_path)
cache: dict[str, Station] = {}
dp = Dispatcher()

# кнопки нижнего меню
BTN_FIND = "🔍 Найти АЗС"
BTN_NEAR = "📍 Рядом со мной"
BTN_MY = "⛽️ Моя АЗС"
BTN_FUEL = "🔎 Где есть топливо"
BTN_SUBS = "🔔 Подписки"
BTN_HELP = "❓ Помощь"

CITIES = ["Краснодар", "Сочи", "Новороссийск", "Анапа",
          "Армавир", "Кропоткин", "Ейск", "Геленджик"]

HELP = (
    "⛽️ <b>Статус топлива на АЗС «Газпромнефть»</b>\n"
    "Краснодарский край\n\n"
    "Всё управление — кнопками внизу экрана:\n\n"
    f"<b>{BTN_FIND}</b> — выбрать город или ввести адрес\n"
    f"<b>{BTN_NEAR}</b> — ближайшие АЗС по геопозиции (один тап)\n"
    f"<b>{BTN_MY}</b> — АЗС, которую вы выбрали последней\n"
    f"<b>{BTN_FUEL}</b> — где по краю есть нужная марка\n"
    f"<b>{BTN_SUBS}</b> — за чем слежу\n\n"
    "В карточке АЗС нажмите на марку, чтобы включить слежение: "
    "🔔 — слежу, 🔕 — нет. Как только топливо появится, придёт сообщение.\n\n"
    f"Данные обновляются каждые {POLL_INTERVAL // 60} мин."
)


def esc(text) -> str:
    return html.escape(str(text or ""))


# ---------------------------------------------------------------- клавиатуры

def main_kb() -> ReplyKeyboardMarkup:
    """Постоянное меню внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FIND),
             KeyboardButton(text=BTN_NEAR, request_location=True)],
            [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_FUEL)],
            [KeyboardButton(text=BTN_SUBS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Или просто напишите адрес",
    )


def home_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🏠 Меню", callback_data="m:main")]


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BTN_FIND, callback_data="m:find"),
         InlineKeyboardButton(text=BTN_FUEL, callback_data="m:fuel")],
        [InlineKeyboardButton(text=BTN_MY, callback_data="m:my"),
         InlineKeyboardButton(text=BTN_SUBS, callback_data="m:subs")],
    ])


def cities_kb() -> InlineKeyboardMarkup:
    """Города кнопками — чтобы не набирать текст."""
    rows, row = [], []
    for idx, city in enumerate(CITIES):
        row.append(InlineKeyboardButton(text=city, callback_data=f"c:{idx}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fuel_choice_kb() -> InlineKeyboardMarkup:
    """Марки топлива кнопками."""
    rows, row = [], []
    for key, display in storage.fuel_codes():
        row.append(InlineKeyboardButton(text=display, callback_data=f"a:{key}:0"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nav_row(prefix: str, page: int, has_more: bool) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}{page - 1}"))
    if has_more:
        row.append(InlineKeyboardButton(text="▶️ Ещё", callback_data=f"{prefix}{page + 1}"))
    return row


def stations_kb(rows, page: int, nav_prefix: Optional[str] = None) -> InlineKeyboardMarkup:
    """Список АЗС кнопками, с листанием."""
    # номер страницы зажимаем: устаревшая кнопка не должна открыть пустой экран
    max_page = max(0, (len(rows) - 1) // PAGE_SIZE)
    page = min(max(page, 0), max_page)
    start = page * PAGE_SIZE
    chunk = rows[start:start + PAGE_SIZE]
    buttons = []
    for r in chunk:
        station = cache.get(r["id"])
        mark = "✅" if station and station.available_codes else "❌"
        title = (r["address"] or r["name"] or r["id"])[:55]
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {title}", callback_data=f"s:{storage.token_for(r['id'])}")])
    if nav_prefix:
        nav = _nav_row(nav_prefix, page, start + PAGE_SIZE < len(rows))
        if nav:
            buttons.append(nav)
    buttons.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def fuels_kb(chat_id: int, station: Station) -> InlineKeyboardMarkup:
    """Карточка АЗС: кнопка на каждую марку — наличие слева, слежение справа."""
    tracked = storage.tracked_codes(chat_id, station.id)
    token = storage.token_for(station.id)
    buttons, row = [], []
    for fuel in sorted(station.fuels, key=lambda f: (f.available is not True, f.key)):
        bell = "🔔" if fuel.key in tracked else "🔕"
        row.append(InlineKeyboardButton(
            text=f"{fuel.mark} {fuel.code} {bell}", callback_data=f"t:{token}:{fuel.key}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"s:{token}"),
        InlineKeyboardButton(text="📜 История", callback_data=f"h:{token}"),
    ])
    link_row = []
    if station.maps_link:
        link_row.append(InlineKeyboardButton(text="🗺 На карте", url=station.maps_link))
    route = station.route_link(storage.user_origin(chat_id))
    if route:
        link_row.append(InlineKeyboardButton(text="🧭 Маршрут", url=route))
    if link_row:
        buttons.append(link_row)
    buttons.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subs_kb(chat_id: int) -> InlineKeyboardMarkup:
    """Подписки: нажатие снимает слежение."""
    buttons = []
    for r in storage.list_subs(chat_id):
        where = "весь край" if r["station_id"] == ANY_STATION else (
            (r["address"] or r["station_id"])[:40])
        buttons.append([InlineKeyboardButton(
            text=f"🔕 {r['display_code'] or r['fuel_code']} — {where}",
            callback_data=f"u:{storage.token_for(r['station_id'])}:{r['fuel_code']}")])
    buttons.append(home_row())
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------- экраны

def station_view(chat_id: int, station_id: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    station = cache.get(station_id)
    if station:
        return station.pretty(), fuels_kb(chat_id, station)

    row = storage.station_row(station_id)
    if not row:
        return "АЗС не найдена. Нажмите «🔍 Найти АЗС».", menu_kb()
    lines = []
    for r in storage.snapshot(station_id):
        mark = "✅" if r["available"] else ("❌" if r["available"] is not None else "❔")
        price = f" — {r['price']:.2f} ₽" if r["price"] is not None else ""
        code = r["display_code"] or r["fuel_code"]
        when = ""
        if r["site_time"]:
            dt = datetime.fromtimestamp(r["site_time"], MSK)
            when = f" · на {dt:%H:%M} МСК ({fmt_age(dt)})"
        lines.append(f"{mark} <b>{esc(code)}</b>{price}{when}")
    body = "\n".join(lines) or "нет данных"
    return (f"⛽️ <b>{esc(row['address'])}</b>\n(из базы, свежие данные недоступны)"
            f"\n\n{body}"), menu_kb()


def history_view(station_id: str, fuel_code: Optional[str] = None) -> str:
    station = cache.get(station_id)
    row = storage.station_row(station_id)
    title = station.title() if station else (row["address"] if row else station_id)

    codes = [fuel_code] if fuel_code else (
        [f.code for f in station.fuels] if station else
        [r["display_code"] or r["fuel_code"] for r in storage.snapshot(station_id)])

    blocks = []
    for code in codes:
        entries = storage.history(station_id, code, limit=6)
        if not entries:
            continue
        held = fmt_delta(time.time() - entries[0]["changed_at"])
        state = "есть" if entries[0]["available"] else "нет"
        lines = [f"   {'✅ появился' if e['available'] else '❌ пропал'} · "
                 f"{datetime.fromtimestamp(e['changed_at'], MSK):%d.%m %H:%M}"
                 for e in entries]
        blocks.append(f"<b>{esc(code)}</b> — сейчас {state}, держится {held}\n"
                      + "\n".join(lines))

    if not blocks:
        return (f"По <b>{esc(title)}</b> история пока не накоплена — "
                "нужен хотя бы один цикл опроса.")
    return f"📜 <b>{esc(title)}</b>\n\n" + "\n\n".join(blocks)


def available_view(fuel_key: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = storage.stations_with_fuel(fuel_key)
    display = next((d for k, d in storage.fuel_codes() if k == fuel_key), fuel_key)
    if not rows:
        return (f"Сейчас <b>{esc(display)}</b> нет ни на одной АЗС края.\n"
                "Откройте любую АЗС и нажмите на марку — сообщу, когда появится.",
                fuel_choice_kb())
    text = f"✅ <b>{esc(display)}</b> есть на {len(rows)} АЗС:"
    return text, stations_kb(rows, page, nav_prefix=f"a:{fuel_key}:")


async def show_search(message: Message, query: str) -> None:
    rows = storage.search_stations(query, limit=60)
    if not rows:
        return await message.answer(
            f"По запросу «{esc(query)}» ничего не нашлось.\nПопробуйте выбрать город:",
            reply_markup=cities_kb())
    storage.set_last_query(message.chat.id, query)
    await message.answer(f"Найдено {len(rows)}. Выберите АЗС:",
                         reply_markup=stations_kb(rows, 0, nav_prefix="q:"))


# ---------------------------------------------------------------- фоновый опрос

async def refresh() -> list[Change]:
    async with GpnClient() as client:
        stations = await client.fetch_krasnodar()
    if not stations:
        log.warning("Получено 0 АЗС по Краснодарскому краю")
        return []
    storage.upsert_stations(stations)
    changes = storage.diff_and_update(stations)
    cache.clear()
    cache.update({s.id: s for s in stations})
    log.info("Обновлено АЗС: %d, изменений: %d", len(stations), len(changes))
    return changes


async def notify(bot: Bot, changes: list[Change]) -> None:
    for ch in changes:
        if ch.appeared:
            icon, verb = "✅", "появился"
        elif ch.gone and ALERT_ON_GONE:
            icon, verb = "❌", "закончился"
        else:
            continue

        prev = storage.history(ch.station_id, ch.fuel_code, limit=2)
        held = ""
        if len(prev) >= 2:
            span = fmt_delta(prev[0]["changed_at"] - prev[1]["changed_at"])
            held = (f"\n⏳ до этого не было {span}" if ch.appeared
                    else f"\n⏳ был в наличии {span}")

        station = cache.get(ch.station_id)
        address = station.title() if station else ch.station_id
        price = f"\n💰 {ch.price:.2f} ₽" if ch.price else ""
        when = (f"\n🕒 по данным на {ch.site_time:%H:%M} МСК "
                f"({fmt_age(ch.site_time)})") if ch.site_time else ""
        text = (f"{icon} <b>{esc(ch.fuel_code)}</b> {verb}\n"
                f"⛽️ {esc(address)}{price}{when}{held}")
        link = station.maps_link if station else None
        if link:
            text += f"\n📍 <a href=\"{link}\">Открыть в Яндекс Картах</a>"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⛽️ Открыть АЗС",
                                 callback_data=f"s:{storage.token_for(ch.station_id)}")]])
        for chat_id in storage.subscribers_for(ch.station_id, ch.fuel_code):
            for attempt in range(BOT_CFG.send_retries + 1):
                try:
                    await bot.send_message(chat_id, text, reply_markup=keyboard,
                                           disable_web_page_preview=True)
                    await asyncio.sleep(BOT_CFG.send_delay)
                    break
                except TelegramRetryAfter as exc:
                    # Telegram просит подождать; слишком долгую паузу не держим
                    wait = min(exc.retry_after, BOT_CFG.max_retry_after)
                    log.info("Лимит Telegram, жду %s с", wait)
                    await asyncio.sleep(wait)
                except TelegramForbiddenError:
                    storage.clear_subs(chat_id)   # пользователь заблокировал бота
                    break
                except Exception as exc:
                    log.warning("Не отправлено %s (попытка %d): %s",
                                chat_id, attempt + 1, exc)
                    await asyncio.sleep(1)


async def poller(bot: Bot) -> None:
    while True:
        try:
            changes = await refresh()
            if changes:
                await notify(bot, changes)
            storage.prune_history()
        except Exception as exc:
            log.error("Ошибка опроса: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------- кнопки меню

@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_kb(), disable_web_page_preview=True)
    await message.answer("С чего начнём?", reply_markup=menu_kb())


@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_kb(), disable_web_page_preview=True)


@dp.message(F.text == BTN_FIND)
async def on_find_button(message: Message) -> None:
    await message.answer("Выберите город или напишите адрес сообщением:",
                         reply_markup=cities_kb())


@dp.message(F.text == BTN_FUEL)
async def on_fuel_button(message: Message) -> None:
    if not storage.fuel_codes():
        return await message.answer("Данные ещё загружаются, попробуйте через минуту.")
    await message.answer("Какая марка нужна?", reply_markup=fuel_choice_kb())


@dp.message(F.text == BTN_MY)
async def on_my_button(message: Message) -> None:
    station_id = storage.current_station(message.chat.id)
    if not station_id:
        return await message.answer(
            "Вы ещё не выбирали АЗС. Нажмите «🔍 Найти АЗС» или «📍 Рядом со мной».",
            reply_markup=menu_kb())
    text, keyboard = station_view(message.chat.id, station_id)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@dp.message(F.text == BTN_SUBS)
async def on_subs_button(message: Message) -> None:
    if not storage.list_subs(message.chat.id):
        return await message.answer(
            "Пока ни за чем не слежу.\nОткройте АЗС и нажмите на нужную марку.",
            reply_markup=menu_kb())
    await message.answer("За чем слежу (нажмите, чтобы отключить):",
                         reply_markup=subs_kb(message.chat.id))


@dp.message(F.location)
async def on_location(message: Message) -> None:
    storage.set_user_origin(message.chat.id,
                            message.location.latitude, message.location.longitude)
    rows = storage.nearest(message.location.latitude, message.location.longitude, limit=20)
    if not rows:
        return await message.answer("Данные ещё не загружены, попробуйте через минуту.")
    await message.answer("Ближайшие АЗС:", reply_markup=stations_kb(rows, 0, "n:"))


@dp.message(F.text & ~F.text.startswith("/"))
async def on_free_text(message: Message) -> None:
    """Любой текст считаем адресом для поиска — печатать команду не нужно."""
    await show_search(message, message.text.strip())


# ---------------------------------------------------------------- inline-кнопки

async def _edit(call: CallbackQuery, text: str,
                keyboard: Optional[InlineKeyboardMarkup]) -> None:
    try:
        await call.message.edit_text(text, reply_markup=keyboard,
                                     disable_web_page_preview=True)
    except TelegramBadRequest:
        # Telegram считает ошибкой правку без изменений — это не наша проблема
        pass


@dp.callback_query(F.data == "m:main")
async def cb_main(call: CallbackQuery) -> None:
    await _edit(call, "Главное меню:", menu_kb())
    await call.answer()


@dp.callback_query(F.data == "m:find")
async def cb_find(call: CallbackQuery) -> None:
    await _edit(call, "Выберите город или напишите адрес сообщением:", cities_kb())
    await call.answer()


@dp.callback_query(F.data == "m:fuel")
async def cb_fuel(call: CallbackQuery) -> None:
    await _edit(call, "Какая марка нужна?", fuel_choice_kb())
    await call.answer()


@dp.callback_query(F.data == "m:subs")
async def cb_subs(call: CallbackQuery) -> None:
    if not storage.list_subs(call.message.chat.id):
        await _edit(call, "Пока ни за чем не слежу.\n"
                          "Откройте АЗС и нажмите на нужную марку.", menu_kb())
    else:
        await _edit(call, "За чем слежу (нажмите, чтобы отключить):",
                    subs_kb(call.message.chat.id))
    await call.answer()


@dp.callback_query(F.data == "m:my")
async def cb_my(call: CallbackQuery) -> None:
    station_id = storage.current_station(call.message.chat.id)
    if not station_id:
        await _edit(call, "Вы ещё не выбирали АЗС.", menu_kb())
        return await call.answer()
    text, keyboard = station_view(call.message.chat.id, station_id)
    await _edit(call, text, keyboard)
    await call.answer()


@dp.callback_query(F.data.startswith("c:"))
async def cb_city(call: CallbackQuery) -> None:
    """Город выбран кнопкой."""
    idx = call.data.split(":", 1)[1]
    if not idx.isdigit() or int(idx) >= len(CITIES):
        return await call.answer("Кнопка устарела", show_alert=True)
    city = CITIES[int(idx)]
    rows = storage.search_stations(city, limit=60)
    if not rows:
        await _edit(call, f"В городе {esc(city)} АЗС не найдено.", cities_kb())
        return await call.answer()
    storage.set_last_query(call.message.chat.id, city)
    await _edit(call, f"{esc(city)} — найдено {len(rows)}:",
                stations_kb(rows, 0, nav_prefix="q:"))
    await call.answer()


@dp.callback_query(F.data.startswith("q:"))
async def cb_query_page(call: CallbackQuery) -> None:
    """Листание результатов поиска."""
    page = call.data.split(":", 1)[1]
    query = storage.last_query(call.message.chat.id)
    if not query or not page.isdigit():
        return await call.answer("Повторите поиск", show_alert=True)
    rows = storage.search_stations(query, limit=60)
    await _edit(call, f"«{esc(query)}» — найдено {len(rows)}:",
                stations_kb(rows, int(page), nav_prefix="q:"))
    await call.answer()


@dp.callback_query(F.data.startswith("n:"))
async def cb_near_page(call: CallbackQuery) -> None:
    page = call.data.split(":", 1)[1]
    origin = storage.user_origin(call.message.chat.id)
    if not origin or not page.isdigit():
        return await call.answer("Пришлите геопозицию заново", show_alert=True)
    rows = storage.nearest(origin[0], origin[1], limit=20)
    await _edit(call, "Ближайшие АЗС:", stations_kb(rows, int(page), "n:"))
    await call.answer()


@dp.callback_query(F.data.startswith("a:"))
async def cb_available(call: CallbackQuery) -> None:
    """Где есть выбранная марка."""
    parts = call.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return await call.answer("Кнопка устарела", show_alert=True)
    text, keyboard = available_view(parts[1], int(parts[2]))
    await _edit(call, text, keyboard)
    await call.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_station(call: CallbackQuery) -> None:
    """Выбор АЗС — запоминаем его для этого чата."""
    station_id = storage.station_for_token(call.data.split(":", 1)[1])
    if not station_id:
        return await call.answer("Кнопка устарела, начните заново", show_alert=True)
    storage.set_current_station(call.message.chat.id, station_id)
    text, keyboard = station_view(call.message.chat.id, station_id)
    await _edit(call, text, keyboard)
    await call.answer("АЗС запомнена")


@dp.callback_query(F.data.startswith("t:"))
async def cb_toggle(call: CallbackQuery) -> None:
    """Включение/выключение слежения за маркой."""
    parts = call.data.split(":", 2)
    if len(parts) != 3:
        return await call.answer("Кнопка устарела", show_alert=True)
    station_id = storage.station_for_token(parts[1])
    if not station_id:
        return await call.answer("Кнопка устарела, начните заново", show_alert=True)

    chat_id, code = call.message.chat.id, parts[2]
    station = cache.get(station_id)
    fuel = station.get_fuel(code) if station else None
    label = fuel.code if fuel else code

    if code in storage.tracked_codes(chat_id, station_id):
        storage.del_sub(chat_id, station_id, code)
        note = f"🔕 {label}: слежение выключено"
    elif storage.count_subs(chat_id) >= MAX_SUBS:
        return await call.answer(f"Лимит {MAX_SUBS} подписок", show_alert=True)
    else:
        storage.add_sub(chat_id, station_id, code)
        note = f"🔔 {label}: сообщу, когда появится"

    if station:
        try:
            await call.message.edit_reply_markup(reply_markup=fuels_kb(chat_id, station))
        except TelegramBadRequest:
            pass
    await call.answer(note)


@dp.callback_query(F.data.startswith("u:"))
async def cb_unsub(call: CallbackQuery) -> None:
    """Снятие подписки с экрана «Подписки»."""
    parts = call.data.split(":", 2)
    if len(parts) != 3:
        return await call.answer("Кнопка устарела", show_alert=True)
    station_id = storage.station_for_token(parts[1])
    if not station_id:
        return await call.answer("Кнопка устарела", show_alert=True)
    storage.del_sub(call.message.chat.id, station_id, parts[2])
    if storage.list_subs(call.message.chat.id):
        await _edit(call, "За чем слежу (нажмите, чтобы отключить):",
                    subs_kb(call.message.chat.id))
    else:
        await _edit(call, "Подписок больше нет.", menu_kb())
    await call.answer("🔕 Отключено")


@dp.callback_query(F.data.startswith("h:"))
async def cb_history(call: CallbackQuery) -> None:
    station_id = storage.station_for_token(call.data.split(":", 1)[1])
    if not station_id:
        return await call.answer("Кнопка устарела", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К АЗС",
                              callback_data=f"s:{storage.token_for(station_id)}")],
        home_row()])
    await _edit(call, history_view(station_id), keyboard)
    await call.answer()


# ---------------------------------------------------------------- команды-псевдонимы

@dp.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        return await on_find_button(message)
    await show_search(message, query)


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await on_my_button(message)


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    await on_subs_button(message)


# ---------------------------------------------------------------- запуск

async def main() -> None:
    try:
        bot = BOT_CFG.build_bot()
    except config.ConfigError as exc:
        raise SystemExit(f"Ошибка настроек:\n{exc}\n\n"
                         "Проверить: python config.py")

    log.info("Токен %s, API %s", BOT_CFG.masked_token,
             BOT_CFG.api_base or "api.telegram.org")
    if APP_CFG.poll_interval_was_clamped:
        log.warning("POLL_INTERVAL поднят до минимальных %d с, чтобы не нагружать сайт",
                    POLL_INTERVAL)
    try:
        await refresh()
    except Exception as exc:
        log.error("Первичная загрузка не удалась: %s", exc)
    task = asyncio.create_task(poller(bot))   # ссылку держим: иначе задачу соберёт GC
    bot._poller_task = task                   # noqa: SLF001
    log.info("Бот запущен, интервал опроса %d с", POLL_INTERVAL)
    await dp.start_polling(bot, **BOT_CFG.polling_kwargs())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        storage.close()
