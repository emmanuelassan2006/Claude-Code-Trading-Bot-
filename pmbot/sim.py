"""Paper exchange used by dry-run and backtest. Same interface a live
executor will implement in Phase 3; only final submission differs.

Conservative fill rules:
  * Orders and cancels take effect after `latency_s`. A resting order can
    still fill while its cancel is in flight.
  * Post-only orders that would cross the book on arrival are rejected.
  * A resting maker order fills ONLY when a public trade prints strictly
    through its price (buy: trade < bid; sell: trade > ask). Prints at our
    price are assumed to fill the queue ahead of us. Fill size is capped by the
    trade's size.
  * IOC taker orders walk the real book (at the first book update after the
    latency) up to their limit price; any remainder is cancelled.
All prices are in Up terms.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from pmbot.book import Level
from pmbot.config import FeeConfig
from pmbot.fees import fill_cost, taker_fees_for_fills

FillCallback = Callable[["SimOrder", Decimal, int, bool, Decimal, float], None]


@dataclass
class SimOrder:
    id: str
    window_key: str
    side: str            # buy | sell
    price: Decimal
    qty: int
    kind: str            # maker | taker
    placed_ts: float
    live_ts: float
    filled: int = 0
    status: str = "pending"      # pending | live | filled | canceled | rejected
    cancel_at: float | None = None

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


class PaperExchange:
    def __init__(self, fees: FeeConfig, latency_s: float, on_fill: FillCallback,
                 on_done: Callable[[SimOrder, str], None] | None = None) -> None:
        self.fees = fees
        self.latency_s = latency_s
        self.on_fill = on_fill
        self.on_done = on_done or (lambda o, why: None)
        self.sim_orders: dict[str, SimOrder] = {}   # every order (for lookup)
        self._open: dict[str, SimOrder] = {}        # pending/live only (hot path)
        self._ids = itertools.count(1)
        self.books: dict[str, tuple[list[Level], list[Level]]] = {}

    def open_orders(self, window_key: str) -> list[SimOrder]:
        return [o for o in self._open.values() if o.window_key == window_key]

    def place(self, window_key: str, side: str, price: Decimal, qty: int, kind: str,
              now: float) -> SimOrder:
        o = SimOrder(f"sim-{next(self._ids)}", window_key, side, price, qty, kind, now,
                     now + self.latency_s)
        self.sim_orders[o.id] = o
        self._open[o.id] = o
        return o

    def cancel(self, order_id: str, now: float) -> None:
        o = self.sim_orders.get(order_id)
        if o and o.status in ("pending", "live") and o.cancel_at is None:
            o.cancel_at = now + self.latency_s

    def _finish(self, o: SimOrder, status: str, why: str) -> None:
        o.status = status
        self._open.pop(o.id, None)
        self.on_done(o, why)

    def _activate(self, now: float) -> None:
        for o in list(self._open.values()):
            if o.status == "pending" and o.cancel_at is not None and o.cancel_at <= o.live_ts \
                    and now >= o.cancel_at:
                self._finish(o, "canceled", "canceled before live")
            elif o.status == "live" and o.cancel_at is not None and now >= o.cancel_at:
                self._finish(o, "canceled", "canceled")
            elif o.status == "pending" and now >= o.live_ts and o.kind == "maker":
                bids, asks = self.books.get(o.window_key, ([], []))
                crosses = (o.side == "buy" and asks and o.price >= asks[0].price) or \
                          (o.side == "sell" and bids and o.price <= bids[0].price)
                if crosses:
                    self._finish(o, "rejected", "post-only would cross")
                else:
                    o.status = "live"

    def on_book(self, window_key: str, bids: list[Level], asks: list[Level], now: float) -> None:
        self.books[window_key] = (bids, asks)
        self.process(now)

    def process(self, now: float) -> None:
        self._activate(now)
        for o in list(self._open.values()):
            if o.kind != "taker" or o.status != "pending" or now < o.live_ts:
                continue
            bids, asks = self.books.get(o.window_key, ([], []))
            ladder = asks if o.side == "buy" else bids
            fills: list[tuple[int, Decimal]] = []
            left = o.remaining
            for lvl in ladder:
                ok = lvl.price <= o.price if o.side == "buy" else lvl.price >= o.price
                if not ok or left <= 0:
                    break
                q = min(left, int(lvl.qty))
                if q > 0:
                    fills.append((q, lvl.price))
                    left -= q
            fees = taker_fees_for_fills(fills, self.fees)
            for (q, p), fee in zip(fills, fees):
                o.filled += q
                self.on_fill(o, p, q, False, fee, now)
            self._finish(o, "filled" if o.remaining == 0 else "canceled",
                         "filled" if o.remaining == 0 else "IOC remainder canceled")

    def on_trade(self, window_key: str, price: Decimal, qty: int, ts: float) -> None:
        self._activate(ts)
        left = qty
        live = sorted((o for o in self._open.values()
                       if o.window_key == window_key and o.kind == "maker"
                       and o.status == "live" and ts >= o.live_ts
                       and (o.cancel_at is None or ts < o.cancel_at)),
                      key=lambda o: o.live_ts)
        for o in live:
            if left <= 0:
                break
            through = price < o.price if o.side == "buy" else price > o.price
            if not through:
                continue
            q = min(o.remaining, left)
            left -= q
            o.filled += q
            fee = fill_cost(q, o.price, True, self.fees)
            self.on_fill(o, o.price, q, True, fee, ts)
            if o.remaining == 0:
                self._finish(o, "filled", "filled")

    def expire_window(self, window_key: str, now: float) -> None:
        for o in self.open_orders(window_key):
            self._finish(o, "canceled", "window closed")
