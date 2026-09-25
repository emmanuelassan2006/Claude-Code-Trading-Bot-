import asyncio
import json
from decimal import Decimal as D

from pmbot.config import Config, load_secrets
from pmbot.discovery import Window
from pmbot.feeds.chainlink import ChainlinkFeed, Tick
from pmbot.monitor import Monitor, outcome_from_settlement
from pmbot.store import Store

START = 1790000000.0


def md(bids, asks):
    return {"bids": [{"px": {"value": str(p)}, "qty": str(q)} for p, q in bids],
            "offers": [{"px": {"value": str(p)}, "qty": str(q)} for p, q in asks]}


class FakePublic:
    def __init__(self, settlement=None):
        self._settlement = settlement

    async def get(self, path, params=None):
        return {"events": []}

    async def market(self, slug):
        return {"description": "Resolves Up if the Chainlink BTC/USD ... "}

    async def settlement(self, slug):
        if self._settlement is None:
            raise RuntimeError("not settled")
        return {"slug": slug, "settlement": self._settlement}


class FakeStream:
    def __init__(self):
        self.wanted = set()

    def watch(self, s):
        self.wanted.update(s)

    def unwatch(self, s):
        self.wanted.difference_update(s)


def make(structure="pair", settlement=None):
    cfg = Config()
    cfg.monitor.spread_sample_interval_s = 0
    store = Store(":memory:")
    feed = ChainlinkFeed(cfg.chainlink, cfg.api)
    mon = Monitor(cfg, load_secrets(None), store, FakePublic(settlement), FakeStream(), feed)
    if structure == "pair":
        w = Window("btc-w", "BTC", "5m", START, START + 300, "pair", "up", "down", True)
    else:
        w = Window("btc-w", "BTC", "5m", START, START + 300, "single", "yes", None, True)
    st = mon.add_window(w)
    return mon, store, st, feed


def test_gap_episode_recorded_with_duration_and_depth():
    mon, store, st, _ = make()
    t = START + 10
    mon.on_book("up", md([(0.40, 5)], [(0.45, 30)]), t, "ws")
    assert st.gap is None  # down book not ready yet
    mon.on_book("down", md([(0.45, 5)], [(0.48, 12)]), t + 0.1, "ws")
    assert st.gap is not None
    mon.on_book("up", md([(0.40, 5)], [(0.44, 30)]), t + 0.3, "ws")  # bigger gap
    mon.on_book("up", md([(0.40, 5)], [(0.60, 30)]), t + 0.5, "ws")  # gap gone
    assert st.gap is None
    rows = store.query("SELECT * FROM taker_gaps")
    assert len(rows) == 1
    g = rows[0]
    assert abs(g["duration_ms"] - 400) < 1
    assert g["depth_up_at_start"] == 30 and g["depth_down_at_start"] == 12
    assert g["max_gap_ts"] == t + 0.3
    assert g["exec_size_at_max"] == 12 and g["exec_profit_at_max"] > 0
    assert g["ended_by"] == "book"
    assert store.query("SELECT COUNT(*) c FROM book_samples")[0]["c"] == 3


def test_no_gap_before_window_opens():
    mon, store, st, _ = make()
    mon.on_book("up", md([(0.40, 5)], [(0.45, 30)]), START - 5, "ws")
    mon.on_book("down", md([(0.45, 5)], [(0.48, 12)]), START - 5, "ws")
    assert st.gap is None


def test_single_book_samples_maker_edge_as_spread():
    mon, store, st, _ = make("single")
    mon.on_book("yes", md([(0.47, 10)], [(0.50, 10)]), START + 1, "ws")
    s = store.query("SELECT * FROM book_samples")[0]
    assert abs(s["maker_edge"] - 0.03) < 1e-9
    assert s["taker_cost"] > 1.0
    assert store.query("SELECT COUNT(*) c FROM taker_gaps")[0]["c"] == 0


def test_lifecycle_refs_close_and_settlement():
    mon, store, st, feed = make("pair", settlement=1)
    for i in range(-60, 400):
        feed.history["BTC"].add(Tick(START + i, 100.0 + (1 if i > 250 else 0)))
    mon.on_book("up", md([(0.40, 5)], [(0.45, 30)]), START + 10, "ws")
    mon.on_book("down", md([(0.45, 5)], [(0.48, 12)]), START + 10, "ws")
    mon.tick_lifecycle(START + 70)
    assert st.open_ref["twap_ending"] == 100.0 and st.ptb == 100.0
    mon.tick_lifecycle(START + 300)  # close: open gap is closed as window_end
    assert st.closed
    assert store.query("SELECT ended_by FROM taker_gaps")[0]["ended_by"] == "window_end"
    mon.tick_lifecycle(START + 370)
    assert json.loads(store.query("SELECT close_ref FROM windows")[0]["close_ref"])[
        "twap_ending"] == 101.0
    assert "up" not in mon.stream.wanted
    asyncio.run(mon.check_settlements(START + 370))
    w = store.query("SELECT * FROM windows")[0]
    assert w["outcome"] == "up" and w["settlement"] == 1.0
    mon.tick_lifecycle(START + 371)
    assert "btc-w" not in mon.windows


def test_trades_recorded_with_timing():
    mon, store, st, _ = make()
    mon.on_trade("up", {"marketSlug": "up", "price": {"value": "0.55"},
                        "quantity": {"value": "7"}, "tradeTime": "2026-09-21T14:14:20Z",
                        "maker": {"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_SELL_LONG"},
                        "taker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG"}},
                 START + 61)
    t = store.query("SELECT * FROM trades")[0]
    assert t["price"] == 0.55 and t["qty"] == 7 and t["secs_into_window"] == 60


def test_outcome_mapping():
    assert outcome_from_settlement(1, True) == "up"
    assert outcome_from_settlement(0, True) == "down"
    assert outcome_from_settlement(1, False) == "down"
    assert outcome_from_settlement(65000.1, True) is None


def test_description_fetched():
    mon, store, st, _ = make()
    asyncio.run(mon.fetch_description(st))
    assert "Chainlink" in store.query("SELECT description FROM windows")[0]["description"]
