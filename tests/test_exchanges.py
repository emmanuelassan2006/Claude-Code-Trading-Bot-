import json

from pmbot.config import ApiConfig, PriceFeedConfig
from pmbot.discovery import parse_market_terms
from pmbot.feeds.exchanges import ExchangeFeed, parse_coinbase, parse_kraken


def test_parse_coinbase_ticker():
    msg = {"type": "ticker", "product_id": "BTC-USD", "best_bid": "83900.10",
           "best_ask": "83900.30", "price": "83900.20"}
    sym, mid = parse_coinbase(msg)
    assert sym == "BTC-USD" and abs(mid - 83900.2) < 1e-6
    assert parse_coinbase({"type": "subscriptions"}) is None


def test_parse_kraken_v2_ticker():
    msg = {"channel": "ticker", "type": "update",
           "data": [{"symbol": "BTC/USD", "bid": 83899.9, "ask": 83900.1, "last": 83900.0}]}
    assert parse_kraken(msg) == [("BTC/USD", 83900.0)]
    assert parse_kraken({"channel": "heartbeat"}) == []


def test_composite_is_median_of_fresh_sources_and_throttled():
    ticks = []
    f = ExchangeFeed(PriceFeedConfig(sample_interval_s=0.5, source_stale_s=5), ApiConfig(),
                     on_tick=lambda a, t: ticks.append((a, t.value)))
    f.update("BTC", "coinbase", 100.0, now=1000.0)
    f.update("BTC", "kraken", 102.0, now=1000.2)       # throttled (< 0.5 s)
    f.update("BTC", "kraken", 102.0, now=1000.6)       # median of 100, 102
    f.update("BTC", "kraken", 104.0, now=1010.0)       # coinbase stale -> kraken only
    assert ticks == [("BTC", 100.0), ("BTC", 101.0), ("BTC", 104.0)]
    assert f.history["BTC"].latest().value == 104.0


def test_feed_handlers_route_by_symbol():
    f = ExchangeFeed(PriceFeedConfig(sample_interval_s=0), ApiConfig())
    f.handle_coinbase(json.dumps({"type": "ticker", "product_id": "BTC-USD",
                                  "best_bid": "10", "best_ask": "12"}))
    f.handle_coinbase(json.dumps({"type": "ticker", "product_id": "ETH-USD",
                                  "best_bid": "1", "best_ask": "2"}))
    f.handle_kraken("not json")
    assert f.history["BTC"].latest().value == 11.0


LIVE_MARKET = {  # trimmed from the owner's live probe, 2026-09-26
    "slug": "cpc-btc-updown-15m-2026-09-26-0315z", "orderPriceMinTickSize": 0.01,
    "feeCoefficient": 0.0695, "minimumTradeQty": 0.01,
    "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}],
    "assetPriceTerms": {"marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN", "indexSymbol": "BRTI",
                        "horizon": "15m", "windowStart": "2026-09-26T03:15:00Z",
                        "windowEnd": "2026-09-26T03:30:00Z",
                        "priceToBeat": {"value": "83920.25", "currency": "USD"},
                        "settlementPrice": None},
    "description": "This market will settle to Up if the Bitcoin price at 11:30PM ET ...",
}


def test_parse_live_market_terms():
    t = parse_market_terms(LIVE_MARKET)
    assert t["ptb"] == 83920.25 and t["settle"] is None
    assert t["window_end"] - t["window_start"] == 900
    assert t["index_symbol"] == "BRTI" and t["fee_coefficient"] == 0.0695
    assert t["tick"] == 0.01 and t["long_side"] == "Yes"
