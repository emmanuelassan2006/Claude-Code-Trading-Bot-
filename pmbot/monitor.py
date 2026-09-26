"""Phase 1 monitor: discover windows, keep live books, measure opportunities.

Records, per window:
  * taker-gap episodes: while ask_up + ask_down + taker fees < 1 - min_gap,
    with max gap, executable size/profit, depth on both sides, and duration;
  * throttled book samples (bids/asks both sides, taker cost, maker edge,
    Chainlink price, price to beat) for maker fill-rate estimation;
  * the public trade tape;
  * candidate price-to-beat values at open and close, and the settlement, so
    the report can tell which reference rule Polymarket US actually uses.

No trading endpoints are used anywhere in Phase 1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmbot.book import OrderBook, WindowBooks
from pmbot.config import Config, Secrets
from pmbot.discovery import Discoverer, Window
from pmbot.edge import maker_pair_edge, quote_taker_pair
from pmbot.feeds.chainlink import ChainlinkFeed
from pmbot.store import Store

log = logging.getLogger(__name__)


def _f(x: Decimal | None) -> float | None:
    return float(x) if x is not None else None


@dataclass
class GapEpisode:
    start_ts: float
    secs_to_close: float
    depth_up: float
    depth_down: float
    max_gap: Decimal = Decimal("-1")
    max_gap_ts: float = 0.0
    top_size: Decimal = Decimal("0")
    exec_size: Decimal = Decimal("0")
    exec_profit: Decimal = Decimal("0")
    updates: int = 0


@dataclass
class WindowState:
    window: Window
    books: WindowBooks
    gap: GapEpisode | None = None
    last_sample: float = 0.0
    open_ref: dict[str, float | None] | None = None
    close_ref: dict[str, float | None] | None = None
    closed: bool = False
    settled: bool = False
    next_settle_check: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ptb(self) -> float | None:
        return None if self.open_ref is None else self.extra.get("ptb")


def outcome_from_settlement(value: Any, long_is_up: bool) -> str | None:
    """Map a YES-market settlement to 'up'/'down' when it is a clean 0/1."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v not in (0.0, 1.0):
        return None
    yes_won = v == 1.0
    return "up" if yes_won == long_is_up else "down"


