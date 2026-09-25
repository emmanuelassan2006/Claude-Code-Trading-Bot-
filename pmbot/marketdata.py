"""Read-only market data: public REST (gateway) and the markets WebSocket.

PHASE 1 SAFETY: this module exposes no order, cancel, or portfolio calls. The
API key (if present) is used only to authenticate the markets WebSocket, which
Polymarket US requires even for public book data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from pmbot.config import ApiConfig, MonitorConfig, Secrets
from pmbot.ratelimit import RateLimiter, backoff

log = logging.getLogger(__name__)

BookHandler = Callable[[str, dict[str, Any], float, str], None]
TradeHandler = Callable[[str, dict[str, Any], float], None]


class PublicAPI:
    """Unauthenticated gateway GETs, rate limited. No trading methods by design."""

    def __init__(self, cfg: ApiConfig) -> None:
        from polymarket_us import AsyncPolymarketUS

        self._client = AsyncPolymarketUS(
            gateway_base_url=cfg.gateway_url, timeout=cfg.timeout_s, max_retries=2
        )
        self.limiter = RateLimiter(cfg.rest_rate_per_s, cfg.rest_burst)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.limiter.acquire()
        return await self._client.get(path, query=params)

    async def book(self, slug: str) -> dict[str, Any]:
        return (await self.get(f"/v1/markets/{slug}/book")).get("marketData", {})

    async def market(self, slug: str) -> dict[str, Any]:
        return (await self.get(f"/v1/market/slug/{slug}")).get("market", {})

    async def settlement(self, slug: str) -> dict[str, Any]:
        return await self.get(f"/v1/markets/{slug}/settlement")

    async def close(self) -> None:
        await self._client.close()


class MarketDataStream:
    """Keeps WebSocket subscriptions in sync with the set of watched slugs.

    Falls back to REST book polling whenever the WebSocket is down or stale
    (or when no API key is configured).
    """

    def __init__(
        self,
        api_cfg: ApiConfig,
        mon_cfg: MonitorConfig,
        secrets: Secrets,
        public: PublicAPI,
        on_book: BookHandler,
        on_trade: TradeHandler,
        on_status: Callable[[str, str], None] | None = None,
    ) -> None:
        self.api_cfg = api_cfg
        self.mon_cfg = mon_cfg
        self.secrets = secrets
        self.public = public
        self.on_book = on_book
        self.on_trade = on_trade
        self.on_status = on_status or (lambda kind, detail: None)
        self.wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self.ws_healthy = False
        self._last_msg = 0.0

    def watch(self, slugs: list[str]) -> None:
        self.wanted.update(slugs)

    def unwatch(self, slugs: list[str]) -> None:
        self.wanted.difference_update(slugs)

    # ---- WebSocket -----------------------------------------------------
    def _on_message(self, msg: dict[str, Any]) -> None:
        self._last_msg = time.time()

    def _on_market_data(self, msg: dict[str, Any]) -> None:
        md = msg.get("marketData") or {}
        slug = md.get("marketSlug")
        if slug:
            self.on_book(slug, md, time.time(), "ws")

    def _on_trade(self, msg: dict[str, Any]) -> None:
        tr = msg.get("trade") or {}
        slug = tr.get("marketSlug")
        if slug:
            self.on_trade(slug, tr, time.time())

    async def _sync_subscriptions(self, ws: Any) -> None:
        for slug in sorted(self.wanted - self._subscribed):
            await ws.subscribe(f"md-{slug}", "SUBSCRIPTION_TYPE_MARKET_DATA", [slug])
            if self.mon_cfg.record_trades:
                await ws.subscribe(f"tr-{slug}", "SUBSCRIPTION_TYPE_TRADE", [slug])
            self._subscribed.add(slug)
        for slug in sorted(self._subscribed - self.wanted):
            await ws.unsubscribe(f"md-{slug}")
            if self.mon_cfg.record_trades:
                await ws.unsubscribe(f"tr-{slug}")
            self._subscribed.discard(slug)

    async def run_ws(self, stop: asyncio.Event) -> None:
        if not self.secrets.has_api_key:
            log.warning("no API key in .env: markets WebSocket needs auth; using REST polling only")
            return
        from polymarket_us.websocket import MarketsWebSocket

        url = self.api_cfg.api_url.replace("https://", "wss://").replace("http://", "ws://")
        attempt = 0
        while not stop.is_set():
            ws = MarketsWebSocket(
                key_id=self.secrets.key_id.get(),
                secret_key=self.secrets.secret_key.get(),
                base_url=url,
            )
            closed = asyncio.Event()
            ws.on("message", self._on_message)
            ws.on("market_data", self._on_market_data)
            ws.on("trade", self._on_trade)
            ws.on("close", lambda *a: closed.set())
            ws.on("error", lambda e: log.warning("markets ws error: %s", e))
            self._subscribed = set()
            try:
                await ws.connect()
                self._last_msg = time.time()
                log.info("markets websocket connected")
                self.on_status("ws_connected", "")
                attempt = 0
                while not stop.is_set() and not closed.is_set():
                    await self._sync_subscriptions(ws)
                    stale = time.time() - self._last_msg > self.mon_cfg.ws_stale_after_s
                    self.ws_healthy = not stale
                    if stale and self.wanted:
                        log.warning("markets websocket stale; reconnecting")
                        break
                    try:
                        await asyncio.wait_for(closed.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("markets websocket failed: %s", e)
            finally:
                self.ws_healthy = False
                try:
                    await ws.close()
                except Exception:
                    pass
            if stop.is_set():
                break
            self.on_status("ws_disconnected", f"attempt={attempt}")
            delay = backoff(attempt, self.api_cfg.reconnect_initial_s, self.api_cfg.reconnect_max_s)
            attempt += 1
            log.info("markets websocket reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ---- REST fallback -------------------------------------------------
    async def run_poll(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.ws_healthy:
                for slug in sorted(self.wanted):
                    try:
                        md = await self.public.book(slug)
                        self.on_book(slug, md, time.time(), "rest")
                    except Exception as e:
                        log.warning("book poll %s failed: %s", slug, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.mon_cfg.book_poll_interval_s)
            except asyncio.TimeoutError:
                pass
