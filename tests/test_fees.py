from decimal import Decimal as D

import pytest

from pmbot.config import FeeConfig
from pmbot.fees import (exact_taker_fee, fill_cost, order_fee, round_money,
                        taker_fees_for_fills)

CFG = FeeConfig(taker_rate=D("0.06"))  # the documented worked examples use 0.06


def test_docs_worked_example_100_at_50c():
    # docs: max taker fee $1.50 per 100 contracts at p = 0.50
    assert fill_cost(100, "0.50", False, CFG) == D("1.50")


@pytest.mark.parametrize("shares,price,expected", [
    (1, "0.50", D("0.02")),     # 0.015 -> half-even -> 0.02
    (1, "0.25", D("0.01")),     # 0.01125
    (10, "0.05", D("0.03")),    # 0.0285
    (1, "0.01", D("0.00")),     # tiny fills round to zero
    (100, "0.90", D("0.54")),   # 0.06*100*0.09
])
def test_taker_fee_examples(shares, price, expected):
    assert fill_cost(shares, price, False, CFG) == expected


def test_live_btc_updown_coefficient_0695():
    # live BTC Up/Down markets report feeCoefficient 0.0695: 0.0695*100*0.25 = 1.7375
    assert fill_cost(100, "0.50", False, FeeConfig()) == D("1.74")


def test_bankers_rounding():
    assert round_money(D("0.025"), CFG) == D("0.02")
    assert round_money(D("0.035"), CFG) == D("0.04")
    assert round_money(D("0.0251"), CFG) == D("0.03")


def test_fee_symmetric_in_p():
    assert exact_taker_fee(37, "0.3", CFG) == exact_taker_fee(37, "0.7", CFG)


def test_multi_fill_cap_never_exceeds_rounded_cumulative():
    fills = [(1, "0.50")] * 3  # each 0.015 -> 0.02, but cumulative 0.045 -> 0.04
    charges = taker_fees_for_fills(fills, CFG)
    assert charges == [D("0.02"), D("0.01"), D("0.01")]
    assert sum(charges) == D("0.04")
    assert order_fee(fills, False, CFG) == D("0.04")


def test_multi_fill_cap_only_reduces():
    fills = [(100, "0.50"), (1, "0.01")]
    charges = taker_fees_for_fills(fills, CFG)
    for c, (s, p) in zip(charges, fills):
        assert c <= fill_cost(s, p, False, CFG)


def test_cap_disabled():
    cfg = FeeConfig(taker_rate=D("0.06"), cap_cumulative=False)
    assert order_fee([(1, "0.50")] * 3, False, cfg) == D("0.06")


def test_maker_pays_nothing_and_gets_rebate():
    # 0.0125 * 100 * 0.25 = 0.3125 -> 0.31 rebate
    assert fill_cost(100, "0.50", True, CFG) == D("-0.31")
    assert fill_cost(100, "0.50", True, FeeConfig(maker_rebate_rate=D("0"))) == D("0")


def test_rates_come_from_config():
    cfg = FeeConfig(taker_rate=D("0.10"))  # explicit rate
    assert fill_cost(100, "0.50", False, cfg) == D("2.50")


def test_price_out_of_range():
    with pytest.raises(ValueError):
        fill_cost(1, "1.2", False, CFG)