class Monitor:
    def __init__(self, cfg: Config, secrets: Secrets, store: Store, public: Any,
                 stream: Any = None, feed: ChainlinkFeed | None = None) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.store = store
        self.public = public
        self.stream = stream
        self.feed = feed
        self.discoverer = Discoverer(cfg.markets, public.get)
        self.engine: Any = None  # optional TradingEngine (paper trading)
        self.windows: dict[str, WindowState] = {}
        self.by_slug: dict[str, tuple[WindowState, OrderBook]] = {}

    # ---- window lifecycle ----------------------------------------------
    def add_window(self, w: Window) -> WindowState:
        up = OrderBook(w.up_slug)
        down = OrderBook(w.down_slug) if w.down_slug else None
        st = WindowState(w, WindowBooks(w.structure, up, down, w.long_is_up))
        self.windows[w.key] = st
        self.by_slug[w.up_slug] = (st, up)
        if down is not None and w.down_slug:
            self.by_slug[w.down_slug] = (st, down)
        self.store.upsert_window({
            "key": w.key, "asset": w.asset, "duration": w.duration,
            "start_ts": w.start_ts, "end_ts": w.end_ts, "structure": w.structure,
            "up_slug": w.up_slug, "down_slug": w.down_slug,
            "long_is_up": int(w.long_is_up), "title": w.title,
        })
        log.info("window %s %s %s structure=%s up=%s down=%s", w.asset, w.duration, w.key,
                 w.structure, w.up_slug, w.down_slug)
        if self.stream is not None:
            self.stream.watch(w.slugs)
        if self.engine is not None:
            self.engine.add_window(w)
        return st

    async def fetch_description(self, st: WindowState) -> None:
        try:
            m = await self.public.market(st.window.up_slug)
            desc = m.get("description")
            if desc:
                self.store.update_window(st.window.key, description=desc)
        except Exception as e:
            log.warning("market detail %s failed: %s", st.window.up_slug, e)

    def _refs(self, st: WindowState, boundary: float) -> dict[str, float | None] | None:
        if self.feed is None:
            return None
        hist = self.feed.history.get(st.window.asset)
        if hist is None:
            return None
        tw = float(self.cfg.chainlink.twap_window_s.get(st.window.duration, 30))
        return hist.candidates(boundary, tw)

    def tick_lifecycle(self, now: float) -> None:
        tw_max = max(self.cfg.chainlink.twap_window_s.values(), default=30)
        for st in list(self.windows.values()):
            w = st.window
            if st.open_ref is None and now >= w.start_ts + tw_max + 2:
                st.open_ref = self._refs(st, w.start_ts) or {}
                st.extra["ptb"] = st.open_ref.get(self.cfg.chainlink.ptb_rule) or \
                    st.open_ref.get("first_tick_after")
                self.store.update_window(w.key, open_ref=st.open_ref)
            if not st.closed and now >= w.end_ts:
                self._close_gap(st, now, "window_end")
                self.evaluate(st, now, force_sample=True)
                st.closed = True
                st.next_settle_check = w.end_ts + 15
            if st.closed and st.close_ref is None and now >= w.end_ts + tw_max + 2:
                st.close_ref = self._refs(st, w.end_ts) or {}
                self.store.update_window(w.key, close_ref=st.close_ref)
            if st.closed and now >= w.end_ts + self.cfg.markets.post_close_grace_s:
                if self.stream is not None:
                    self.stream.unwatch(w.slugs)
            if st.settled or now > w.end_ts + self.cfg.monitor.settlement_poll_s:
                if st.close_ref is not None:
                    self._forget(st, now)

    def _forget(self, st: WindowState, now: float) -> None:
        if self.engine is not None and st.window.key in self.engine.windows:
            self.engine.on_settled(st.window.key, None, now)  # settlement never arrived
        self.store.update_window(st.window.key, finalized_at=now)
        self.windows.pop(st.window.key, None)
        for s in st.window.slugs:
            self.by_slug.pop(s, None)

    async def check_settlements(self, now: float) -> None:
        for st in list(self.windows.values()):
            if not st.closed or st.settled or now < st.next_settle_check:
                continue
            st.next_settle_check = now + 30
            try:
                res = await self.public.settlement(st.window.up_slug)
            except Exception as e:
                log.debug("settlement %s not ready: %s", st.window.up_slug, e)
                continue
            value = res.get("settlement") if isinstance(res, dict) else None
            if value is None:
                continue
            outcome = outcome_from_settlement(value, st.window.long_is_up)
            st.settled = True
            self.store.update_window(st.window.key, settlement=float(value), outcome=outcome,
                                     settled_at=now)
            log.info("settled %s value=%s outcome=%s", st.window.key, value, outcome)
            if self.engine is not None:
                self.engine.on_settled(st.window.key, outcome, now)

    # ---- market data -----------------------------------------------------
    def on_book(self, slug: str, md: dict[str, Any], recv_ts: float, source: str) -> None:
        entry = self.by_slug.get(slug)
        if entry is None:
            return
        st, book = entry
        book.apply_snapshot(md, recv_ts)
        self.evaluate(st, recv_ts)
        if self.engine is not None and st.books.ready():
            self.engine.on_book(st.window.key, st.books.up_bids(), st.books.up_asks(), recv_ts)

    def on_trade(self, slug: str, tr: dict[str, Any], recv_ts: float) -> None:
        entry = self.by_slug.get(slug)
        if entry is None:
            return
        st = entry[0]
        from pmbot.discovery import parse_ts

        def amt(x: Any) -> float | None:
            if isinstance(x, dict):
                x = x.get("value")
            return float(x) if x is not None else None

        ts = parse_ts(tr.get("tradeTime")) or recv_ts
        maker, taker = tr.get("maker") or {}, tr.get("taker") or {}
        self.store.add_trade({
            "ts": ts, "recv_ts": recv_ts, "window_key": st.window.key, "slug": slug,
            "price": amt(tr.get("price")), "qty": amt(tr.get("quantity")),
            "maker_side": maker.get("side"), "maker_intent": maker.get("intent"),
            "taker_side": taker.get("side"), "taker_intent": taker.get("intent"),
            "secs_into_window": ts - st.window.start_ts,
        })
        price, qty = amt(tr.get("price")), amt(tr.get("quantity"))
        if self.engine is not None and price is not None and qty:
            from decimal import Decimal as _D

            up_price = price if (st.window.structure == "pair" or st.window.long_is_up) \
                else 1 - price
            self.engine.on_trade(st.window.key, _D(str(round(up_price, 6))), int(qty), ts)

    def evaluate(self, st: WindowState, now: float, force_sample: bool = False) -> None:
        """Runs on every book update."""
        wb = st.books
        if not wb.ready() or st.closed:
            return
        mcfg = self.cfg.monitor
        up_asks, down_asks = wb.up_asks(), wb.down_asks()
        q = quote_taker_pair(up_asks, down_asks, self.cfg.fees, mcfg.min_gap,
                             mcfg.max_depth_levels)
        secs_to_close = st.window.end_ts - now
        in_window = now >= st.window.start_ts

        if q.top_gap is not None and q.top_gap > mcfg.min_gap and in_window:
            if st.gap is None:
                st.gap = GapEpisode(now, secs_to_close, float(up_asks[0].qty),
                                    float(down_asks[0].qty))
                log.info("GAP open %s gap=%.4f top_size=%s", st.window.key, q.top_gap, q.top_size)
            g = st.gap
            g.updates += 1
            if q.top_gap > g.max_gap:
                g.max_gap, g.max_gap_ts = q.top_gap, now
                g.top_size, g.exec_size, g.exec_profit = q.top_size, q.size, q.profit
        elif st.gap is not None:
            self._close_gap(st, now, "book")

        if force_sample or now - st.last_sample >= mcfg.spread_sample_interval_s:
            st.last_sample = now
            ub, ua = wb.up_bids(), up_asks
            db, da = wb.down_bids(), down_asks
            top = lambda lv: lv[0] if lv else None  # noqa: E731
            ubt, uat, dbt, dat = top(ub), top(ua), top(db), top(da)
            price = None
            if self.feed is not None and st.window.asset in self.feed.history:
                t = self.feed.history[st.window.asset].latest()
                price = t.value if t else None
            self.store.add_sample({
                "ts": now, "window_key": st.window.key, "secs_to_close": secs_to_close,
                "up_bid": _f(ubt.price if ubt else None), "up_bid_qty": _f(ubt.qty if ubt else None),
                "up_ask": _f(uat.price if uat else None), "up_ask_qty": _f(uat.qty if uat else None),
                "down_bid": _f(dbt.price if dbt else None),
                "down_bid_qty": _f(dbt.qty if dbt else None),
                "down_ask": _f(dat.price if dat else None),
                "down_ask_qty": _f(dat.qty if dat else None),
                "taker_cost": _f(q.top_cost),
                "maker_edge": _f(maker_pair_edge(ubt.price if ubt else None,
                                                 dbt.price if dbt else None)),
                "chainlink": price, "ptb": st.ptb,
            })

    def _close_gap(self, st: WindowState, now: float, ended_by: str) -> None:
        g = st.gap
        if g is None:
            return
        st.gap = None
        self.store.add_gap({
            "window_key": st.window.key, "asset": st.window.asset,
            "duration": st.window.duration, "start_ts": g.start_ts, "end_ts": now,
            "duration_ms": (now - g.start_ts) * 1000, "secs_to_close_at_start": g.secs_to_close,
            "max_gap": float(g.max_gap), "max_gap_ts": g.max_gap_ts,
            "top_size_at_max": float(g.top_size), "exec_size_at_max": float(g.exec_size),
            "exec_profit_at_max": float(g.exec_profit),
            "depth_up_at_start": g.depth_up, "depth_down_at_start": g.depth_down,
            "updates": g.updates, "ended_by": ended_by,
        })
        log.info("GAP close %s max_gap=%.4f lasted=%.0fms exec_size=%s profit=%s",
                 st.window.key, g.max_gap, (now - g.start_ts) * 1000, g.exec_size, g.exec_profit)

    # ---- main loops ------------------------------------------------------
    async def discovery_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                for w in await self.discoverer.poll():
                    st = self.add_window(w)
                    await self.fetch_description(st)
            except Exception as e:
                log.exception("discovery failed: %s", e)
                self.store.log_event("error", "discovery", str(e))
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.markets.discovery_interval_s)
            except asyncio.TimeoutError:
                pass

    async def lifecycle_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = time.time()
            try:
                self.tick_lifecycle(now)
                if self.engine is not None:
                    self.engine.tick(now)
                await self.check_settlements(now)
            except Exception as e:
                log.exception("lifecycle error: %s", e)
                self.store.log_event("error", "lifecycle", str(e))
            if Path(self.cfg.paths.kill_file).exists():
                log.warning("kill file %s present: stopping", self.cfg.paths.kill_file)
                if self.engine is not None:
                    self.engine.pull_all(time.time(), why="kill switch")
                self.store.log_event("warning", "kill_switch", "kill file present")
                stop.set()
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def shutdown(self) -> None:
        now = time.time()
        if self.engine is not None:
            self.engine.shutdown(now)
        for st in self.windows.values():
            self._close_gap(st, now, "shutdown")
