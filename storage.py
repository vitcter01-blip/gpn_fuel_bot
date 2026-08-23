"""Хранилище: подписки пользователей и предыдущее состояние топлива (SQLite)."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import Station, norm_code

# путь к базе можно переопределить: GPN_DB=/var/lib/gpn/gpn.db
DB_PATH = Path(os.getenv("GPN_DB") or Path(__file__).with_name("gpn.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    name TEXT, address TEXT, lat REAL, lon REAL,
    schedule TEXT, notice TEXT, region TEXT, seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS fuel_state (
    station_id TEXT, fuel_code TEXT, available INTEGER, price REAL,
    status_text TEXT, checked_at INTEGER,
    display_code TEXT,   -- 'G-100' как на сайте (fuel_code хранит 'G100')
    site_time INTEGER,   -- отметка времени самого сайта, 'на 12:46 МСК'
    PRIMARY KEY (station_id, fuel_code)
);
CREATE TABLE IF NOT EXISTS subs (
    chat_id INTEGER, station_id TEXT, fuel_code TEXT, created_at INTEGER,
    PRIMARY KEY (chat_id, station_id, fuel_code)
);
CREATE INDEX IF NOT EXISTS idx_subs_target ON subs(station_id, fuel_code);
CREATE INDEX IF NOT EXISTS idx_stations_addr ON stations(address);

-- Запомненный выбор пользователя: какая АЗС «текущая» в диалоге
CREATE TABLE IF NOT EXISTS user_state (
    chat_id INTEGER PRIMARY KEY,
    station_id TEXT,
    updated_at INTEGER,
    last_query TEXT,            -- последний поисковый запрос: для листания страниц
    selected_fuel TEXT,
    selected_city TEXT
);

-- Журнал изменений статуса по каждой марке: нужен, чтобы знать,
-- сколько времени топливо уже отсутствует, и показывать историю
CREATE TABLE IF NOT EXISTS fuel_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT, fuel_code TEXT, display_code TEXT,
    available INTEGER, price REAL, site_time INTEGER, changed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_hist ON fuel_history(station_id, fuel_code, changed_at);

-- Короткие токены для inline-кнопок: callback_data в Telegram ограничен 64 байтами,
-- а id АЗС может быть длинным
CREATE TABLE IF NOT EXISTS tokens (
    tok INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT UNIQUE
);
"""

HISTORY_LIMIT = 60          # сколько записей истории держим на каждую марку

ANY_STATION = "*"  # подписка на весь Краснодарский край


@dataclass
class Change:
    station_id: str
    fuel_code: str          # отображаемый код, 'G-100'
    was: Optional[bool]
    now: Optional[bool]
    price: Optional[float]
    site_time: Optional[datetime] = None   # время, к которому относится новый статус

    @property
    def appeared(self) -> bool:
        return self.now is True and self.was is not True

    @property
    def gone(self) -> bool:
        return self.now is False and self.was is True


