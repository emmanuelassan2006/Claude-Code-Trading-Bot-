"""BRTI proxy: live composite BTC/USD from Coinbase and Kraken order-book mids.

Polymarket US Up/Down windows settle on CF Benchmarks' Bitcoin Real-Time Index
(BRTI), which is computed from the order books of major USD exchanges. BRTI
itself is not freely streamed, so we use the median of the best bid/ask mids
of Coinbase and Kraken (both public WebSockets, no keys, US-accessible) as a
proxy. The remaining basis is covered by `price_uncertainty_bps` in the
fair-value band, and the monitor measures it against the API's priceToBeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from typing import Any, Callable

from pmbot.config import ApiConfig, PriceFeedConfig
from pmbot.feeds.history import PriceHistory, Tick
from pmbot.ratelimit import backoff

log = logging.getLogger(__name__)


def parse_coinbase(msg: dict[str, Any]) -> tuple[str, float] | None:
    """Coinbase Exchange `ticker` message -> (product_id, mid)."""
    if msg.get("type") != "ticker":
        return None
    try:
        bid, ask = float(msg["best_bid"]), float(msg["best_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    return str(msg.get("product_id")), (bid + ask) / 2


def parse_kraken(msg: dict[str, Any]) -> list[tuple[str, float]]:
    """Kraken WS v2 `ticker` snapshot/update -> [(symbol, mid)]."""
    if msg.get("channel") != "ticker" or msg.get("type") not in ("snapshot", "update"):
        return []
    out = []
    for d in msg.get("data") or []:
        try:
            bid, ask = float(d["bid"]), float(d["ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid > 0 and ask > 0:
            out.append((str(d.get("symbol")), (bid + ask) / 2))
    return out


class ExchangeFeed:
    def __init__(self, cfg: PriceFeedConfig, api: ApiConfig,
                 on_tick: Callable[[str, Tick], None] | None = None) -> None:
        self.cfg = cfg
        self.api = api
        self.on_tick = on_tick
        assets = set(cfg.coinbase_products) | set(cfg.kraken_symbols)
        self.history: dict[str, PriceHistory] = {a: PriceHistory(cfg.history_s) for a in assets}
        self._cb = {v: k for k, v in cfg.coinbase_products.items()}
        self._kr = {v: k for k, v in cfg.kraken_symbols.items()}
        self.last: dict[str, dict[str, tuple[float, float]]] = {a: {} for a in assets}
        self._last_emit: dict[str, float] = {}
        self.connected: dict[str, bool] = {"coinbase": False, "kraken": False}

    def update(self, asset: str, source: str, mid: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.last[asset][source] = (now, mid)
        if now - self._last_emit.get(asset, 0.0) < self.cfg.sample_interval_s:
            return
        fresh = [m for ts, m in self.last[asset].values() if now - ts <= self.cfg.source_stale_s]
        if not fresh:
            return
        tick = Tick(now, statistics.median(fresh))
        self.history[asset].add(tick)
        self._last_emit[asset] = now
        if self.on_tick:
            self.on_tick(asset, tick)

    def handle_coinbase(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        parsed = parse_coinbase(msg) if isinstance(msg, dict) else None
        if parsed and parsed[0] in self._cb:
            self.update(self._cb[parsed[0]], "coinbase", parsed[1])

    def handle_kraken(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        for sym, mid in parse_kraken(msg):
            if sym in self._kr:
                self.update(self._kr[sym], "kraken", mid)

    async def _stream(self, name: str, url: str, sub: dict[str, Any],
                      handler: Callable[[Any], None], stop: asyncio.Event) -> None:
        import websockets

        attempt = 0
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    await ws.send(json.dumps(sub))
                    self.connected[name] = True
                    attempt = 0
                    log.info("price feed %s connected", name)
                    async for raw in ws:
                        handler(raw)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("price feed %s error: %s", name, e)
            self.connected[name] = False
            if stop.is_set():
                break
            delay = backoff(attempt, self.api.reconnect_initial_s, self.api.reconnect_max_s)
            attempt += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        if self.cfg.source == "none":
            log.info("price feed disabled")
            return
        tasks = []
        if self.cfg.coinbase_products:
            tasks.append(self._stream(
                "coinbase", self.cfg.coinbase_url,
                {"type": "subscribe", "product_ids": list(self.cfg.coinbase_products.values()),
                 "channels": ["ticker"]},
                self.handle_coinbase, stop))
        if self.cfg.kraken_symbols:
            tasks.append(self._stream(
                "kraken", self.cfg.kraken_url,
                {"method": "subscribe",
                 "params": {"channel": "ticker", "symbol": list(self.cfg.kraken_symbols.values())}},
                self.handle_kraken, stop))
        await asyncio.gather(*tasks)
