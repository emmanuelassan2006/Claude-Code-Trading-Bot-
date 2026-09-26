import json

from pmbot.config import load_config
from pmbot.patterns import ladder, longshot, render_ladder, render_longshot
from pmbot.store import Store
from pmbot.wallet import (_clob_winner, _winner, analyze_wallet, fetch_resolutions,
                          fill_breakdown)

CFG = load_config(None)


def store_with_window(outcome="up", start=0.0):
    s = Store(":memory:")
    key = f"w{start}"
    s.upsert_window({"key": key, "asset": "BTC", "duration": "15m", "start_ts": start,
                     "end_ts": start + 900, "structure": "single", "up_slug": "y",
                     "down_slug": None, "long_is_up": 1, "title": "t"})
    s.update_window(key, outcome=outcome)
    return s, key


def test_longshot_one_obs_per_side_per_checkpoint():
    s, key = store_with_window("down")
    # Up is cheap (0.08/0.10) late in the window, and Down wins.
    for ts in range(0, 900):
        s.add_sample({"ts": ts, "window_key": key, "secs_to_close": 900 - ts,
                      "up_bid": 0.08, "up_ask": 0.10})
    res = longshot(CFG, s)
    g = res["groups"]["15m"]
    cheap = g["cheap_late"]
    # checkpoints <= 450 s left: 420,300,240,180,120,90,60,45,30 -> 9 obs of the Up side
    assert cheap["n"] == 9 and cheap["windows"] == 1 and cheap["win_rate"] == 0
    assert cheap["edge_taker"] < -0.10
    # Down side at ask 0.92 always won
    assert g["by_price"]["0.90-0.95"]["win_rate"] == 1.0
    assert "CHEAP + LATE" in render_longshot(res)


def test_longshot_empty():
    assert "No settled windows" in render_longshot(longshot(CFG, Store(":memory:")))


def test_ladder_fills_on_trade_through_and_pnl():
    s, key = store_with_window("up")
    s.add_sample({"ts": 5, "window_key": key, "secs_to_close": 895, "up_bid": 0.49,
                  "up_ask": 0.51})
    # price dips to 0.30 (fills Up bids 0.45, 0.35), then rallies to 0.70
    # (fills Down bids 0.45, 0.35 = Up offers at 0.55, 0.65)
    for ts, p in [(10, 0.30), (20, 0.70)]:
        s.add_trade({"ts": ts, "recv_ts": ts, "window_key": key, "slug": "y", "price": p,
                     "qty": 1, "secs_into_window": ts})
    res = ladder(s, levels=[0.35, 0.45], shares=1)
    w = res["windows"][0]
    assert w["fills"] == 4 and w["both_sides"]
    # paid 0.35+0.45 (Up) + 0.35+0.45 (Down) = 1.60; Up wins 2 shares -> +0.40
    assert abs(w["pnl"] - 0.40) < 1e-9 and abs(w["worst_case"] - 0.40) < 1e-9
    assert "by level" in render_ladder(res)


def test_ladder_post_only_skips_levels_at_or_above_ask():
    s, key = store_with_window("down")
    s.add_sample({"ts": 5, "window_key": key, "secs_to_close": 895, "up_bid": 0.30,
                  "up_ask": 0.32})
    s.add_trade({"ts": 10, "recv_ts": 10, "window_key": key, "slug": "y", "price": 0.20,
                 "qty": 1, "secs_into_window": 10})
    res = ladder(s, levels=[0.25, 0.45], shares=1)
    # Up 0.45 was above the ask (not placed); Up 0.25 filled; Down 0.45 = offer at 0.55 not hit
    assert [(f["side"], f["price"]) for f in res["fills"]] == [("up", 0.25)]


def test_winner_parsing_variants():
    m = {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'}
    assert _winner(m) == "Down"
    assert _winner({**m, "closed": False}) is None
    assert _winner({**m, "closed": False, "umaResolutionStatus": "resolved"}) == "Down"
    assert _winner({**m, "outcomePrices": '["0.5", "0.5"]'}) is None
    assert _clob_winner({"tokens": [{"outcome": "Up", "winner": False},
                                    {"outcome": "Down", "winner": True}]}) == "Down"


def test_fetch_resolutions_falls_back_and_reports_errors():
    calls = []

    def get(url, params):
        calls.append(url)
        if url.endswith("/markets") and "closed" not in params:
            raise RuntimeError("HTTP 422")          # default gamma query fails
        if url.endswith("/markets"):
            return []                               # closed=true: nothing
        if url.endswith("/events"):
            return [{"markets": [{"closed": True, "outcomes": ["Up", "Down"],
                                  "outcomePrices": ["1", "0"]}]}]
        raise AssertionError(url)

    winners, diag = fetch_resolutions({"a": "0x1", "b": "0x2"}, get, pause_s=0)
    assert winners == {"a": "Up", "b": "Up"}
    assert diag["found_by"] == {"gamma_event": 2}
    assert diag["errors_by_source"]["gamma_slug"] == 1   # 2nd market tries events first
    assert "HTTP 422" in diag["first_error_by_source"]["gamma_slug"]


def test_fetch_resolutions_clob_fallback_and_drop_failing_source():
    def get(url, params):
        if "clob" in url:
            return {"tokens": [{"outcome": "Up", "winner": True}]}
        raise RuntimeError("403")

    mk = {f"s{i}": f"0x{i}" for i in range(12)}
    winners, diag = fetch_resolutions(mk, get, pause_s=0)
    assert all(w == "Up" for w in winners.values())
    assert diag["found_by"] == {"clob": 12}


def test_fill_breakdown_and_analyze_wallet(tmp_path):
    slug = "btc-updown-5m-1771535700"
    trades = [
        {"slug": slug, "conditionId": "0xc", "outcome": "Up", "side": "BUY", "size": 10,
         "price": 0.05, "timestamp": 1771535950, "transactionHash": "a", "asset": "1"},
        {"slug": slug, "conditionId": "0xc", "outcome": "Down", "side": "BUY", "size": 10,
         "price": 0.45, "timestamp": 1771535710, "transactionHash": "b", "asset": "2"},
    ]
    fb = fill_breakdown([dict(t, _role="maker") for t in trades], {slug: "Up"})
    late = fb["cheap_late_buys (<=15c, 2nd half)"]
    assert late["shares"] == 10 and late["edge_per_share"] == 0.95
    assert fb["by_time_in_window"]["first 20%"]["win_rate_pct"] == 0.0

    def get(url, params):
        if url.endswith("/trades"):
            return [] if params["offset"] else trades
        if url.endswith("/markets"):
            return [{"closed": True, "outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]'}]
        raise AssertionError(url)

    summary = analyze_wallet("0xabc", str(tmp_path / "w.csv"), get, pause_s=0)
    assert summary["resolution_lookup"]["resolved"] == 1
    assert summary["total_pnl"] == 5.0          # -0.5 - 4.5 + 10
    assert summary["pnl_both_sides_markets"] == 5.0
    json.dumps(summary)
