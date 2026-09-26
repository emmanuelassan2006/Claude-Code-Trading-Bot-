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


def fav_store(windows):
    """windows: list of (outcome, up_bid, up_ask) held flat for the whole 15m window."""
    s = Store(":memory:")
    for i, (outcome, bid, ask) in enumerate(windows):
        key, start = f"w{i}", i * 900.0
        s.upsert_window({"key": key, "asset": "BTC", "duration": "15m", "start_ts": start,
                         "end_ts": start + 900, "structure": "single", "up_slug": "y",
                         "down_slug": None, "long_is_up": 1, "title": "t"})
        s.update_window(key, outcome=outcome)
        for t in range(0, 900, 5):
            s.add_sample({"ts": start + t, "window_key": key, "secs_to_close": 900 - t,
                          "up_bid": bid, "up_bid_qty": 50, "up_ask": ask, "up_ask_qty": 5})
    return s


def test_favorite_one_entry_per_window_fee_and_side():
    from pmbot.patterns import favorite_trades

    s = fav_store([("up", 0.90, 0.92), ("up", 0.05, 0.07)])
    tr = favorite_trades(CFG, s, lo=0.80, hi=0.97, secs_left=120, shares=20)
    assert len(tr) == 2
    up, down = sorted(tr, key=lambda t: t["window"])
    assert up["side"] == "up" and up["price"] == 0.92 and up["won"] == 1
    assert 115 <= up["secs_left"] <= 120
    # fee: round(0.0695 * 20 * 0.92 * 0.08, 2) = 0.10 -> 0.005/share
    assert abs(up["fee_per_share"] - 0.005) < 1e-9
    assert abs(up["pnl_per_share"] - (1 - 0.92 - 0.005)) < 1e-9
    # Down is the favourite at 1 - 0.05 = 0.95, and loses
    assert down["side"] == "down" and down["price"] == 0.95 and down["won"] == 0


def test_favorite_respects_cutoff_and_range():
    from pmbot.patterns import favorite_trades

    s = fav_store([("up", 0.60, 0.62)])
    assert favorite_trades(CFG, s, lo=0.80, hi=0.97, secs_left=120) == []
    s = fav_store([("up", 0.90, 0.92)])
    assert favorite_trades(CFG, s, secs_left=20, cutoff_s=30) == []


def test_favorite_stats_and_bound():
    from pmbot.patterns import favorite, favorite_stats, loss_rate_upper, render_favorite

    assert abs(loss_rate_upper(0, 100) - 0.0295) < 0.001      # ~3/n rule
    assert loss_rate_upper(5, 10) > 0.5
    s = fav_store([("up", 0.90, 0.92)] * 9 + [("down", 0.90, 0.92)])
    st = favorite_stats(__import__("pmbot.patterns", fromlist=["x"]).favorite_trades(
        CFG, s, secs_left=120), shares=20)
    assert st["n"] == 10 and st["losses"] == 1
    assert st["thin_top_pct"] == 100.0          # only 5 shares at the ask
    assert st["edge_per_share"] < 0             # 90% wins at 0.925 all-in loses
    res = favorite(CFG, s)
    assert "15m" in res["groups"] and "b/e%" in render_favorite(res)
