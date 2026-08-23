"""Проверки выборок топлива и городов: python test_storage.py"""
import sys

from models import Fuel, Station
from storage import Storage


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    assert condition, label


def main() -> None:
    db = Storage(":memory:")
    stations = [
        Station("1", address="Краснодар, Северная", fuels=[
            Fuel("АИ-92", available=True), Fuel("ДТ", available=True)]),
        Station("2", address="Краснодар, Российская", fuels=[
            Fuel("ДТ", available=True), Fuel("АИ-95", available=False)]),
        Station("3", address="Сочи, Донская", fuels=[
            Fuel("G-95", available=True)]),
    ]
    db.upsert_stations(stations)
    db.diff_and_update(stations)

    print("топливо по городу:")
    rows = db.stations_with_fuel_in_city("АИ-92", "Краснодар")
    check("92 найден в Краснодаре", [r["id"] for r in rows] == ["1"])
    check("92 не найден в Сочи", not db.stations_with_fuel_in_city("АИ-92", "Сочи"))

    print("\nвыбранный фильтр пользователя:")
    db.set_fuel_filter(777, "АИ92")
    db.set_city_filter(777, "Краснодар")
    check("марка сохранена", db.user_filters(777) == ("АИ92", "Краснодар"))
    rows = db.filtered_stations(777)
    check("фильтр возвращает только подходящие АЗС", [r["id"] for r in rows] == ["1"])

    print("\nбензин в наличии:")
    rows = db.stations_with_gasoline()
    check("найдены бензиновые АЗС", {r["id"] for r in rows} == {"1", "3"})
    check("дизельная АЗС исключена", "2" not in {r["id"] for r in rows})

    print("\nВсе проверки хранилища пройдены ✅")


if __name__ == "__main__":
    main()
