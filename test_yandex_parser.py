"""Проверки разбора публичной карточки Яндекс Карт."""
import sys

from yandex_parser import is_blocked_text, parse_card_text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def main() -> None:
    assortment = """Газпромнефть
Краснодарский край, хутор Ленина, улица Ленина, 100
Особенности
Топливо: АИ-95, АИ-92, АИ-100, ДТ
Оплата картой
"""
    station = parse_card_text(
        assortment, "https://yandex.ru/maps/org/gazpromneft/1795622990/")
    check("oid взят из ссылки", station.id == "yandex:1795622990")
    check("адрес распознан", "хутор Ленина" in station.address)
    check("все марки распознаны", {f.key for f in station.fuels} ==
          {"АИ95", "АИ92", "АИ100", "ДТ"})
    check("ассортимент не считается наличием",
          all(f.available is None for f in station.fuels))

    explicit = """Газпромнефть
Краснодар, улица Российская, 1
Наличие топлива
АИ-100 — в наличии
АИ-95 — нет в наличии
Обновлено в 15:30
"""
    station = parse_card_text(explicit, "https://yandex.ru/maps/org/123456789/")
    check("явное наличие принято", station.get_fuel("АИ-100").available is True)
    check("явное отсутствие принято", station.get_fuel("АИ-95").available is False)
    check("время статуса сохранено", station.get_fuel("АИ-100").updated_at is not None)
    check("страница limited распознаётся как блокировка", is_blocked_text("limited"))

    print("Все проверки Яндекс-парсера пройдены ✅")


if __name__ == "__main__":
    main()
