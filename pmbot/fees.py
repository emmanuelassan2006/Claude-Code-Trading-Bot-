"""The single fee model. Every edge calculation and the simulator use this.

Polymarket US (docs.polymarket.us/fees):
  taker fee    = taker_rate        * shares * p * (1 - p)
  maker fee    = maker_fee_rate    * shares * p * (1 - p)   (0 today)
  maker rebate = maker_rebate_rate * shares * p * (1 - p)
Each fill is rounded to the cent with banker's rounding. For an aggressive
order with several fills, the total charged never exceeds the rounded
cumulative exact fee; that adjustment only ever reduces a fill's charge.

p(1-p) is symmetric, so the fee is the same whether p is the YES price or the
complementary short (NO) price.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Iterable

from pmbot.config import FeeConfig

ZERO = Decimal("0")
ONE = Decimal("1")


def D(x: object) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_money(amount: Decimal, cfg: FeeConfig) -> Decimal:
    if cfg.rounding_mode == "none":
        return amount
    mode = ROUND_HALF_EVEN if cfg.rounding_mode == "half_even" else ROUND_HALF_UP
    if cfg.rounding_mode not in ("half_even", "half_up"):
        raise ValueError(f"unknown rounding_mode {cfg.rounding_mode!r}")
    return amount.quantize(cfg.rounding_increment, rounding=mode)


def _curve(shares: object, price: object) -> Decimal:
    p = D(price)
    if p < ZERO or p > ONE:
        raise ValueError(f"price out of range: {p}")
    return D(shares) * p * (ONE - p)


def exact_taker_fee(shares: object, price: object, cfg: FeeConfig) -> Decimal:
    return cfg.taker_rate * _curve(shares, price)


def exact_maker_rebate(shares: object, price: object, cfg: FeeConfig) -> Decimal:
    return cfg.maker_rebate_rate * _curve(shares, price)


def fill_cost(shares: object, price: object, is_maker: bool, cfg: FeeConfig) -> Decimal:
    """Net fee for ONE fill (positive = you pay, negative = you receive a rebate)."""
    if is_maker:
        fee = round_money(cfg.maker_fee_rate * _curve(shares, price), cfg)
        rebate = round_money(exact_maker_rebate(shares, price, cfg), cfg)
        return fee - rebate
    return round_money(exact_taker_fee(shares, price, cfg), cfg)


def taker_fees_for_fills(
    fills: Iterable[tuple[object, object]], cfg: FeeConfig
) -> list[Decimal]:
    """Per-fill taker fees for one aggressive order, with the cumulative cap."""
    charged: list[Decimal] = []
    cum_exact = ZERO
    cum_charged = ZERO
    for shares, price in fills:
        exact = exact_taker_fee(shares, price, cfg)
        charge = round_money(exact, cfg)
        cum_exact += exact
        if cfg.cap_cumulative:
            cap = round_money(cum_exact, cfg) - cum_charged
            charge = max(ZERO, min(charge, cap))
        cum_charged += charge
        charged.append(charge)
    return charged


def order_fee(
    fills: Iterable[tuple[object, object]], is_maker: bool, cfg: FeeConfig
) -> Decimal:
    """Total net fee for an order given its fills [(shares, price), ...].

    Taker orders apply the multi-fill cap; maker fills are independent.
    """
    fills = list(fills)
    if is_maker:
        return sum((fill_cost(s, p, True, cfg) for s, p in fills), ZERO)
    return sum(taker_fees_for_fills(fills, cfg), ZERO)


def taker_fee_per_share(price: object, cfg: FeeConfig) -> Decimal:
    """Exact (unrounded) marginal taker fee per share; used for level walking."""
    return exact_taker_fee(1, price, cfg)
