"""Arbitrage edge math shared by the monitor and (Phase 2) strategies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmbot.book import Level
from pmbot.config import FeeConfig
from pmbot.fees import order_fee, taker_fee_per_share

ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass(frozen=True)
class TakerPairQuote:
    """Buying `size` Up and `size` Down by walking both ask ladders."""

    top_cost: Decimal | None      # ask_up + ask_down + exact per-share fees at top of book
    top_gap: Decimal | None       # 1 - top_cost  (>0 means a gap exists)
    top_size: Decimal             # min(qty at best ask up, qty at best ask down)
    size: Decimal                 # executable pairs while marginal cost <= 1 - min_gap
    cost: Decimal                 # total cash paid incl. rounded fees for `size`
    fees: Decimal
    profit: Decimal               # size * 1 - cost (locked at resolution)
    up_fills: tuple[tuple[Decimal, Decimal], ...]
    down_fills: tuple[tuple[Decimal, Decimal], ...]


def marginal_pair_cost(p_up: Decimal, p_down: Decimal, cfg: FeeConfig) -> Decimal:
    return p_up + p_down + taker_fee_per_share(p_up, cfg) + taker_fee_per_share(p_down, cfg)


def quote_taker_pair(
    up_asks: list[Level],
    down_asks: list[Level],
    cfg: FeeConfig,
    min_gap: Decimal = ZERO,
    max_levels: int = 10,
    max_size: Decimal | None = None,
) -> TakerPairQuote:
    if not up_asks or not down_asks:
        return TakerPairQuote(None, None, ZERO, ZERO, ZERO, ZERO, ZERO, (), ())

    top_cost = marginal_pair_cost(up_asks[0].price, down_asks[0].price, cfg)
    top_size = min(up_asks[0].qty, down_asks[0].qty)
    threshold = ONE - min_gap

    ui = di = 0
    u_left = up_asks[0].qty
    d_left = down_asks[0].qty
    size = ZERO
    up_fills: list[tuple[Decimal, Decimal]] = []
    down_fills: list[tuple[Decimal, Decimal]] = []

    def add(fills: list[tuple[Decimal, Decimal]], price: Decimal, qty: Decimal) -> None:
        if fills and fills[-1][1] == price:
            fills[-1] = (fills[-1][0] + qty, price)
        else:
            fills.append((qty, price))

    while ui < min(len(up_asks), max_levels) and di < min(len(down_asks), max_levels):
        pu, pd = up_asks[ui].price, down_asks[di].price
        if marginal_pair_cost(pu, pd, cfg) > threshold:
            break
        q = min(u_left, d_left)
        if max_size is not None:
            q = min(q, max_size - size)
        # whole contracts only
        q = q.to_integral_value(rounding="ROUND_FLOOR")
        if q <= 0:
            break
        add(up_fills, pu, q)
        add(down_fills, pd, q)
        size += q
        u_left -= q
        d_left -= q
        if max_size is not None and size >= max_size:
            break
        if u_left <= 0:
            ui += 1
            u_left = up_asks[ui].qty if ui < len(up_asks) else ZERO
        if d_left <= 0:
            di += 1
            d_left = down_asks[di].qty if di < len(down_asks) else ZERO

    # Rounded fees can make the last increment unprofitable; shrink until it isn't.
    while size > 0:
        fees = order_fee(up_fills, False, cfg) + order_fee(down_fills, False, cfg)
        notional = sum((q * p for q, p in up_fills + down_fills), ZERO)
        cost = notional + fees
        if size - cost >= min_gap * size:
            return TakerPairQuote(
                top_cost, ONE - top_cost, top_size, size, cost, fees, size - cost,
                tuple(up_fills), tuple(down_fills),
            )
        up_fills = _trim_last(up_fills)
        down_fills = _trim_last(down_fills)
        size -= 1

    return TakerPairQuote(top_cost, ONE - top_cost, top_size, ZERO, ZERO, ZERO, ZERO, (), ())


def _trim_last(fills: list[tuple[Decimal, Decimal]]) -> list[tuple[Decimal, Decimal]]:
    fills = list(fills)
    q, p = fills[-1]
    if q <= 1:
        fills.pop()
    else:
        fills[-1] = (q - 1, p)
    return fills


def maker_pair_edge(up_bid: Decimal | None, down_bid: Decimal | None) -> Decimal | None:
    """Locked edge per pair if resting bids on both sides fill: 1 - (bid_up + bid_down).

    Makers pay no fee, so this is the edge before rebates. On a "single" book it
    equals the YES bid/ask spread.
    """
    if up_bid is None or down_bid is None:
        return None
    return ONE - up_bid - down_bid
