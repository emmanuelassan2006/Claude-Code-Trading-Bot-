"""Live Chainlink price stream + price-to-beat derivation.

No Polymarket API returns the "price to beat". It is derived from the Chainlink
stream the market resolves on. polymarket.com moved 5m/15m/4h Up/Down
settlement to a Chainlink TWAP on 2026-08-07; whether Polymarket US uses the
same rule is UNVERIFIED. So `PriceHistory` exposes several candidate rules and
the monitor records all of them, then scores them against actual settlements.

Source "rtds" = polymarket.com Real-Time Data Service, which relays Chainlink
Data Streams over a public, read-only WebSocket (no credentials). It is
international infrastructure used purely as a data feed; no trading happens
there. Swap in a direct Chainlink Data Streams client later by implementing the
same `on_tick` contract.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from pmbot.config import ApiConfig, ChainlinkConfig
from pmbot.ratelimit import backoff

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tick:
    ts: float      # source timestamp, seconds
    value: float


class PriceHistory:
    """Time-ordered ticks for one symbol with candidate price-to-beat rules."""

    def __init__(self, max_age_s: float = 7200.0) -> None:
        self.max_age_s = max_age_s
        self._ts: deque[float] = deque()
        self._vals: deque[float] = deque()

    def add(self, tick: Tick) -> None:
        if self._ts and tick.ts < self._ts[-1]:
            # out-of-order: insert (rare; keeps the series sorted)
            ts, vals = list(self._ts), list(self._vals)
            i = bisect.bisect_right(ts, tick.ts)
            ts.insert(i, tick.ts)
            vals.insert(i, tick.value)
            self._ts, self._vals = deque(ts), deque(vals)
        else:
            self._ts.append(tick.ts)
            self._vals.append(tick.value)
        cutoff = tick.ts - self.max_age_s
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
            self._vals.popleft()

    def latest(self) -> Tick | None:
        return Tick(self._ts[-1], self._vals[-1]) if self._ts else None

    def at_or_after(self, t: float) -> Tick | None:
        ts = list(self._ts)
        i = bisect.bisect_left(ts, t)
        return Tick(ts[i], self._vals[i]) if i < len(ts) else None

    def at_or_before(self, t: float) -> Tick | None:
        ts = list(self._ts)
        i = bisect.bisect_right(ts, t) - 1
        return Tick(ts[i], self._vals[i]) if i >= 0 else None

    def twap(self, start: float, end: float) -> float | None:
        """Time-weighted average over [start, end], treating ticks as a step function.

        Returns None unless a price is known at `start` and data extends to `end`.
        """
        ts, vals = list(self._ts), list(self._vals)
        if end <= start or not ts or ts[-1] < end:
            return None
        i = bisect.bisect_right(ts, start) - 1
        if i < 0:
            return None
        total, t = 0.0, start
        while t < end:
            seg_end = min(ts[i + 1], end) if i + 1 < len(ts) else end
            total += vals[i] * (seg_end - t)
            t = seg_end
            i += 1
        return total / (end - start)

    def average(self, start: float, end: float) -> float | None:
        """Like twap() but carries the last known price forward past the last tick.

        Used for the already-realized part of a settlement average.
        """
        ts, vals = list(self._ts), list(self._vals)
        if end <= start or not ts:
            return None
        i = bisect.bisect_right(ts, start) - 1
        if i < 0:
            return None
        total, t = 0.0, start
        while t < end:
            seg_end = min(ts[i + 1], end) if i + 1 < len(ts) else end
            total += vals[i] * (seg_end - t)
            t = seg_end
            i += 1
        return total / (end - start)

    def series(self) -> tuple[list[float], list[float]]:
        return list(self._ts), list(self._vals)

    def candidates(self, boundary: float, twap_s: float) -> dict[str, float | None]:
        """Candidate reference prices at a window boundary (open or close)."""
        first = self.at_or_after(boundary)
        last = self.at_or_before(boundary)
        return {
            "first_tick_after": first.value if first else None,
            "last_tick_before": last.value if last else None,
            "twap_ending": self.twap(boundary - twap_s, boundary),
            "twap_starting": self.twap(boundary, boundary + twap_s),
        }


def parse_rtds_message(msg: Any, topic: str) -> list[tuple[str, Tick]]:
    """Extract (symbol, tick) pairs from an RTDS message. Tolerant of shapes."""
    out: list[tuple[str, Tick]] = []
    if not isinstance(msg, dict) or msg.get("topic") not in (topic, None):
        return out
    payload = msg.get("payload")
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):  # initial snapshot style
            sym = payload.get("symbol")
            items = [dict(d, symbol=d.get("symbol", sym)) for d in payload["data"]]
        else:
            items = [payload]
    elif isinstance(payload, list):
        items = payload
    for it in items:
        sym = it.get("symbol")
        val = it.get("value", it.get("price"))
        ts = it.get("timestamp", msg.get("timestamp"))
        if sym is None or val is None or ts is None:
            continue
        ts = float(ts)
        out.append((str(sym).lower(), Tick(ts / 1000 if ts > 1e12 else ts, float(val))))
    return out


class ChainlinkFeed:
    def __init__(self, cfg: ChainlinkConfig, api: ApiConfig,
                 on_tick: Callable[[str, Tick], None] | None = None) -> None:
        self.cfg = cfg
        self.api = api
        self.on_tick = on_tick
        self.history: dict[str, PriceHistory] = {
            asset: PriceHistory(cfg.history_s) for asset in cfg.symbols
        }
        self._sym_to_asset = {v.lower(): k for k, v in cfg.symbols.items()}
        self.connected = False
        self.last_msg_ts = 0.0

    def handle(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw or raw.strip().upper() in ("PONG", "PING"):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.last_msg_ts = time.time()
        for sym, tick in parse_rtds_message(msg, self.cfg.rtds_topic):
            asset = self._sym_to_asset.get(sym)
            if asset is None:
                continue
            self.history[asset].add(tick)
            if self.on_tick:
                self.on_tick(asset, tick)

    async def run(self, stop: asyncio.Event) -> None:
        if self.cfg.source == "none":
            log.info("chainlink feed disabled")
            return
        if self.cfg.source != "rtds":
            raise ValueError(f"unknown chainlink source {self.cfg.source!r}")
        import websockets

        attempt = 0
        while not stop.is_set():
            try:
                async with websockets.connect(self.cfg.rtds_url, ping_interval=None) as ws:
                    subs = [
                        {"topic": self.cfg.rtds_topic, "type": "*",
                         "filters": json.dumps({"symbol": s})}
                        for s in self.cfg.symbols.values()
                    ]
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                    self.connected = True
                    attempt = 0
                    log.info("chainlink feed connected (%s)", self.cfg.rtds_url)
                    pinger = asyncio.create_task(self._ping(ws, stop))
                    try:
                        async for raw in ws:
                            self.handle(raw)
                            if stop.is_set():
                                break
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("chainlink feed error: %s", e)
            self.connected = False
            if stop.is_set():
                break
            delay = backoff(attempt, self.api.reconnect_initial_s, self.api.reconnect_max_s)
            attempt += 1
            log.info("chainlink feed reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _ping(self, ws: Any, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(5)
            await ws.send("PING")
