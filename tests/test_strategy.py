from decimal import Decimal as D

from pmbot.book import Level
from pmbot.config import FeeConfig, StrategyConfig
from pmbot.model import FairValue
from pmbot.strategy import FairValueStrategy

TICK = D("0.01")


def fv(p, low=None, high=None):
    return FairValue(p, low if low is not None else p, high if high is not None else p,
                     100_000, 100_000, 1e-4, 600)


def strat(**kw):
    cfg = StrategyConfig(**kw)
    return FairValueStrategy(cfg, FeeConfig(), TICK)


def L(*pq):
    return [Level(D(str(p)), D(str(q))) for p, q in pq]


def decide(s, f, bids=None, asks=None, pos=0, to_close=600, since_open=60, scale=D("1")):
    return s.decide(f, bids if bids is not None else L((0.30, 100)),
                    asks if asks is not None else L((0.70, 100)), pos, to_close, since_open,
                    "15m", scale)


def test_quotes_bracket_fair_band():
    d = decide(strat(), fv(0.50, 0.48, 0.52))
    # 0.48 - 0.01 margin - 0.01 half-spread = 0.46 ; 0.52 + 0.02 = 0.54
    assert d.quotes["buy"].price == D("0.46")
    assert d.quotes["sell"].price == D("0.54")
    assert d.quotes["buy"].qty == 10 and d.takes == []
    assert d.quotes["buy"].exp_edge_ps > D("0.04")


def test_rounding_is_conservative():
    d = decide(strat(), fv(0.503, 0.503, 0.503))
    assert d.quotes["buy"].price == D("0.48")    # 0.483 floored
    assert d.quotes["sell"].price == D("0.53")   # 0.523 ceiled


def test_post_only_never_crosses():
    d = decide(strat(), fv(0.50), bids=L((0.45, 5)), asks=L((0.47, 5)))
    assert d.quotes["buy"].price <= D("0.46")
    d = decide(strat(), fv(0.50), bids=L((0.53, 5)), asks=L((0.60, 5)))
    assert d.quotes["sell"].price >= D("0.54")


def test_inventory_skew_leans_against_position():
    flat = decide(strat(), fv(0.50))
    long = decide(strat(), fv(0.50), pos=20)     # 20 * 0.001 = 0.02 lower
    assert long.quotes["buy"].price == flat.quotes["buy"].price - D("0.02")
    assert long.quotes["sell"].price == flat.quotes["sell"].price - D("0.02")


def test_no_quotes_near_close_or_just_after_open():
    assert decide(strat(), fv(0.5), to_close=59).quotes == {"buy": None, "sell": None}
    assert decide(strat(), fv(0.5), since_open=1).quotes == {"buy": None, "sell": None}


def test_no_quotes_at_price_extremes():
    d = decide(strat(), fv(0.97), bids=L((0.90, 5)), asks=L((0.99, 5)))
    assert d.quotes["sell"] is None


def test_trade_scale_scales_sizes():
    d = decide(strat(), fv(0.5), scale=D("2.5"))
    assert d.quotes["buy"].qty == 25


def test_taker_buys_only_when_edge_beats_fee():
    s = strat()
    # fair 0.60; ask 0.55: edge = 0.05 - 0.06*0.2475 = 0.03515 >= 0.02 -> take
    d = decide(s, fv(0.60), bids=L((0.50, 50)), asks=L((0.55, 7), (0.56, 50)))
    t = d.takes[0]
    assert t.side == "buy" and t.qty == 20 and t.limit_price == D("0.56")
    # ask 0.58: edge 0.02 - 0.0146 < 0.02 -> no take
    d = decide(s, fv(0.60), bids=L((0.50, 50)), asks=L((0.58, 50)))
    assert d.takes == []


def test_taker_sells_rich_bid_and_respects_cap():
    d = decide(strat(taker_max_shares=5), fv(0.40), bids=L((0.46, 100)), asks=L((0.60, 5)))
    assert d.takes[0].side == "sell" and d.takes[0].qty == 5


def test_components_can_be_disabled():
    d = decide(strat(taker_enabled=False, maker_enabled=False), fv(0.60),
               asks=L((0.40, 10)))
    assert d.takes == [] and d.quotes == {"buy": None, "sell": None}


def test_spread_widens_with_fair_value_volatility():
    calm = decide(strat(), fv(0.50))
    fast = FairValue(0.50, 0.50, 0.50, 100_000, 100_000, 1e-4, 600, p_sd=0.03)
    d = decide(strat(), fast)
    assert d.quotes["buy"].price == D("0.46")          # 0.50 - 0.01 margin - 0.03
    assert d.quotes["buy"].price < calm.quotes["buy"].price
