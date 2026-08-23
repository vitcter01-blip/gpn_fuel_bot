"""
Конфигурация проекта. Всё, что касается Telegram Bot API, собрано в BotConfig,
чтобы настройки не были размазаны по коду через os.getenv.

Проверить настройки перед запуском, не поднимая бота:

    python config.py

Выведет действующие значения (токен замаскирован) и список проблем, если они есть.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Опрашивать сайт чаще нельзя: это чужой публичный сервис, и частые запросы
# с одного адреса приведут к блокировке IP сервера.
MIN_POLL_INTERVAL = 120

# Формат токена @BotFather: <числовой id>:<строка не короче 30 символов>
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

# сравнение идёт в нижнем регистре, поэтому и набор храним так же
PLACEHOLDERS = {s.lower() for s in (
    "", "your_token_here", "123456789:AAExampleTokenReplaceMe",
    "changeme", "xxx", "token", "вставьте_токен")}


class ConfigError(ValueError):
    """Настройки заданы так, что запускаться нельзя."""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on", "да"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "нет"}:
        return False
    raise ConfigError(f"{name}: ожидалось да/нет, получено {raw!r}")


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}: минимум {minimum}, получено {value}")
    return value


def _env_ids(name: str) -> tuple[int, ...]:
    raw = _env(name)
    if not raw:
        return ()
    ids = []
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name}: {part!r} не похоже на id пользователя") from exc
    return tuple(ids)


# ---------------------------------------------------------------- Telegram Bot API

@dataclass(frozen=True)
class BotConfig:
    """Настройки Telegram Bot API."""

    token: str
    parse_mode: str = "HTML"

    # Локальный Bot API сервер (github.com/tdlib/telegram-bot-api).
    # Нужен, если official api.telegram.org недоступен из вашей сети
    # или требуются повышенные лимиты.
    api_base: Optional[str] = None
    api_is_local: bool = False

    proxy: Optional[str] = None          # http://user:pass@host:port или socks5://...
    request_timeout: int = 60            # таймаут запроса к Bot API, секунд

    # long polling
    polling_timeout: int = 30
    drop_pending_updates: bool = True    # не разгребать очередь, накопленную при простое
    allowed_updates: tuple[str, ...] = ("message", "callback_query")

    # рассылка уведомлений
    send_delay: float = 0.05             # пауза между сообщениями, ~20 сообщений/сек
    max_retry_after: int = 300           # дольше этого ждать по 429 не будем
    send_retries: int = 2

    admin_ids: tuple[int, ...] = ()
    max_subs_per_user: int = 30
    alert_on_gone: bool = True

    # ------------------------------------------------------------ создание

    @classmethod
    def from_env(cls) -> "BotConfig":
        api_base = _env("BOT_API_URL") or None
        return cls(
            token=_env("BOT_TOKEN"),
            parse_mode=_env("BOT_PARSE_MODE", "HTML").upper(),
            api_base=api_base,
            api_is_local=_env_bool("BOT_API_LOCAL", bool(api_base)),
            proxy=_env("BOT_PROXY") or None,
            request_timeout=_env_int("BOT_REQUEST_TIMEOUT", 60, minimum=5),
            polling_timeout=_env_int("BOT_POLLING_TIMEOUT", 30, minimum=1),
            drop_pending_updates=_env_bool("BOT_DROP_PENDING", True),
            send_delay=max(0.0, float(_env("BOT_SEND_DELAY", "0.05") or 0.05)),
            max_retry_after=_env_int("BOT_MAX_RETRY_AFTER", 300, minimum=1),
            send_retries=_env_int("BOT_SEND_RETRIES", 2, minimum=0),
            admin_ids=_env_ids("BOT_ADMIN_IDS"),
            max_subs_per_user=_env_int("MAX_SUBS_PER_USER", 30, minimum=1),
            alert_on_gone=_env_bool("ALERT_ON_GONE", True),
        )

    # ------------------------------------------------------------ проверки

    @property
    def masked_token(self) -> str:
        """Токен для логов: 123456789:AAHd…Dsaw — целиком светить нельзя."""
        if not self.token:
            return "<не задан>"
        head, _, tail = self.token.partition(":")
        return f"{head}:{tail[:4]}…{tail[-4:]}" if len(tail) > 10 else f"{head}:…"

    @property
    def bot_id(self) -> Optional[int]:
        head = self.token.partition(":")[0]
        return int(head) if head.isdigit() else None

    def problems(self) -> list[str]:
        """Список проблем; пустой список означает, что можно запускаться."""
        issues = []
        if self.token.lower() in PLACEHOLDERS:
            issues.append("BOT_TOKEN не задан или остался заглушкой из .env.example")
        elif not TOKEN_RE.match(self.token):
            issues.append(
                "BOT_TOKEN не похож на токен @BotFather "
                "(ожидается вид 123456789:AA...). Проверьте, не попали ли пробелы "
                "или кавычки при копировании")
        if self.parse_mode not in {"HTML", "MARKDOWN", "MARKDOWNV2"}:
            issues.append(f"BOT_PARSE_MODE: неизвестное значение {self.parse_mode!r}")
        if self.api_base and not self.api_base.startswith(("http://", "https://")):
            issues.append("BOT_API_URL должен начинаться с http:// или https://")
        if self.proxy and not self.proxy.startswith(("http://", "https://", "socks5://")):
            issues.append("BOT_PROXY: поддерживаются схемы http, https, socks5")
        return issues

    def validate(self) -> "BotConfig":
        issues = self.problems()
        if issues:
            raise ConfigError("\n".join(f"  - {i}" for i in issues))
        return self

    # ------------------------------------------------------------ сборка объектов

    def build_session(self):
        """Сессия aiogram. Нужна только при своём Bot API сервере или прокси."""
        if not self.api_base and not self.proxy:
            return None
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        api = None
        if self.api_base:
            api = TelegramAPIServer.from_base(self.api_base.rstrip("/"),
                                              is_local=self.api_is_local)
        return AiohttpSession(api=api, proxy=self.proxy,
                              timeout=self.request_timeout)

    def build_bot(self):
        """Готовый объект Bot со всеми настройками."""
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties

        self.validate()
        return Bot(
            self.token,
            session=self.build_session(),
            default=DefaultBotProperties(parse_mode=self.parse_mode),
        )

    def polling_kwargs(self) -> dict:
        return {
            "polling_timeout": self.polling_timeout,
            "drop_pending_updates": self.drop_pending_updates,
            "allowed_updates": list(self.allowed_updates),
        }


# ---------------------------------------------------------------- остальное

@dataclass(frozen=True)
class AppConfig:
    """Настройки опроса сайта и хранилища."""

    poll_interval: int = 300
    requested_poll_interval: int = 300   # что просили до ограничения снизу
    db_path: Path = field(default_factory=lambda: Path(__file__).with_name("gpn.db"))
    api_url: Optional[str] = None        # адрес API сайта вручную
    benzuber_api_key: Optional[str] = None
    history_limit: int = 60
    page_size: int = 8

    @classmethod
    def from_env(cls) -> "AppConfig":
        requested = _env_int("POLL_INTERVAL", 300, minimum=1)
        # не падаем, но и не разрешаем долбить чужой сайт
        interval = max(requested, MIN_POLL_INTERVAL)
        return cls(
            poll_interval=interval,
            requested_poll_interval=requested,
            db_path=Path(_env("GPN_DB") or Path(__file__).with_name("gpn.db")),
            api_url=_env("GPN_API_URL") or None,
            benzuber_api_key=_env("BENZUBER_API_KEY") or None,
            history_limit=_env_int("HISTORY_LIMIT", 60, minimum=2),
            page_size=_env_int("PAGE_SIZE", 8, minimum=1),
        )

    @property
    def poll_interval_was_clamped(self) -> bool:
        """Значение фиксируется при загрузке: читать окружение заново нельзя,
        оно может измениться, и признак стал бы неверным."""
        return self.requested_poll_interval < self.poll_interval

    @property
    def masked_benzuber_api_key(self) -> str:
        if not self.benzuber_api_key:
            return "не задан"
        key = self.benzuber_api_key
        return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "задан (скрыт)"


def load() -> tuple[BotConfig, AppConfig]:
    return BotConfig.from_env(), AppConfig.from_env()


# ---------------------------------------------------------------- python config.py

def _report() -> int:
    bot, app = load()
    print("Telegram Bot API")
    print(f"  токен              {bot.masked_token}")
    print(f"  bot id             {bot.bot_id or '—'}")
    print(f"  режим разметки     {bot.parse_mode}")
    print(f"  адрес API          {bot.api_base or 'api.telegram.org (официальный)'}")
    print(f"  локальный сервер   {'да' if bot.api_is_local else 'нет'}")
    print(f"  прокси             {bot.proxy or '—'}")
    print(f"  таймаут запроса    {bot.request_timeout} с")
    print(f"  long polling       {bot.polling_timeout} с, "
          f"пропуск накопленного: {'да' if bot.drop_pending_updates else 'нет'}")
    print(f"  типы обновлений    {', '.join(bot.allowed_updates)}")
    print(f"  пауза в рассылке   {bot.send_delay} с")
    print(f"  админы             {', '.join(map(str, bot.admin_ids)) or '—'}")
    print(f"  лимит подписок     {bot.max_subs_per_user}")
    print(f"  алерт «закончился» {'да' if bot.alert_on_gone else 'нет'}")
    print("\nОпрос сайта и хранилище")
    clamp = "  ⚠️ поднято до минимума" if app.poll_interval_was_clamped else ""
    print(f"  интервал опроса    {app.poll_interval} с{clamp}")
    print(f"  база               {app.db_path}")
    print(f"  адрес API сайта    {app.api_url or 'определяется автоматически'}")
    print(f"  Benzuber API key   {app.masked_benzuber_api_key}")

    issues = bot.problems()
    if issues:
        print("\n❌ Запускаться нельзя:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("\n✅ Настройки корректны")
    return 0


if __name__ == "__main__":
    raise SystemExit(_report())
