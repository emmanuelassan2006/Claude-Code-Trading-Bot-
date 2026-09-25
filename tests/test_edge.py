from decimal import Decimal as D

from pmbot.book import Level, OrderBook, WindowBooks
from pmbot.config import FeeConfig
from pmbot.edge import maker_pair_edge, marginal_pair_cost, quote_taker_pair

CFG = FeeConfig()


def md(bids, asks):
    return {"bids": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in bids],
            "offers": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in asks]}


def single(bids, asks, long_is_up=True):
    b = OrderBook("m")
    b.apply_snapshot(md(bids, asks))
    return WindowBooks("single", b, None, long_is_up)


def pair(up, down):
    u, d = OrderBook("u"), OrderBook("d")
    u.apply_snapshot(md(*up))
    d.apply_snapshot(md(*down))
    return WindowBooks("pair", u, d)


def test_book_sorting_and_parsing():
    b = OrderBook("m")
    b.apply_snapshot(md([(0.40, 5), (0.45, 3)], [(0.55, 2), (0.50, 7)]))
    assert b.best_bid == Level(D("0.45"), D("3"))
    assert b.best_ask == Level(D("0.50"), D("7"))


def test_single_book_views():
    wb = single([(0.45, 10), (0.44, 5)], [(0.47, 8)])
    assert wb.up_asks()[0] == Level(D("0.47"), D("8"))
    assert wb.down_asks()[0] == Level(D("0.55"), D("10"))  # 1 - YES bid
    assert wb.down_bids()[0] == Level(D("0.53"), D("8"))   # 1 - YES ask


def test_single_book_inverted_when_market_is_down():
    wb = single([(0.45, 10)], [(0.47, 8)], long_is_up=False)
    assert wb.down_asks()[0].price == D("0.47")
    assert wb.up_asks()[0].price == D("0.55")


def test_single_book_can_never_show_taker_gap():
    wb = single([(0.49, 100)], [(0.50, 100)])
    q = quote_taker_pair(wb.up_asks(), wb.down_asks(), CFG)
    assert q.top_cost >= D("1.01")  # 1 + spread + fees
    assert q.size == 0


def test_single_book_maker_edge_equals_spread():
    wb = single([(0.47, 10)], [(0.50, 10)])
    assert maker_pair_edge(wb.up_bids()[0].price, wb.down_bids()[0].price) == D("0.03")


def test_pair_taker_gap_sized_to_min_depth():
    wb = pair(([(0.40, 5)], [(0.45, 30)]), ([(0.45, 5)], [(0.48, 12)]))
    q = quote_taker_pair(wb.up_asks(), wb.down_asks(), CFG)
    # 0.45 + 0.48 + 0.06*(0.2475 + 0.2496) = 0.95983
    assert q.top_cost == marginal_pair_cost(D("0.45"), D("0.48"), CFG)
    assert q.top_gap > D("0.04")
    assert q.size == 12 and q.top_size == 12
    assert q.profit == q.size - q.cost and q.profit > 0


def test_pair_walks_levels_until_unprofitable():
    up_asks = [Level(D("0.45"), D("10")), Level(D("0.50"), D("10"))]
    dn_asks = [Level(D("0.48"), D("15")), Level(D("0.52"), D("50"))]
    q = quote_taker_pair(up_asks, dn_asks, CFG)
    # 10 @ (0.45+0.48) ok; next (0.50+0.48)+fees > 1 -> stop
    assert q.size == 10
    assert q.up_fills == ((D("10"), D("0.45")),)


def test_min_gap_threshold():
    up_asks = [Level(D("0.48"), D("10"))]
    dn_asks = [Level(D("0.48"), D("10"))]
    # cost ~= 0.96 + 0.06*2*0.2496 = 0.98995
    assert quote_taker_pair(up_asks, dn_asks, CFG, min_gap=D("0")).size == 10
    assert quote_taker_pair(up_asks, dn_asks, CFG, min_gap=D("0.02")).size == 0


def test_rounded_fees_can_shrink_size():
    # exact edge is tiny: rounding the per-order fees up must not create a loss
    up_asks = [Level(D("0.49"), D("1"))]
    dn_asks = [Level(D("0.49"), D("1"))]
    q = quote_taker_pair(up_asks, dn_asks, CFG)
    assert q.profit >= 0


def test_empty_books():
    q = quote_taker_pair([], [Level(D("0.5"), D("1"))], CFG)
    assert q.top_cost is None and q.size == 0
