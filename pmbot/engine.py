"""Trading engine: strategy -> risk -> executor -> ledger, per window.

The same engine runs paper trading on live data (`pmbot run`), replays of
recorded data (`pmbot backtest`), and will run live in Phase 3. Only the
executor differs, so logs match one-to-one. In this build the only executor
is the PaperExchange; there is no code path that can submit a real order.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pmbot.book import Level
from pmbot.config import Config
from pmbot.discovery import Window
from pmbot.feeds.history import PriceHistory
from pmbot.model import FairValue, annual_to_ps, fair_value, realized_vol_ps
from pmbot.risk import RiskEngine
from pmbot.sim import PaperExchange, SimOrder
from pmbot.store import Store
from pmbot.strategy import FairValueStrategy

log = logging.getLogger(__name__)
ZERO = Decimal("0")
PTB_RULES = ("twap_ending", "first_tick_after", "last_tick_before", "twap_starting")


@dataclass
class EngineWindow:
    window: Window
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    strikes: list[float] = field(default_factory=list)
    fv: FairValue | None = None
    resting: dict[str, str] = field(default_factory=dict)   # side -> order id
    last_requote: float = 0.0
    last_take: dict[str, float] = field(default_factory=dict)
    closed: bool = False
    cutoff_done: bool = False
    strike_fixed: bool = False        # True once the API's priceToBeat is known
    stats: dict[str, Any] = field(default_factory=lambda: defaultdict(lambda: ZERO))
    note: str = ""


class TradingEngine:
    def __init__(self, cfg: Config, store: Store, histories: dict[str, PriceHistory],
                 mode: str = "dry_run") -> None:
        if mode not in ("dry_run", "backtest"):
            raise ValueError("live execution is not built (Phase 3)")
        self.cfg = cfg
        self.store = store
        self.histories = histories
        self.mode = mode
        self.strategy = FairValueStrategy(cfg.strategy, cfg.fees, cfg.markets.tick_size)
        self.risk = RiskEngine(cfg.risk, cfg.sizing)
        self.exec = PaperExchange(cfg.fees, cfg.sim.latency_ms / 1000.0,
                                  self._on_fill, self._on_done)
        self.windows: dict[str, EngineWindow] = {}
        self._markouts: list[tuple[int, str, float, str, Decimal, int]] = []
        self._vol_cache: dict[str, tuple[float, float | None]] = {}
        self._now = 0.0

    # ---- lifecycle -------------------------------------------------------
    def add_window(self, w: Window) -> None:
        if w.structure != "single":
            log.warning("engine: %s has structure %s; only single-book windows are traded",
                        w.key, w.structure)
            return
        if w.key in self.windows:
            return
        self.windows[w.key] = EngineWindow(w)
        self.risk.register_window(w.key, w.end_ts)

    def set_strike(self, key: str, value: float) -> None:
        """Use the exchange's published price to beat instead of our estimate."""
        ew = self.windows.get(key)
        if ew is not None:
            ew.strikes = [float(value)]
            ew.strike_fixed = True

    def update_window_times(self, key: str) -> None:
        ew = self.windows.get(key)
        wr = self.risk.windows.get(key)
        if ew is not None and wr is not None:
            wr.end_ts = ew.window.end_ts

    def on_book(self, key: str, bids: list[Level], asks: list[Level], now: float) -> None:
        ew = self.windows.get(key)
        if ew is None:
            return
        ew.bids, ew.asks = bids, asks
        self._now = now
        self.exec.on_book(key, bids, asks, now)
        self.evaluate(key, now)

    def on_trade(self, key: str, price_up: Decimal, qty: int, ts: float) -> None:
        if key in self.windows:
            self.exec.on_trade(key, price_up, qty, ts)

    def on_price(self, asset: str, now: float) -> None:
        for key, ew in list(self.windows.items()):
            if ew.window.asset == asset and not ew.closed:
                self.evaluate(key, now)

    def tick(self, now: float) -> None:
        self._now = now
        self.exec.process(now)
        for key, ew in list(self.windows.items()):
            if not ew.closed and now >= ew.window.end_ts:
                self.on_window_end(key, now)
            elif not ew.closed:
                self._enforce_cutoff(ew, now)
        self._do_markouts(now)
        newly, halted = self.risk.check_pnl_locks(now)
        for key in newly:
            reason = self.risk.windows[key].locked
            log.warning("window %s locked (%s): cancelling its orders", key, reason)
            self.store.log_event("warning", "window_lock", f"{key} {reason}")
            self.pull_all(now, key=key, why="window lock")
        if halted:
            log.error("TRADING HALTED: %s (until %s)", self.risk.halt_reason,
                      self.risk.halted_until)
            self.store.log_event("error", "halt", self.risk.halt_reason)
            self.pull_all(now, why="halt")

    def on_window_end(self, key: str, now: float) -> None:
        ew = self.windows.get(key)
        if ew is None or ew.closed:
            return
        self.pull_all(now, key=key, why="window end")
        self.exec.expire_window(key, now)
        ew.closed = True

    def on_settled(self, key: str, outcome: str | None, now: float) -> None:
        """outcome 'up'/'down' from the exchange, or None to fall back to our feed."""
        ew = self.windows.get(key)
        if ew is None:
            return
        if not ew.closed:
            self.on_window_end(key, now)
        source = "exchange"
        if outcome not in ("up", "down"):
            outcome, source = self._predict_outcome(ew), "predicted"
        wr = self.risk.windows.get(key)
        cash = wr.cash if wr else ZERO
        pos = wr.pos if wr else 0
        x = 1 if outcome == "up" else 0
        pnl = cash + pos * x if outcome else cash + pos * Decimal("0.5")
        if outcome is None:
            source = "unknown(marked 0.5)"
        self.risk.on_settle(key, pnl)
        st = ew.stats
        self.store.insert("window_results", {
            "window_key": key, "asset": ew.window.asset, "duration": ew.window.duration,
            "end_ts": ew.window.end_ts, "outcome": outcome, "outcome_source": source,
            "final_pos": pos, "cash": float(cash), "pnl": float(pnl),
            "maker_fills": int(st["maker_fills"]), "taker_fills": int(st["taker_fills"]),
            "fees": float(st["fees"]), "rebates": float(st["rebates"]),
            "carried_inventory": int(pos != 0), "locked": wr.locked if wr else None,
        })
        log.info("SETTLED %s outcome=%s(%s) pos=%d cash=%.4f pnl=%.4f", key, outcome, source,
                 pos, cash, pnl)
        self._finalize_markouts(key)
        del self.windows[key]

    def _predict_outcome(self, ew: EngineWindow) -> str | None:
        h = self.histories.get(ew.window.asset)
        if h is None or not ew.strikes:
            return None
        w = float(self.cfg.strategy.settle_twap_s.get(ew.window.duration, 0))
        end = ew.window.end_ts
        close = h.twap(end - w, end) if w > 0 else None
        if close is None:
            t = h.at_or_before(end)
            close = t.value if t else None
        if close is None:
            return None
        return "up" if close >= ew.strikes[0] else "down"

    # ---- evaluation --------------------------------------------------------
    def _strikes(self, ew: EngineWindow) -> list[float]:
        h = self.histories.get(ew.window.asset)
        if h is None:
            return []
        tw = float(self.cfg.price_feed.twap_window_s.get(ew.window.duration, 30))
        cands = h.candidates(ew.window.start_ts, tw)
        primary = cands.get(self.cfg.price_feed.ptb_rule)
        if primary is None:
            return []
        others = [cands[r] for r in PTB_RULES
                  if r != self.cfg.price_feed.ptb_rule and cands.get(r) is not None]
        return [primary] + (others if self.cfg.strategy.use_all_ptb_candidates else [])

    def _sigma(self, asset: str, now: float) -> float | None:
        cached = self._vol_cache.get(asset)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        c = self.cfg.strategy
        h = self.histories.get(asset)
        sig = realized_vol_ps(h, now, c.vol_lookback_s, c.vol_sample_s, c.vol_min_returns) \
            if h else None
        if sig is not None:
            sig = min(max(sig, annual_to_ps(c.vol_floor_annual)), annual_to_ps(c.vol_cap_annual))
        self._vol_cache[asset] = (now, sig)
        return sig

    def compute_fair(self, ew: EngineWindow, now: float) -> FairValue | None:
        c = self.cfg.strategy
        w = ew.window
        h = self.histories.get(w.asset)
        if h is None:
            ew.note = "no price history"
            return None
        latest = h.latest()
        if latest is None or now - latest.ts > c.max_feed_age_s:
            ew.note = "price feed stale"
            return None
        if now < w.start_ts:
            ew.note = "window not open"
            return None
        tw = float(self.cfg.price_feed.twap_window_s.get(w.duration, 30))
        if ew.strike_fixed:
            pass
        elif len(ew.strikes) < len(PTB_RULES) and now <= w.start_ts + tw + 5:
            ew.strikes = self._strikes(ew) or ew.strikes
        elif not ew.strikes:
            ew.strikes = self._strikes(ew)
        if not ew.strikes:
            ew.note = "price to beat unknown"
            return None
        sigma = self._sigma(w.asset, now)
        if sigma is None:
            ew.note = "not enough price history for volatility"
            return None
        settle_w = float(c.settle_twap_s.get(w.duration, 0))
        tau = w.end_ts - now
        realized = None
        if settle_w > 0 and tau < settle_w:
            realized = h.average(w.end_ts - settle_w, now)
        return fair_value(latest.value, ew.strikes, sigma, tau, settle_w, realized,
                          c.vol_uncertainty, c.price_uncertainty_bps, c.quote_horizon_s)

    def _enforce_cutoff(self, ew: EngineWindow, now: float) -> bool:
        if self.risk.in_cutoff(ew.window.key, now):
            if not ew.cutoff_done:
                self.pull_all(now, key=ew.window.key, why="entry cutoff")
                ew.cutoff_done = True
                wr = self.risk.windows.get(ew.window.key)
                if wr and wr.pos != 0:
                    log.info("CARRY %s: holding %d Up shares to resolution", ew.window.key,
                             wr.pos)
            return True
        return False

    def evaluate(self, key: str, now: float) -> None:
        ew = self.windows.get(key)
        if ew is None or ew.closed:
            return
        if self._enforce_cutoff(ew, now):
            return
        wr = self.risk.windows[key]
        if self.risk.halted(now) or wr.locked:
            if ew.resting:
                self.pull_all(now, key=key, why="halted/locked")
            return
        fv = self.compute_fair(ew, now)
        ew.fv = fv
        if fv is None:
            if ew.resting:
                self.pull_all(now, key=key, why=ew.note)
            return
        self.risk.update_mark(key, fv.p)
        scale = self.cfg.sizing.trade_scale
        d = self.strategy.decide(fv, ew.bids, ew.asks, wr.pos, ew.window.end_ts - now,
                                 now - ew.window.start_ts, ew.window.duration, scale)
        for t in d.takes:
            if now - ew.last_take.get(t.side, -1e18) < self.cfg.strategy.taker_cooldown_s:
                continue
            q, why = self.risk.check(key, t.side, t.limit_price, t.qty, now)
            if q <= 0:
                log.debug("take %s blocked: %s", t.side, why)
                continue
            ew.last_take[t.side] = now
            self._send(ew, "taker", t.side, t.limit_price, q, t.exp_edge_ps, fv, now, why)
        for side in ("buy", "sell"):
            want = d.quotes.get(side)
            oid = ew.resting.get(side)
            o = self.exec.sim_orders.get(oid) if oid else None
            if o is not None and (o.status not in ("pending", "live") or o.cancel_at):
                ew.resting.pop(side, None)
                o = None
            if want is None:
                if o is not None:
                    self._cancel(o, now, "no quote wanted")
                continue
            if o is not None:
                sc = self.cfg.strategy
                ref = Decimal(str(fv.low if side == "buy" else fv.high))
                edge_now = ref - o.price if side == "buy" else o.price - ref
                edge_want = ref - want.price if side == "buy" else want.price - ref
                if edge_now >= sc.keep_min_edge:
                    step = self.cfg.markets.tick_size * sc.requote_improve_ticks
                    if edge_now - edge_want < step \
                            or now - ew.last_requote < sc.requote_min_interval_s \
                            or not self.risk.can_requote(now):
                        continue          # still safe and not worth moving
                elif not self.risk.can_requote(now):
                    self._cancel(o, now, "pull: edge below floor (rate-limited)")
                    continue
            q, why = self.risk.check(key, side, want.price, want.qty, now,
                                     replacing=o.id if o else None)
            if q <= 0:
                if o is not None and why.startswith(("entry", "halted", "window locked")):
                    self._cancel(o, now, why)
                continue
            if o is not None:
                self._cancel(o, now, "requote")
                ew.last_requote = now
            new = self._send(ew, "maker", side, want.price, q, want.exp_edge_ps, fv, now, why)
            ew.resting[side] = new.id

    # ---- order plumbing ------------------------------------------------------
    def _send(self, ew: EngineWindow, kind: str, side: str, price: Decimal, qty: int,
              exp_edge_ps: Decimal, fv: FairValue, now: float, note: str) -> SimOrder:
        key = ew.window.key
        wr = self.risk.windows[key]
        stake = price * qty if side == "buy" else (1 - price) * qty
        budget_left = self.risk.window_budget - wr.exposure()
        log.info(
            "SIZING %s strategy=%s.%s side=%s px=%s qty=%d stake=$%.2f exp_edge=$%.3f "
            "window_budget_left=$%.2f balance_left=$%.2f fair=%.4f%s",
            key, self.strategy.name, kind, side, price, qty, stake, exp_edge_ps * qty,
            budget_left, self.risk.remaining_balance(), fv.p, f" ({note})" if note else "")
        o = self.exec.place(key, side, price, qty, kind, now)
        self.risk.note_order(now)
        self.risk.on_order_live(key, o.id, side, price, qty)
        self.store.insert("orders", {
            "id": o.id, "window_key": key, "strategy": self.strategy.name, "kind": kind,
            "side": side, "price": float(price), "qty": qty, "status": "sent",
            "reason": note or None, "fair": fv.p, "exp_edge_ps": float(exp_edge_ps),
            "signal_ts": now, "sent_ts": now, "ack_ts": o.live_ts,
        })
        return o

    def _cancel(self, o: SimOrder, now: float, why: str) -> None:
        if o.cancel_at is not None or o.status not in ("pending", "live"):
            return
        self.exec.cancel(o.id, now)
        self.risk.note_cancel(now)
        self.store.insert("order_events", {"ts": now, "order_id": o.id, "event": "cancel",
                                           "detail": why})

    def pull_all(self, now: float, key: str | None = None, why: str = "") -> None:
        for k, ew in self.windows.items():
            if key is not None and k != key:
                continue
            for o in self.exec.open_orders(k):
                self._cancel(o, now, why)
            ew.resting.clear()

    def _on_fill(self, o: SimOrder, price: Decimal, qty: int, is_maker: bool, fee: Decimal,
                 ts: float) -> None:
        ew = self.windows.get(o.window_key)
        self.risk.on_fill(o.window_key, o.id, o.side, price, qty, fee)
        fair = ew.fv.p if ew and ew.fv else None
        fid = self.store.insert("fills", {
            "ts": ts, "order_id": o.id, "window_key": o.window_key,
            "strategy": self.strategy.name, "kind": o.kind, "side": o.side,
            "price": float(price), "qty": qty, "fee": float(fee), "fair_at_fill": fair,
        })
        self.store.update("orders", "id=?", [o.id], filled=o.filled)
        if ew:
            st = ew.stats
            st["maker_fills" if is_maker else "taker_fills"] += 1
            if fee >= 0:
                st["fees"] += fee
            else:
                st["rebates"] += -fee
        for i, _ in enumerate(self.cfg.sim.markout_s):
            self._markouts.append((fid, o.window_key, ts, o.side, price, i))
        wr = self.risk.windows.get(o.window_key)
        log.info("FILL %s %s %s %d @ %s fee=%s pos=%s", o.window_key, o.kind, o.side, qty,
                 price, fee, wr.pos if wr else "?")

    def _on_done(self, o: SimOrder, why: str) -> None:
        self.risk.on_order_done(o.window_key, o.id)
        ew = self.windows.get(o.window_key)
        if ew and ew.resting.get(o.side) == o.id:
            ew.resting.pop(o.side, None)
        self.store.update("orders", "id=?", [o.id], status=o.status, done_ts=self._now,
                          filled=o.filled)
        if o.status == "rejected":
            self.store.insert("order_events", {"ts": self._now, "order_id": o.id,
                                               "event": "reject", "detail": why})

    def _do_markouts(self, now: float) -> None:
        keep = []
        for fid, key, ts, side, price, i in self._markouts:
            ew = self.windows.get(key)
            if now < ts + self.cfg.sim.markout_s[i] or ew is None or ew.fv is None:
                keep.append((fid, key, ts, side, price, i))
                continue
            self._write_markout(fid, side, price, i, ew.fv.p)
        self._markouts = keep

    def _write_markout(self, fid: int, side: str, price: Decimal, i: int, fair: float) -> None:
        m = fair - float(price) if side == "buy" else float(price) - fair
        self.store.update("fills", "id=?", [fid], **{f"markout_{i + 1}": m})

    def _finalize_markouts(self, key: str) -> None:
        ew = self.windows.get(key)
        fair = ew.fv.p if ew and ew.fv else None
        keep = []
        for fid, k, ts, side, price, i in self._markouts:
            if k != key:
                keep.append((fid, k, ts, side, price, i))
            elif fair is not None:
                self._write_markout(fid, side, price, i, fair)
        self._markouts = keep

    def shutdown(self, now: float) -> None:
        self.pull_all(now, why="shutdown")
