"""Fair-value market making with opportunistic taking, for single-book
Up/Down windows. Everything here is expressed in "Up" terms: buy = buy Up
(long YES), sell = sell Up (= buy Down / short YES).

The strategy only *proposes*. The risk engine (pmbot/risk.py) decides what
may actually be sent, and it cannot be overridden from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pmbot.book import Level
from pmbot.config import FeeConfig, StrategyConfig
from pmbot.fees import exact_maker_rebate, taker_fee_per_share
from pmbot.model import FairValue

ZERO = Decimal("0")


@dataclass(frozen=True)
class Quote:
    side: str          # "buy" | "sell"
    price: Decimal
    qty: int
    exp_edge_ps: Decimal   # expected edge per share vs fair (incl. rebate)


@dataclass(frozen=True)
class Take:
    side: str
    limit_price: Decimal
    qty: int
    exp_edge_ps: Decimal   # after taker fee


@dataclass
class Decision:
    quotes: dict[str, Quote | None]
    takes: list[Take]
    note: str = ""


def _round(p: float | Decimal, tick: Decimal, down: bool) -> Decimal:
    q = (Decimal(str(p)) / tick).to_integral_value(rounding=ROUND_FLOOR if down else ROUND_CEILING)
    return q * tick


class FairValueStrategy:
    name = "fv"

    def __init__(self, cfg: StrategyConfig, fees: FeeConfig, tick: Decimal) -> None:
        self.cfg = cfg
        self.fees = fees
        self.tick = tick

    def decide(
        self,
        fv: FairValue,
        bids: list[Level],
        asks: list[Level],
        position: int,
        secs_to_close: float,
        secs_since_open: float,
        duration: str,
        scale: Decimal,
    ) -> Decision:
        c = self.cfg
        quotes: dict[str, Quote | None] = {"buy": None, "sell": None}
        takes: list[Take] = []
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        low, high = Decimal(str(fv.low)), Decimal(str(fv.high))

        # ---- taker: only when the book is clearly through our band --------
        if c.taker_enabled:
            max_q = int(Decimal(c.taker_max_shares) * scale)
            buy_q, buy_px, buy_edge = 0, ZERO, ZERO
            for lvl in asks:
                p = lvl.price
                if not (c.taker_price_min <= p <= c.taker_price_max):
                    break
                edge = low - p - taker_fee_per_share(p, self.fees)
                if edge < c.taker_min_edge or buy_q >= max_q:
                    break
                q = min(int(lvl.qty), max_q - buy_q)
                if q <= 0:
                    break
                buy_edge = edge if buy_q == 0 else min(buy_edge, edge)
                buy_q += q
                buy_px = p
            if buy_q > 0:
                takes.append(Take("buy", buy_px, buy_q, buy_edge))
            sell_q, sell_px, sell_edge = 0, ZERO, ZERO
            for lvl in bids:
                p = lvl.price
                if not (c.taker_price_min <= p <= c.taker_price_max):
                    break
                edge = p - high - taker_fee_per_share(p, self.fees)
                if edge < c.taker_min_edge or sell_q >= max_q:
                    break
                q = min(int(lvl.qty), max_q - sell_q)
                if q <= 0:
                    break
                sell_edge = edge if sell_q == 0 else min(sell_edge, edge)
                sell_q += q
                sell_px = p
            if sell_q > 0:
                takes.append(Take("sell", sell_px, sell_q, sell_edge))

        # ---- maker quotes around the fair-value band ----------------------
        stop = c.quote_stop_before_close_s.get(duration, 60.0)
        if not c.maker_enabled:
            return Decision(quotes, takes, "maker disabled")
        if secs_to_close <= stop:
            return Decision(quotes, takes, "maker stopped near close")
        if secs_since_open < c.quote_start_after_open_s:
            return Decision(quotes, takes, "maker waiting after open")

        size = int(Decimal(c.quote_size) * scale)
        if size <= 0:
            return Decision(quotes, takes, "size 0")
        skew = c.inventory_skew_per_share * position
        half = max(c.min_half_spread, Decimal(str(round(c.spread_vol_mult * fv.p_sd, 6))))
        bid = _round(low - c.model_margin - half - skew, self.tick, down=True)
        ask = _round(high + c.model_margin + half - skew, self.tick, down=False)
        # post-only: never cross the book
        if best_ask is not None:
            bid = min(bid, best_ask - self.tick)
        if best_bid is not None:
            ask = max(ask, best_bid + self.tick)
        fair = Decimal(str(fv.p))
        if c.quote_price_min <= bid <= c.quote_price_max:
            reb = exact_maker_rebate(1, bid, self.fees)
            quotes["buy"] = Quote("buy", bid, size, fair - bid + reb)
        if c.quote_price_min <= ask <= c.quote_price_max:
            reb = exact_maker_rebate(1, ask, self.fees)
            quotes["sell"] = Quote("sell", ask, size, ask - fair + reb)
        return Decision(quotes, takes)
