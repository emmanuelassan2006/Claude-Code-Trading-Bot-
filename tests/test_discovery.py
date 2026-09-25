import asyncio

from pmbot.config import MarketsConfig
from pmbot.discovery import Discoverer, classify_event, detect_asset, parse_ts

CFG = MarketsConfig()


def ev(**kw):
    base = {"slug": "btc-updown-5m-1790000000", "title": "Bitcoin Up or Down - 5 Minutes",
            "startTime": "2026-09-21T14:13:20Z", "endTime": "2026-09-21T14:18:20Z",
            "markets": [{"slug": "btc-updown-5m-1790000000", "outcome": "Up"}]}
    base.update(kw)
    return base


def test_parse_ts_variants():
    assert parse_ts("2026-09-21T14:13:20Z") == 1790000000.0
    assert parse_ts(1790000000000) == 1790000000.0
    assert parse_ts("1790000000") == 1790000000.0
    assert parse_ts("garbage") is None


def test_asset_detection_word_boundaries():
    assert detect_asset("Bitcoin Up or Down", ["BTC", "ETH"]) == "BTC"
    assert detect_asset("eth-updown-15m", ["BTC", "ETH"]) == "ETH"
    assert detect_asset("Solana price", ["BTC"]) is None


def test_single_market_window():
    w = classify_event(ev(), CFG)
    assert w and w.structure == "single" and w.long_is_up
    assert w.asset == "BTC" and w.duration == "5m"
    assert w.end_ts - w.start_ts == 300


def test_single_market_down_outcome_inverts():
    w = classify_event(ev(markets=[{"slug": "x", "outcome": "Down"}]), CFG)
    assert w and not w.long_is_up


def test_pair_window():
    w = classify_event(ev(markets=[{"slug": "b-up", "outcome": "Up"},
                                   {"slug": "b-down", "outcome": "Down"}]), CFG)
    assert w and w.structure == "pair"
    assert (w.up_slug, w.down_slug) == ("b-up", "b-down")


def test_slug_timestamp_fallback():
    w = classify_event(ev(startTime=None, endTime=None), CFG)
    assert w and w.start_ts == 1790000000 and w.end_ts == 1790000300


def test_rejects_non_updown_and_unwanted_duration():
    assert classify_event(ev(title="Will BTC hit 200k?", slug="btc-200k"), CFG) is None
    cfg = MarketsConfig(durations=["15m"])
    assert classify_event(ev(), cfg) is None


def test_rejects_unknown_layout():
    assert classify_event(ev(markets=[{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]), CFG) is None


def test_discoverer_lookahead_and_dedup():
    now = 1790000000 + 10
    later = ev(slug="btc-updown-5m-1790003000", startTime=None, endTime=None,
               markets=[{"slug": "l", "outcome": "Up"}])
    ended = ev(slug="btc-updown-5m-1789990000", startTime=None, endTime=None,
               markets=[{"slug": "e", "outcome": "Up"}])

    async def fetch(path, params):
        return {"events": [ev(), later, ended]}

    d = Discoverer(CFG, fetch)
    found = asyncio.run(d.poll(now))
    assert [w.key for w in found] == ["btc-updown-5m-1790000000"]
    assert asyncio.run(d.poll(now)) == []  # already known


def test_discoverer_survives_fetch_errors():
    async def fetch(path, params):
        raise RuntimeError("boom")

    assert asyncio.run(Discoverer(CFG, fetch).poll(0)) == []
