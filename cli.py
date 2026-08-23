"""
Парсер из командной строки — без Telegram.

    python cli.py                        # все АЗС Краснодарского края
    python cli.py --fuel 92              # только там, где есть 92
    python cli.py --search Кропоткин
    python cli.py --json out.json        # выгрузка в JSON
    python cli.py --csv out.csv          # выгрузка в CSV
    python cli.py --all-regions
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict

from models import Station, norm_code
from parser import GpnClient


def filter_stations(stations: list[Station], fuel: str | None,
                    search: str | None, only_available: bool) -> list[Station]:
    result = stations
    if search:
        needle = search.lower()
        result = [s for s in result
                  if needle in s.address.lower() or needle in s.name.lower()]
    if fuel:
        want = norm_code(fuel)
        result = [s for s in result
                  if (f := s.get_fuel(want)) and (f.available or not only_available)]
    elif only_available:
        result = [s for s in result if s.available_codes]
    return result


async def run(args: argparse.Namespace) -> None:
    async with GpnClient() as client:
        stations = await (client.fetch_stations() if args.all_regions
                          else client.fetch_krasnodar())

    stations = filter_stations(stations, args.fuel, args.search, args.available)
    stations.sort(key=lambda s: s.title())

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            # asdict не включает свойства, поэтому ссылку добавляем явно
            payload = []
            for st in stations:
                item = asdict(st)
                item["maps_link"] = st.maps_link
                item["route_link"] = st.route_link()
                payload.append(item)
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        print(f"JSON: {args.json} ({len(stations)} АЗС)")
        return

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["id", "адрес", "широта", "долгота", "режим",
                             "топливо", "цена", "в_наличии", "статус",
                             "данные_на_МСК", "доставка", "прибытие_МСК", "проверено_МСК",
                             "яндекс_карты"])
            for st in stations:
                for f in st.fuels:
                    writer.writerow([
                        st.id, st.title(), st.lat, st.lon, st.schedule, f.code, f.price,
                        "да" if f.available else ("нет" if f.available is False else "?"),
                        f.status_ru,
                        f"{f.updated_at:%Y-%m-%d %H:%M}" if f.updated_at else "",
                        f.delivery,
                        f"{f.eta:%H:%M}" if f.eta else "",
                        f"{st.fetched_at:%H:%M}" if st.fetched_at else "",
                        st.maps_link or ""])
        print(f"CSV: {args.csv} ({len(stations)} АЗС)")
        return

    total_available = 0
    for st in stations:
        print(f"\n[{st.id}] {st.title()}")
        if st.notice:
            print(f"  ⚠️  {st.notice}")
        for f in sorted(st.fuels, key=lambda f: (f.available is not True, f.key)):
            mark = "✅" if f.available else ("❌" if f.available is False else "❔")
            price = f"{f.price:>7.2f} ₽" if f.price is not None else "      — ₽"
            when = f"  на {f.updated_at:%H:%M} МСК" if f.updated_at else ""
            extra = f"  🚛 {f.delivery}" if f.delivery else ""
            if f.eta:
                extra += f" → к {f.eta:%H:%M}"
            print(f"  {mark} {f.code:<6} {price}  {f.status_ru:<12}{when}{extra}")
        print(f"  → {st.summary()}")
        if st.maps_link:
            print(f"  🗺  {st.maps_link}")
        total_available += bool(st.available_codes)
    print(f"\nВсего АЗС: {len(stations)}; с топливом в наличии: {total_available}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер статуса АЗС Газпромнефть")
    parser.add_argument("--fuel", help="код топлива: 95, 92, G-100, ДТл")
    parser.add_argument("--search", help="подстрока адреса")
    parser.add_argument("--available", action="store_true", help="только то, что есть в наличии")
    parser.add_argument("--all-regions", action="store_true", help="не ограничивать регионом")
    parser.add_argument("--json", metavar="FILE")
    parser.add_argument("--csv", metavar="FILE")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
