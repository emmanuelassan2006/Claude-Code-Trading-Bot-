from decimal import Decimal as D

from pmbot.book import Level
from pmbot.config import FeeConfig
from pmbot.sim import PaperExchange


def ex():
    fills, done = [], []
    x = PaperExchange(FeeConfig(), 0.15, lambda o, p, q, m, fee, ts: fills.append((o.id, p, q, m, fee)),
                      lambda o, why: done.append((o.id, o.status, why)))
    x.on_book("w", [Level(D("0.45"), D("10"))], [Level(D("0.50"), D("10"))], 0)
    return x, fills, done


def test_post_only_rejected_if_crossing_when_live():
    x, fills, done = ex()
    o = x.place("w", "buy", D("0.50"), 5, "maker", 0)
    x.process(0.2)
    assert o.status == "rejected" and done[0][2] == "post-only would cross"


def test_maker_fills_only_on_trade_through_after_latency():
    x, fills, _ = ex()
    o = x.place("w", "buy", D("0.46"), 10, "maker", 0)
    x.on_trade("w", D("0.45"), 5, 0.1)       # before live -> ignored
    x.on_trade("w", D("0.46"), 5, 0.3)       # at our price -> queue ahead, no fill
    assert fills == []
    x.on_trade("w", D("0.44"), 4, 0.4)       # through -> fill 4 at OUR price
    assert fills[0][1:4] == (D("0.46"), 4, True)
    assert fills[0][4] <= 0                   # maker: rebate (or zero)
    assert o.remaining == 6 and o.status == "live"


def test_cancel_in_flight_can_still_fill():
    x, fills, done = ex()
    o = x.place("w", "sell", D("0.49"), 5, "maker", 0)
    x.process(0.2)
    x.cancel(o.id, 1.0)                        # effective at 1.15
    x.on_trade("w", D("0.50"), 5, 1.1)
    assert fills and fills[0][2] == 5
    o2 = x.place("w", "sell", D("0.49"), 5, "maker", 2.0)
    x.process(2.2)
    x.cancel(o2.id, 3.0)
    x.on_trade("w", D("0.50"), 5, 3.2)          # after cancel effective
    assert len(fills) == 1 and o2.status == "canceled"


def test_ioc_walks_book_with_limit_and_fee_cap():
    x, fills, done = ex()
    x.on_book("w", [], [Level(D("0.50"), D("3")), Level(D("0.52"), D("5")),
                        Level(D("0.60"), D("50"))], 0)
    o = x.place("w", "buy", D("0.52"), 20, "taker", 0)
    x.process(0.1)
    assert fills == []                          # latency not elapsed
    x.process(0.2)
    assert [(f[1], f[2]) for f in fills] == [(D("0.50"), 3), (D("0.52"), 5)]
    assert all(f[3] is False for f in fills)
    assert sum(f[4] for f in fills) > 0
    assert o.filled == 8 and o.status == "canceled"   # remainder of IOC canceled


def test_expire_window_cancels_everything():
    x, _, done = ex()
    x.place("w", "buy", D("0.40"), 5, "maker", 0)
    x.expire_window("w", 1)
    assert done[-1][1] == "canceled" and x.open_orders("w") == []
