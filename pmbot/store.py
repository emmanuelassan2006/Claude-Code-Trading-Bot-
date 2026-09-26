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
    first_seen REAL, finalized_at REAL,
    ptb_api REAL,             -- assetPriceTerms.priceToBeat (authoritative)
    settle_api REAL,          -- assetPriceTerms.settlementPrice
    index_symbol TEXT, fee_coefficient REAL, tick REAL
);
CREATE TABLE IF NOT EXISTS book_samples (
    ts REAL, mode TEXT, window_key TEXT, secs_to_close REAL,
    up_bid REAL, up_bid_qty REAL, up_ask REAL, up_ask_qty REAL,
    down_bid REAL, down_bid_qty REAL, down_ask REAL, down_ask_qty REAL,
    taker_cost REAL,          -- ask_up + ask_down + exact per-share taker fees
    maker_edge REAL,          -- 1 - bid_up - bid_down
    ref_price REAL, ptb REAL
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
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, mode TEXT, window_key TEXT, strategy TEXT, kind TEXT,
    side TEXT, price REAL, qty INTEGER, filled INTEGER DEFAULT 0,
    status TEXT, reason TEXT,
    fair REAL, exp_edge_ps REAL,
    signal_ts REAL, sent_ts REAL, ack_ts REAL, done_ts REAL
);
CREATE TABLE IF NOT EXISTS order_events (
    ts REAL, mode TEXT, order_id TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, ts REAL, order_id TEXT,
    window_key TEXT, strategy TEXT, kind TEXT, side TEXT, price REAL, qty INTEGER,
    fee REAL,               -- net: positive = fee paid, negative = rebate
    fair_at_fill REAL, markout_1 REAL, markout_2 REAL,
    mid_at_fill REAL, mkt_markout_1 REAL, mkt_markout_2 REAL
);
CREATE INDEX IF NOT EXISTS ix_fills_window ON fills(window_key);
CREATE TABLE IF NOT EXISTS window_results (
    window_key TEXT, mode TEXT, asset TEXT, duration TEXT, end_ts REAL,
    outcome TEXT, outcome_source TEXT, final_pos INTEGER, cash REAL, pnl REAL,
    maker_fills INTEGER, taker_fills INTEGER, fees REAL, rebates REAL,
    carried_inventory INTEGER, locked TEXT,
    PRIMARY KEY (window_key, mode)
);
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
        self._migrate()
        self.mode = mode

    # columns added after the first release; ALTER existing databases in place
    _ADDED = {
        "windows": {"ptb_api": "REAL", "settle_api": "REAL", "index_symbol": "TEXT",
                    "fee_coefficient": "REAL", "tick": "REAL"},
        "book_samples": {"ref_price": "REAL"},
        "fills": {"mid_at_fill": "REAL", "mkt_markout_1": "REAL", "mkt_markout_2": "REAL"},
    }

    def _migrate(self) -> None:
        for table, cols in self._ADDED.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

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

    def insert(self, table: str, row: dict[str, Any]) -> int:
        return self._insert(table, row)

    def update(self, table: str, where: str, params: list[Any], **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*fields.values(), *params])
        self.conn.commit()

    def log_event(self, level: str, kind: str, detail: str = "") -> None:
        self._insert("event_log", {"ts": time.time(), "level": level, "kind": kind, "detail": detail})

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, list(params)))
