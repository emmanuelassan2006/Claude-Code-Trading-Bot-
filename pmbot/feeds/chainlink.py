"""Optional price source: Chainlink streams via the polymarket.com RTDS relay.

NOTE: Polymarket US crypto Up/Down markets settle on CF Benchmarks' BRTI, not
Chainlink (confirmed from market descriptions, 2026-09-26). The default source
is now pmbot/feeds/exchanges.py; this relay is kept as an alternative.

Original notes:

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
import json
import logging
import time
from typing import Any, Callable

from pmbot.config import ApiConfig, PriceFeedConfig
from pmbot.feeds.history import PriceHistory, Tick  # noqa: F401  (re-exported)
from pmbot.ratelimit import backoff

log = logging.getLogger(__name__)


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
    def __init__(self, cfg: PriceFeedConfig, api: ApiConfig,
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
