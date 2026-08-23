"""Модели данных: АЗС и топливо."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def norm_code(code: str) -> str:
    """Нормализованный код топлива для сравнения: 'G-100' -> 'G100', 'ДТл' -> 'ДТЛ'."""
    return (code or "").upper().replace(" ", "").replace("-", "").replace("_", "").strip()


@dataclass
class Fuel:
    code: str                          # как на сайте: '95', '92', 'G-100', 'ДТл'
    price: Optional[float] = None
    available: Optional[bool] = None   # True «В наличии», False «Отсутствует», None неизвестно
    status_text: str = ""              # исходный текст статуса
    updated_raw: str = ""              # как пришло с сайта: 'на 12:46 МСК'
    updated_at: Optional[datetime] = None   # разобранное время в МСК
    delivery: str = ""                 # 'В пути (еще ~2ч)'
    eta: Optional[datetime] = None     # расчётное время прибытия бензовоза

    @property
    def key(self) -> str:
        return norm_code(self.code)

    @property
    def mark(self) -> str:
        return "✅" if self.available else ("❌" if self.available is False else "❔")

    @property
    def status_ru(self) -> str:
        if self.status_text:
            return self.status_text
        return "В наличии" if self.available else (
            "Отсутствует" if self.available is False else "нет данных")

    def time_note(self, ref: Optional[datetime] = None) -> str:
        """'на 12:46 МСК (3 мин назад)' — время, к которому относится статус."""
        from parser import STALE_MINUTES, fmt_age, now_msk

        if self.updated_at is None:
            return self.updated_raw or ""
        ref = ref or now_msk()
        note = f"на {self.updated_at:%H:%M} МСК ({fmt_age(self.updated_at, ref)})"
        if (ref - self.updated_at).total_seconds() > STALE_MINUTES * 60:
            note += " ⚠️ устарело"
        return note

    def status_line(self, ref: Optional[datetime] = None) -> str:
        price = f"{self.price:.2f} ₽" if self.price is not None else "— ₽"
        parts = [f"{self.mark} <b>{self.code}</b> — {price}", self.status_ru]
        time_note = self.time_note(ref)
        if time_note:
            parts.append(time_note)
        line = " · ".join(parts)
        if self.delivery:
            eta = f" → ориентировочно к {self.eta:%H:%M} МСК" if self.eta else ""
            line += f"\n     🚛 {self.delivery}{eta}"
        return line

    def __str__(self) -> str:
        return self.status_line()


@dataclass
class Station:
    id: str
    name: str = ""
    address: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    schedule: str = ""
    notice: str = ""                   # 'Временно изменен порядок и объем заправки'
    region: str = ""
    map_url: str = ""                  # готовая ссылка с сайта, если она яндексовая
    yandex_oid: str = ""               # id карточки организации в Яндекс Картах
    fetched_at: Optional[datetime] = None   # когда мы сами забрали данные
    fuels: list[Fuel] = field(default_factory=list)

    def get_fuel(self, code: str) -> Optional[Fuel]:
        want = norm_code(code)
        for f in self.fuels:
            if f.key == want:
                return f
        return None

    @property
    def available_codes(self) -> list[str]:
        return [f.code for f in self.fuels if f.available]

    @property
    def as_of(self) -> Optional[datetime]:
        """Самая свежая отметка времени среди марок топлива."""
        stamps = [f.updated_at for f in self.fuels if f.updated_at]
        return max(stamps) if stamps else None

    def title(self) -> str:
        return self.address or self.name or f"АЗС {self.id}"

    @property
    def maps_link(self) -> Optional[str]:
        """Ссылка на Яндекс Карты: карточка организации, метка или поиск по адресу."""
        from maps import station_url

        return station_url(self)

    def route_link(self, origin=None) -> Optional[str]:
        from maps import station_route_url

        return station_route_url(self, origin)

    def summary(self) -> str:
        """Короткая сводка: что есть в наличии и на какое время."""
        codes = self.available_codes
        head = f"✅ есть: {', '.join(codes)}" if codes else "❌ в наличии ничего нет"
        if self.as_of:
            head += f" (на {self.as_of:%H:%M} МСК)"
        return head

    def pretty(self, ref: Optional[datetime] = None) -> str:
        from parser import now_msk

        ref = ref or now_msk()
        head = f"⛽️ <b>{self.title()}</b>\nID: <code>{self.id}</code>"
        if self.schedule:
            head += f"\n🕒 {self.schedule}"
        if self.notice:
            head += f"\n⚠️ {self.notice}"
        link = self.maps_link
        if link:
            head += f"\n📍 <a href=\"{link}\">Посмотреть на Яндекс Картах</a>"
        elif self.lat and self.lon:
            head += f"\n📍 {self.lat:.5f}, {self.lon:.5f}"

        # сначала то, что есть в наличии
        ordered = sorted(self.fuels, key=lambda f: (f.available is not True, f.key))
        body = "\n".join(f.status_line(ref) for f in ordered) or "нет данных по топливу"

        foot = f"\n\n{self.summary()}"
        if self.fetched_at:
            foot += f"\n🔄 проверено: {self.fetched_at:%H:%M} МСК"
        return f"{head}\n\n{body}{foot}"
