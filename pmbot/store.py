"""SQLite storage. Phase 1 tables: windows, book samples, taker-gap episodes,
public trades, and an event log (disconnects, errors). Every row carries `mode`
so Phase 2 dry-run and Phase 3 live rows never mix with monitor rows.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    key TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    asset TEXT, duration TEXT, start_ts REAL, end_ts REAL,
    structure TEXT, up_slug TEXT, down_slug TEXT, long_is_up INTEGER,
    title TEXT, description TEXT,
    open_ref TEXT,            -- JSON: candidate price-to-beat values
    close_ref TEXT,           -- JSON: candidate close values
    settlement REAL,          -- raw settlement value for up_slug (YES)
    outcome TEXT,             -- 'up' | 'down' | NULL
    settled_at REAL,
    first_seen REAL, finalized_at REAL
);
CREATE TABLE IF NOT EXISTS book_samples (
    ts REAL, mode TEXT, window_key TEXT, secs_to_close REAL,
    up_bid REAL, up_bid_qty REAL, up_ask REAL, up_ask_qty REAL,
    down_bid REAL, down_bid_qty REAL, down_ask REAL, down_ask_qty REAL,
    taker_cost REAL,          -- ask_up + ask_down + exact per-share taker fees
    maker_edge REAL,          -- 1 - bid_up - bid_down
    chainlink REAL, ptb REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_window ON book_samples(window_key);
CREATE TABLE IF NOT EXISTS taker_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT, window_key TEXT, asset TEXT, duration TEXT,
    start_ts REAL, end_ts REAL, duration_ms REAL,
    secs_to_close_at_start REAL,
    max_gap REAL, max_gap_ts REAL,
    top_size_at_max REAL, exec_size_at_max REAL, exec_profit_at_max REAL,
    depth_up_at_start REAL, depth_down_at_start REAL,
    updates INTEGER, ended_by TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    ts REAL, recv_ts REAL, mode TEXT, window_key TEXT, slug TEXT,
    price REAL, qty REAL, maker_side TEXT, maker_intent TEXT,
    taker_side TEXT, taker_intent TEXT, secs_into_window REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_window ON trades(window_key);
CREATE TABLE IF NOT EXISTS event_log (
    ts REAL, mode TEXT, level TEXT, kind TEXT, detail TEXT
);
"""


class Store:
    def __init__(self, path: str, mode: str = "monitor") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.mode = mode

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        row = {"mode": self.mode, **row}
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def upsert_window(self, row: dict[str, Any]) -> None:
        row = {"mode": self.mode, "first_seen": time.time(), **row}
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        updates = ",".join(f"{c}=excluded.{c}" for c in row if c not in ("key", "first_seen"))
        self.conn.execute(
            f"INSERT INTO windows ({cols}) VALUES ({qs}) ON CONFLICT(key) DO UPDATE SET {updates}",
            list(row.values()),
        )
        self.conn.commit()

    def update_window(self, key: str, **fields: Any) -> None:
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = json.dumps(v)
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE windows SET {sets} WHERE key=?", [*fields.values(), key])
        self.conn.commit()

    def add_sample(self, row: dict[str, Any]) -> None:
        self._insert("book_samples", row)

    def add_gap(self, row: dict[str, Any]) -> int:
        return self._insert("taker_gaps", row)

    def add_trade(self, row: dict[str, Any]) -> None:
        self._insert("trades", row)

    def log_event(self, level: str, kind: str, detail: str = "") -> None:
        self._insert("event_log", {"ts": time.time(), "level": level, "kind": kind, "detail": detail})

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, list(params)))
