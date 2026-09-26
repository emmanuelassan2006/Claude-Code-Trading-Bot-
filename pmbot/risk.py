"""Risk engine. Sits between the strategy and execution; strategies cannot
bypass it. All exposure is *worst-case loss at resolution* in dollars.

Position model (single netted book, Up terms): per window we hold `cash` (net
cash flow incl. fees/rebates) and `pos` (signed Up shares; negative = short
Up = long Down). At resolution the window is worth cash + pos * X, X in {0,1},
so the worst case is min(cash, cash + pos) and the exposure is its negative.
Resting orders are included as if they all fill (every subset is checked
because the loss is convex in fill amounts, so its maximum is at a vertex).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmbot.config import RiskConfig, SizingConfig

log = logging.getLogger(__name__)
ZERO = Decimal("0")


def worst_loss(cash: Decimal, pos: int, buys: list[tuple[Decimal, int]] = (),
               sells: list[tuple[Decimal, int]] = ()) -> Decimal:
    """Max loss at resolution over all-or-nothing fills of resting buys/sells."""
    bc = sum((p * q for p, q in buys), ZERO)
    bq = sum(q for _, q in buys)
    sc = sum((p * q for p, q in sells), ZERO)
    sq = sum(q for _, q in sells)
    worst = ZERO
    for fb in (0, 1):
        for fs in (0, 1):
            c = cash - fb * bc + fs * sc
            n = pos + fb * bq - fs * sq
            worst = max(worst, -min(c, c + n))
    return worst


@dataclass
class WindowRisk:
    key: str
    end_ts: float
    cash: Decimal = ZERO
    pos: int = 0
    resting: dict[str, tuple[str, Decimal, int]] = field(default_factory=dict)
    mark: Decimal | None = None
    locked: str | None = None

    def orders(self, side: str, exclude: str | None = None) -> list[tuple[Decimal, int]]:
        return [(p, q) for oid, (s, p, q) in self.resting.items() if s == side and oid != exclude]

    def exposure(self, extra: tuple[str, Decimal, int] | None = None,
                 exclude: str | None = None) -> Decimal:
        buys, sells = self.orders("buy", exclude), self.orders("sell", exclude)
        if extra:
            (buys if extra[0] == "buy" else sells).append((extra[1], extra[2]))
        return worst_loss(self.cash, self.pos, buys, sells)

    def mark_pnl(self) -> Decimal:
        m = self.mark if self.mark is not None else Decimal("0.5")
        return self.cash + self.pos * m


class RiskEngine:
    def __init__(self, cfg: RiskConfig, sizing: SizingConfig) -> None:
        self.cfg = cfg
        self.sizing = sizing
        self.windows: dict[str, WindowRisk] = {}
        self.realized_today = ZERO
        self.realized_total = ZERO
        self.day = None
        self.halted_until = 0.0
        self.halt_reason = ""
        self._orders: deque[float] = deque()
        self._cancels: deque[float] = deque()
        self._actions: deque[float] = deque()

    # ---- bookkeeping -----------------------------------------------------
    @property
    def window_budget(self) -> Decimal:
        return min(self.cfg.max_window_exposure,
                   self.sizing.base_window_budget * self.sizing.trade_scale)

    def register_window(self, key: str, end_ts: float) -> WindowRisk:
        return self.windows.setdefault(key, WindowRisk(key, end_ts))

    def total_exposure(self) -> Decimal:
        return sum((w.exposure() for w in self.windows.values()), ZERO)

    def on_order_live(self, key: str, oid: str, side: str, price: Decimal, qty: int) -> None:
        self.windows[key].resting[oid] = (side, price, qty)

    def on_order_done(self, key: str, oid: str) -> None:
        w = self.windows.get(key)
        if w:
            w.resting.pop(oid, None)

    def on_fill(self, key: str, oid: str | None, side: str, price: Decimal, qty: int,
                fee: Decimal) -> None:
        w = self.windows[key]
        if side == "buy":
            w.cash -= price * qty
            w.pos += qty
        else:
            w.cash += price * qty
            w.pos -= qty
        w.cash -= fee
        if oid and oid in w.resting:
            s, p, q = w.resting[oid]
            if q - qty > 0:
                w.resting[oid] = (s, p, q - qty)
            else:
                del w.resting[oid]

    def update_mark(self, key: str, fair: float) -> None:
        if key in self.windows:
            self.windows[key].mark = Decimal(str(round(fair, 6)))

    def on_settle(self, key: str, pnl: Decimal) -> None:
        self.realized_today += pnl
        self.realized_total += pnl
        self.windows.pop(key, None)

    def remaining_balance(self) -> Decimal:
        return self.sizing.paper_balance + self.realized_total - self.total_exposure()

    # ---- limits ----------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        d = datetime.fromtimestamp(now, timezone.utc).date()
        if d != self.day:
            if self.day is not None:
                log.info("new UTC day: daily P&L reset (was %s)", self.realized_today)
            self.day = d
            self.realized_today = ZERO
        if self.halted_until and now >= self.halted_until:
            log.warning("daily halt lifted")
            self.halted_until, self.halt_reason = 0.0, ""

    def halted(self, now: float) -> bool:
        self._roll_day(now)
        return now < self.halted_until

    def daily_pnl(self) -> Decimal:
        return self.realized_today + sum((w.mark_pnl() for w in self.windows.values()), ZERO)

    def check_pnl_locks(self, now: float) -> tuple[list[str], bool]:
        """Returns (windows newly locked, halted_now)."""
        self._roll_day(now)
        newly: list[str] = []
        for w in self.windows.values():
            if w.locked:
                continue
            pnl = w.mark_pnl()
            if self.cfg.window_take_profit > 0 and pnl >= self.cfg.window_take_profit:
                w.locked = f"take-profit {pnl:.2f}"
            elif self.cfg.window_stop_loss > 0 and pnl <= -self.cfg.window_stop_loss:
                w.locked = f"stop-loss {pnl:.2f}"
            if w.locked:
                newly.append(w.key)
        halted_now = False
        if now >= self.halted_until:
            daily = self.daily_pnl()
            reason = ""
            if daily <= -self.cfg.daily_loss_limit:
                reason = f"daily loss limit ({daily:.2f})"
            elif self.cfg.daily_take_profit > 0 and daily >= self.cfg.daily_take_profit:
                reason = f"daily take-profit ({daily:.2f})"
            if reason:
                nxt = datetime.fromtimestamp(now, timezone.utc).date() + timedelta(days=1)
                self.halted_until = datetime(nxt.year, nxt.month, nxt.day,
                                             tzinfo=timezone.utc).timestamp()
                self.halt_reason = reason
                halted_now = True
        return newly, halted_now

    def in_cutoff(self, key: str, now: float) -> bool:
        w = self.windows.get(key)
        return w is not None and now >= w.end_ts - self.cfg.entry_cutoff_s

    @staticmethod
    def _prune(dq: deque[float], now: float, horizon: float) -> None:
        while dq and dq[0] <= now - horizon:
            dq.popleft()

    def _rate_ok(self, now: float, cancel: bool = False, order: bool = False) -> str:
        self._prune(self._orders, now, 60)
        self._prune(self._cancels, now, 60)
        self._prune(self._actions, now, 1)
        if len(self._actions) >= self.cfg.max_actions_per_s:
            return "rate: actions/s"
        if order and len(self._orders) >= self.cfg.max_orders_per_min:
            return "rate: orders/min"
        if cancel and len(self._cancels) >= self.cfg.max_cancels_per_min:
            return "rate: cancels/min"
        return ""

    def can_requote(self, now: float) -> bool:
        """Strategy-driven cancel/replace (forced cancels bypass this)."""
        return not self._rate_ok(now, cancel=True, order=True)

    def note_order(self, now: float) -> None:
        self._orders.append(now)
        self._actions.append(now)

    def note_cancel(self, now: float) -> None:
        self._cancels.append(now)
        self._actions.append(now)

    def check(self, key: str, side: str, price: Decimal, qty: int, now: float,
              replacing: str | None = None) -> tuple[int, str]:
        """Largest allowed quantity (<= qty) for a new order, and a reason if cut."""
        if qty <= 0:
            return 0, "qty 0"
        if self.halted(now):
            return 0, f"halted: {self.halt_reason}"
        w = self.windows.get(key)
        if w is None:
            return 0, "unknown window"
        if w.locked:
            return 0, f"window locked: {w.locked}"
        if self.in_cutoff(key, now):
            return 0, "entry cutoff"
        rate = self._rate_ok(now, order=True)
        if rate:
            return 0, rate
        others = sum((x.exposure() for k, x in self.windows.items() if k != key), ZERO)
        total_cap = self.total_cap()
        q = qty
        while q > 0:
            wexp = w.exposure((side, price, q), exclude=replacing)
            if wexp <= self.window_budget and others + wexp <= total_cap:
                return q, "" if q == qty else f"cut {qty}->{q} by exposure caps"
            q -= 1
        return 0, "exposure caps"

    def total_cap(self) -> Decimal:
        """Total worst-case exposure allowed now.

        With daily_loss_hard_cap, open risk can never exceed what is left of the
        daily loss limit, so settlement cannot push the day past the limit.
        """
        cap = self.cfg.max_total_exposure
        if self.cfg.daily_loss_hard_cap:
            cap = min(cap, max(ZERO, self.cfg.daily_loss_limit + self.realized_today))
        return cap