class Storage:
    def __init__(self, path: Path = DB_PATH):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # встроенный lower() в SQLite работает только с ASCII и не видит кириллицу
        self.db.create_function("pylower", 1, lambda s: s.lower() if isinstance(s, str) else s)
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся позже, чтобы старая БД не ломалась."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(fuel_state)")}
        for name, decl in (("display_code", "TEXT"), ("site_time", "INTEGER"),
                           ("checked_at", "INTEGER")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE fuel_state ADD COLUMN {name} {decl}")
        ucols = {r["name"] for r in self.db.execute("PRAGMA table_info(user_state)")}
        for name, decl in (("last_query", "TEXT"), ("selected_fuel", "TEXT"),
                           ("selected_city", "TEXT")):
            if ucols and name not in ucols:
                self.db.execute(f"ALTER TABLE user_state ADD COLUMN {name} {decl}")

    # ---------------------------------------------------------------- АЗС

    def upsert_stations(self, stations: list[Station]) -> None:
        now = int(time.time())
        rows = [(s.id, s.name, s.address, s.lat, s.lon, s.schedule, s.notice, s.region, now)
                for s in stations]
        with self._lock:
            self.db.executemany(
                "INSERT INTO stations (id,name,address,lat,lon,schedule,notice,region,seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, address=excluded.address, lat=excluded.lat, "
                "lon=excluded.lon, schedule=excluded.schedule, notice=excluded.notice, "
                "region=excluded.region, seen_at=excluded.seen_at", rows)
            self.db.commit()

    def search_stations(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        like = f"%{query.strip().lower()}%"
        with self._lock:
            return self.db.execute(
                "SELECT * FROM stations WHERE pylower(address) LIKE ? "
                "OR pylower(name) LIKE ? OR id = ? LIMIT ?",
                (like, like, query.strip(), limit)).fetchall()

    def station_row(self, station_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()

    # ---------------------------------------------------------------- состояние

    def snapshot(self, station_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM fuel_state WHERE station_id = ? ORDER BY fuel_code",
                (station_id,)).fetchall()

    def diff_and_update(self, stations: list[Station]) -> list[Change]:
        """Сравнивает свежие данные с сохранёнными и записывает новое состояние."""
        now = int(time.time())
        changes: list[Change] = []
        with self._lock:
            prev = {(r["station_id"], r["fuel_code"]): r
                    for r in self.db.execute("SELECT * FROM fuel_state").fetchall()}
            rows, history = [], []
            for st in stations:
                for f in st.fuels:
                    key = (st.id, f.key)
                    old = prev.get(key)
                    was = None if old is None or old["available"] is None else bool(old["available"])
                    site_ts = int(f.updated_at.timestamp()) if f.updated_at else None
                    # первое наблюдение тоже пишем в историю — иначе не от чего
                    # отсчитывать, «сколько уже нет» этой марки
                    if old is None or was != f.available:
                        history.append((st.id, f.key, f.code,
                                        None if f.available is None else int(f.available),
                                        f.price, site_ts, now))
                    if old is not None and was != f.available:
                        changes.append(Change(st.id, f.code, was, f.available,
                                              f.price, f.updated_at))
                    rows.append((st.id, f.key, f.code,
                                 None if f.available is None else int(f.available),
                                 f.price, f.status_text or "", site_ts, now))
            self.db.executemany(
                "INSERT INTO fuel_state (station_id,fuel_code,display_code,available,price,"
                "status_text,site_time,checked_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(station_id,fuel_code) DO UPDATE SET "
                "display_code=excluded.display_code, available=excluded.available, "
                "price=excluded.price, status_text=excluded.status_text, "
                "site_time=excluded.site_time, checked_at=excluded.checked_at", rows)
            if history:
                self.db.executemany(
                    "INSERT INTO fuel_history (station_id,fuel_code,display_code,available,"
                    "price,site_time,changed_at) VALUES (?,?,?,?,?,?,?)", history)
            self.db.commit()
        return changes

    # ---------------------------------------------------------------- история

    def history(self, station_id: str, fuel_code: str,
                limit: int = 10) -> list[sqlite3.Row]:
        """Последние изменения статуса по марке, свежие сверху."""
        with self._lock:
            return self.db.execute(
                "SELECT * FROM fuel_history WHERE station_id=? AND fuel_code=? "
                "ORDER BY changed_at DESC LIMIT ?",
                (station_id, norm_code(fuel_code), limit)).fetchall()

    def since(self, station_id: str, fuel_code: str) -> Optional[int]:
        """Unix-время, с которого держится текущий статус марки."""
        rows = self.history(station_id, fuel_code, limit=1)
        return rows[0]["changed_at"] if rows else None

    def prune_history(self, keep: int = HISTORY_LIMIT) -> int:
        """Оставляет только последние `keep` записей на каждую марку."""
        with self._lock:
            try:
                cur = self.db.execute(
                    "DELETE FROM fuel_history WHERE id NOT IN ("
                    "  SELECT id FROM ("
                    "    SELECT id, ROW_NUMBER() OVER ("
                    "      PARTITION BY station_id, fuel_code ORDER BY changed_at DESC) rn"
                    "    FROM fuel_history) WHERE rn <= ?)", (keep,))
            except sqlite3.OperationalError:
                # оконные функции появились в SQLite 3.25; на старых сборках
                # чистим по каждой марке отдельно
                pairs = self.db.execute(
                    "SELECT DISTINCT station_id, fuel_code FROM fuel_history").fetchall()
                removed = 0
                for pair in pairs:
                    cur = self.db.execute(
                        "DELETE FROM fuel_history WHERE station_id=? AND fuel_code=? "
                        "AND id NOT IN (SELECT id FROM fuel_history WHERE station_id=? "
                        "AND fuel_code=? ORDER BY changed_at DESC LIMIT ?)",
                        (pair["station_id"], pair["fuel_code"],
                         pair["station_id"], pair["fuel_code"], keep))
                    removed += cur.rowcount
                self.db.commit()
                return removed
            self.db.commit()
            return cur.rowcount

    # ---------------------------------------------------------------- выбор пользователя

    def set_current_station(self, chat_id: int, station_id: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO user_state (chat_id, station_id, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET station_id=excluded.station_id, "
                "updated_at=excluded.updated_at", (chat_id, station_id, int(time.time())))
            self.db.commit()

    def set_last_query(self, chat_id: int, query: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO user_state (chat_id, last_query, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_query=excluded.last_query, "
                "updated_at=excluded.updated_at", (chat_id, query, int(time.time())))
            self.db.commit()

    def last_query(self, chat_id: int) -> Optional[str]:
        with self._lock:
            row = self.db.execute(
                "SELECT last_query FROM user_state WHERE chat_id=?", (chat_id,)).fetchone()
        return row["last_query"] if row else None

    def current_station(self, chat_id: int) -> Optional[str]:
        with self._lock:
            row = self.db.execute(
                "SELECT station_id FROM user_state WHERE chat_id=?", (chat_id,)).fetchone()
        return row["station_id"] if row else None

    def set_fuel_filter(self, chat_id: int, fuel_code: str) -> None:
        fuel_key = norm_code(fuel_code)
        with self._lock:
            self.db.execute(
                "INSERT INTO user_state (chat_id,selected_fuel,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET selected_fuel=excluded.selected_fuel, "
                "updated_at=excluded.updated_at", (chat_id, fuel_key, int(time.time())))
            self.db.commit()

    def set_city_filter(self, chat_id: int, city: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO user_state (chat_id,selected_city,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET selected_city=excluded.selected_city, "
                "updated_at=excluded.updated_at", (chat_id, city.strip(), int(time.time())))
            self.db.commit()

    def user_filters(self, chat_id: int) -> tuple[Optional[str], Optional[str]]:
        with self._lock:
            row = self.db.execute(
                "SELECT selected_fuel,selected_city FROM user_state WHERE chat_id=?",
                (chat_id,)).fetchone()
        return ((row["selected_fuel"], row["selected_city"])
                if row else (None, None))

    def filtered_stations(self, chat_id: int, limit: int = 60) -> list[sqlite3.Row]:
        fuel_key, city = self.user_filters(chat_id)
        if not fuel_key or not city:
            return []
        like = f"%{city.lower()}%"
        with self._lock:
            return self.db.execute(
                "SELECT DISTINCT s.* FROM stations s JOIN fuel_state f ON f.station_id=s.id "
                "WHERE f.fuel_code=? AND f.available=1 AND "
                "(pylower(s.address) LIKE ? OR pylower(s.name) LIKE ?) "
                "ORDER BY s.address LIMIT ?", (fuel_key, like, like, limit)).fetchall()

    def fuel_codes(self) -> list[tuple[str, str]]:
        """Все марки, встречавшиеся в данных: (ключ, отображаемый код)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT fuel_code, MAX(display_code) disp, COUNT(*) n FROM fuel_state "
                "GROUP BY fuel_code ORDER BY n DESC").fetchall()
        return [(r["fuel_code"], r["disp"] or r["fuel_code"]) for r in rows]

    def stations_with_fuel(self, fuel_code: str, only_available: bool = True
                           ) -> list[sqlite3.Row]:
        """АЗС, где марка есть в наличии — берём из БД, чтобы работало и после
        перезапуска, когда кэш ещё пуст."""
        sql = ("SELECT s.id, s.address, s.name, f.price, f.site_time, f.available "
               "FROM fuel_state f JOIN stations s ON s.id = f.station_id "
               "WHERE f.fuel_code = ?")
        if only_available:
            sql += " AND f.available = 1"
        sql += " ORDER BY s.address"
        with self._lock:
            return self.db.execute(sql, (norm_code(fuel_code),)).fetchall()

    def stations_with_fuel_in_city(self, fuel_code: str, city: str,
                                   only_available: bool = True) -> list[sqlite3.Row]:
        """АЗС с выбранной маркой в городе."""
        city_like = f"%{city.strip().lower()}%"
        sql = ("SELECT s.id, s.address, s.name, f.price, f.site_time, f.available "
               "FROM fuel_state f JOIN stations s ON s.id = f.station_id "
               "WHERE f.fuel_code = ? AND (pylower(s.address) LIKE ? "
               "OR pylower(s.name) LIKE ?)")
        if only_available:
            sql += " AND f.available = 1"
        sql += " ORDER BY s.address"
        with self._lock:
            return self.db.execute(
                sql, (norm_code(fuel_code), city_like, city_like)).fetchall()

    def stations_with_gasoline(self) -> list[sqlite3.Row]:
        """АЗС, где доступна хотя бы одна бензиновая марка 92/95/98/100."""
        with self._lock:
            rows = self.db.execute(
                "SELECT s.*, f.fuel_code FROM fuel_state f "
                "JOIN stations s ON s.id = f.station_id "
                "WHERE f.available = 1 ORDER BY s.address").fetchall()

        result, seen = [], set()
        for row in rows:
            key = norm_code(row["fuel_code"])
            if row["id"] not in seen and any(grade in key for grade in ("92", "95", "98", "100")):
                result.append(row)
                seen.add(row["id"])
        return result

    # ---------------------------------------------------------------- токены кнопок

    def token_for(self, station_id: str) -> str:
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO tokens (station_id) VALUES (?)", (station_id,))
            row = self.db.execute(
                "SELECT tok FROM tokens WHERE station_id=?", (station_id,)).fetchone()
            self.db.commit()
        return str(row["tok"])

    def station_for_token(self, token: str) -> Optional[str]:
        if not str(token).isdigit():
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT station_id FROM tokens WHERE tok=?", (int(token),)).fetchone()
        return row["station_id"] if row else None

    # ---------------------------------------------------------------- подписки

    def add_sub(self, chat_id: int, station_id: str, fuel_code: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO subs VALUES (?,?,?,?)",
                (chat_id, station_id, norm_code(fuel_code), int(time.time())))
            self.db.commit()

    def del_sub(self, chat_id: int, station_id: str, fuel_code: str) -> int:
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM subs WHERE chat_id=? AND station_id=? AND fuel_code=?",
                (chat_id, station_id, norm_code(fuel_code)))
            self.db.commit()
            return cur.rowcount

    def clear_subs(self, chat_id: int) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM subs WHERE chat_id=?", (chat_id,))
            self.db.commit()
            return cur.rowcount

    def list_subs(self, chat_id: int) -> list[sqlite3.Row]:
        """Подписки пользователя. display_code подтягиваем из состояния топлива:
        в subs хранится нормализованный ключ ('G100'), а показывать нужно 'G-100'."""
        with self._lock:
            return self.db.execute(
                "SELECT s.*, st.address, COALESCE("
                "  (SELECT f.display_code FROM fuel_state f "
                "     WHERE f.fuel_code = s.fuel_code AND f.station_id = s.station_id), "
                "  (SELECT f.display_code FROM fuel_state f "
                "     WHERE f.fuel_code = s.fuel_code LIMIT 1), "
                "  s.fuel_code) AS display_code "
                "FROM subs s LEFT JOIN stations st ON st.id = s.station_id "
                "WHERE s.chat_id = ? ORDER BY s.created_at", (chat_id,)).fetchall()

    def count_subs(self, chat_id: int) -> int:
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) c FROM subs WHERE chat_id=?", (chat_id,)).fetchone()["c"]

    def tracked_codes(self, chat_id: int, station_id: str) -> set[str]:
        """Нормализованные коды, на которые пользователь подписан на этой АЗС
        (включая подписки на весь край) — для отметок на кнопках."""
        with self._lock:
            rows = self.db.execute(
                "SELECT fuel_code FROM subs WHERE chat_id=? AND (station_id=? OR station_id=?)",
                (chat_id, station_id, ANY_STATION)).fetchall()
        return {r["fuel_code"] for r in rows}

    def subscribers_for(self, station_id: str, fuel_code: str) -> list[int]:
        code = norm_code(fuel_code)
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT chat_id FROM subs WHERE (station_id=? OR station_id=?) "
                "AND (fuel_code=? OR fuel_code=?)",
                (station_id, ANY_STATION, code, ANY_STATION)).fetchall()
        return [r["chat_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self.db.close()
