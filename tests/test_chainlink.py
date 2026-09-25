import json

from pmbot.config import ApiConfig, ChainlinkConfig
from pmbot.feeds.chainlink import ChainlinkFeed, PriceHistory, Tick, parse_rtds_message


def hist(points):
    h = PriceHistory()
    for t, v in points:
        h.add(Tick(t, v))
    return h


def test_twap_step_function():
    h = hist([(0, 100.0), (10, 110.0), (20, 120.0), (40, 130.0)])
    # [0,30): 100 for 10s, 110 for 10s, 120 for 10s
    assert h.twap(0, 30) == 110.0
    assert h.twap(5, 15) == 105.0


def test_twap_needs_full_coverage():
    h = hist([(10, 100.0), (20, 110.0)])
    assert h.twap(0, 15) is None      # no price known at start
    assert h.twap(15, 30) is None     # no data through end yet


def test_out_of_order_ticks_are_sorted():
    h = hist([(0, 1.0), (20, 3.0), (10, 2.0)])
    assert h.at_or_after(5).value == 2.0


def test_candidates():
    h = hist([(0, 100.0), (30, 101.0), (61, 105.0), (100, 106.0)])
    c = h.candidates(60, 30)
    assert c["first_tick_after"] == 105.0
    assert c["last_tick_before"] == 101.0
    assert c["twap_ending"] == 101.0      # [30, 60) at 101
    assert c["twap_starting"] is not None


def test_parse_rtds_update_and_snapshot():
    upd = {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 1790000000500,
           "payload": {"symbol": "btc/usd", "timestamp": 1790000000000, "value": 65000.5}}
    assert parse_rtds_message(upd, "crypto_prices_chainlink") == [
        ("btc/usd", Tick(1790000000.0, 65000.5))]
    snap = {"topic": "crypto_prices_chainlink", "payload": {
        "symbol": "eth/usd", "data": [{"timestamp": 1790000000000, "value": 2500}]}}
    assert parse_rtds_message(snap, "crypto_prices_chainlink")[0][0] == "eth/usd"
    assert parse_rtds_message({"topic": "other", "payload": {}}, "crypto_prices_chainlink") == []


def test_feed_routes_ticks_to_assets():
    seen = []
    f = ChainlinkFeed(ChainlinkConfig(), ApiConfig(), on_tick=lambda a, t: seen.append(a))
    f.handle(json.dumps({"topic": "crypto_prices_chainlink",
                         "payload": {"symbol": "BTC/USD", "timestamp": 1, "value": 2}}))
    f.handle("PONG")
    f.handle("not json")
    assert seen == ["BTC"]
    assert f.history["BTC"].latest().value == 2
