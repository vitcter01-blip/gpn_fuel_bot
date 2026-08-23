"""Проверки конфигурации: python test_config.py"""
import importlib
import os
import sys

import config as config_module


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

GOOD_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def check(label: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    assert cond, label


def with_env(**env):
    """Загружает конфиг с подменёнными переменными окружения."""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config_module)
        return config_module.load()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    print("токен:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN)
    check("настоящий токен принимается", not bot.problems())
    check("bot id извлечён", bot.bot_id == 123456789)

    bot, _ = with_env(BOT_TOKEN="")
    check("пустой токен отклоняется", bool(bot.problems()))

    bot, _ = with_env(BOT_TOKEN="123456789:AAExampleTokenReplaceMe")
    check("заглушка из .env.example отклоняется", bool(bot.problems()))

    bot, _ = with_env(BOT_TOKEN="простотекст")
    check("мусор вместо токена отклоняется", bool(bot.problems()))

    bot, _ = with_env(BOT_TOKEN=f'"{GOOD_TOKEN}"')
    check("токен в кавычках отклоняется с подсказкой",
          any("кавычки" in p for p in bot.problems()))

    bot, _ = with_env(BOT_TOKEN=f"  {GOOD_TOKEN}  ")
    check("пробелы по краям срезаются", not bot.problems())

    print("\nмаскирование токена (чтобы не утёк в логи):")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN)
    masked = bot.masked_token
    check(f"замаскирован: {masked}", GOOD_TOKEN not in masked and "…" in masked)
    check("id виден, секрет скрыт", masked.startswith("123456789:"))

    print("\nзащита чужого сайта от частого опроса:")
    _, app = with_env(BOT_TOKEN=GOOD_TOKEN, POLL_INTERVAL="10")
    check(f"10 с поднято до {config_module.MIN_POLL_INTERVAL} с",
          app.poll_interval == config_module.MIN_POLL_INTERVAL)
    check("факт подъёма отмечен для лога", app.poll_interval_was_clamped)
    _, app = with_env(BOT_TOKEN=GOOD_TOKEN, POLL_INTERVAL="600")
    check("нормальное значение не трогается", app.poll_interval == 600)
    check("подъёма не было", not app.poll_interval_was_clamped)

    print("\nразбор значений:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, ALERT_ON_GONE="нет")
    check("'нет' понимается как выключено", bot.alert_on_gone is False)
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, ALERT_ON_GONE="on")
    check("'on' понимается как включено", bot.alert_on_gone is True)
    try:
        with_env(BOT_TOKEN=GOOD_TOKEN, ALERT_ON_GONE="возможно")
        check("мусор в булевом значении -> ошибка", False)
    except config_module.ConfigError:
        check("мусор в булевом значении -> понятная ошибка", True)
    try:
        with_env(BOT_TOKEN=GOOD_TOKEN, MAX_SUBS_PER_USER="много")
        check("нечисловой лимит -> ошибка", False)
    except config_module.ConfigError:
        check("нечисловой лимит -> понятная ошибка", True)
    try:
        with_env(BOT_TOKEN=GOOD_TOKEN, MAX_SUBS_PER_USER="0")
        check("лимит 0 -> ошибка", False)
    except config_module.ConfigError:
        check("лимит 0 -> понятная ошибка", True)

    print("\nсписок админов:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_ADMIN_IDS="111, 222 333")
    check("разделители — запятая и пробел", bot.admin_ids == (111, 222, 333))
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_ADMIN_IDS=None)
    check("пустой список допустим", bot.admin_ids == ())

    print("\nсвой Bot API сервер и прокси:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_API_URL="http://127.0.0.1:8081")
    check("адрес принят", bot.api_base == "http://127.0.0.1:8081")
    check("режим локального сервера включается сам", bot.api_is_local is True)
    check("сессия нужна", bot.build_session is not None)
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_API_URL="127.0.0.1:8081")
    check("адрес без схемы отклоняется", bool(bot.problems()))
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_PROXY="socks5://127.0.0.1:9050")
    check("socks5-прокси принят", not bot.problems())
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN, BOT_PROXY="ftp://x")
    check("неизвестная схема прокси отклоняется", bool(bot.problems()))

    print("\nбез сети сессия не создаётся впустую:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN)
    check("без прокси и своего API сессия не нужна", bot.build_session() is None)

    print("\nпараметры long polling:")
    bot, _ = with_env(BOT_TOKEN=GOOD_TOKEN)
    kwargs = bot.polling_kwargs()
    check("типы обновлений ограничены нужными",
          kwargs["allowed_updates"] == ["message", "callback_query"])
    check("накопленные обновления пропускаются", kwargs["drop_pending_updates"] is True)

    print("\nВсе проверки конфигурации пройдены ✅")


if __name__ == "__main__":
    main()
