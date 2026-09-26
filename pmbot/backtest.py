"""Replay recorded monitor data through the real engine (strategy + risk +
paper exchange). Uses only information available at each moment: prices are
fed into the history as the replay clock reaches them.

Limits of the recording (be conservative when reading results):
  * books are 1 Hz top-of-book samples, so takers only see level 1 and act
    up to ~1 s late;
  * the Chainlink price is sampled with each book sample (1 Hz).
"""

from __future__ import annotations

import heapq
import json
import logging
from decimal import Decimal
from typing import Any

from pmbot.book import Level
from pmbot.config import Config
from pmbot.discovery import Window
from pmbot.engine import TradingEngine
from pmbot.feeds.history import PriceHistory, Tick
from pmbot.store import Store

log = logging.getLogger(__name__)


def _lvl(p: Any, q: Any) -> list[Level]:
    if p is None or q is None or q <= 0:
        return []
    return [Level(Decimal(str(p)), Decimal(str(q)))]


def run_backtest(cfg: Config, src: Store, out: Store, since: float | None = None) -> dict[str, Any]:
    rows = [dict(r) for r in src.query(
        "SELECT * FROM windows WHERE structure='single' AND start_ts >= ? ORDER BY start_ts",
        [since or 0])]
    windows = {r["key"]: r for r in rows}
    if not windows:
        return {"windows": 0}
    keys = list(windows)
    qs = ",".join("?" * len(keys))
    samples = [dict(r) for r in src.query(
        f"SELECT * FROM book_samples WHERE window_key IN ({qs}) ORDER BY ts", keys)]
    trades = [dict(r) for r in src.query(
        f"SELECT * FROM trades WHERE window_key IN ({qs}) ORDER BY ts", keys)]

    histories = {a: PriceHistory(max_age_s=cfg.price_feed.history_s)
                 for a in {w["asset"] for w in windows.values()}}
    engine = TradingEngine(cfg, out, histories, mode="backtest")
    added: set[str] = set()
    settled: set[str] = set()
    ends = sorted((w["end_ts"], k) for k, w in windows.items())

    def ensure(key: str) -> None:
        if key in added:
            return
        r = windows[key]
        engine.add_window(Window(key, r["asset"], r["duration"], r["start_ts"], r["end_ts"],
                                 "single", r["up_slug"], None, bool(r["long_is_up"]),
                                 r["title"] or ""))
        added.add(key)

    def settle_due(now: float) -> None:
        while ends and ends[0][0] + 1 <= now:
            _, k = ends.pop(0)
            if k in added and k not in settled:
                engine.on_settled(k, windows[k]["outcome"], now)
                settled.add(k)

    last_tick = 0.0
    events = heapq.merge(((s["ts"], 0, s) for s in samples), ((t["ts"], 1, t) for t in trades),
                         key=lambda e: (e[0], e[1]))
    n = 0
    for ts, kind, row in events:
        n += 1
        key = row["window_key"]
        ensure(key)
        settle_due(ts)
        if ts - last_tick >= 1.0:
            engine.tick(ts)
            last_tick = ts
        if kind == 0:
            if row.get("ref_price") is not None:
                histories[windows[key]["asset"]].add(Tick(ts, float(row["ref_price"])))
            engine.on_book(key, _lvl(row["up_bid"], row["up_bid_qty"]),
                           _lvl(row["up_ask"], row["up_ask_qty"]), ts)
        elif row.get("price") is not None and row.get("qty"):
            p = row["price"] if windows[key]["long_is_up"] else 1 - row["price"]
            engine.on_trade(key, Decimal(str(round(p, 6))), int(row["qty"]), ts)
    final = (samples[-1]["ts"] if samples else 0.0) + 10
    engine.tick(final)
    settle_due(final)
    return {"windows": len(windows), "events": n, "settled": len(settled),
            "notes": json.dumps(sorted({ew.note for ew in engine.windows.values() if ew.note}))}
