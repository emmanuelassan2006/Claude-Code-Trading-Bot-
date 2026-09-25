import json

from pmbot.reports import build_report, maker_fill_proxy, render_text
from pmbot.store import Store
from pmbot.wallet import analyze_tape, fetch_wallet_trades, summarize_wallet, window_of


def sample(ts, ub, db, edge):
    return {"ts": ts, "up_bid": ub, "down_bid": db, "maker_edge": edge}


def test_maker_fill_proxy_pair_trade_through_only():
    samples = [sample(0, 0.45, 0.50, 0.05)]
    trades = [{"slug": "u", "ts": 5, "price": 0.45},   # at price: not counted (queue)
              {"slug": "u", "ts": 6, "price": 0.44},   # through
              {"slug": "d", "ts": 7, "price": 0.49}]   # through
    assert maker_fill_proxy(samples, trades, "pair", "u", "d", True, 0.02, 60) == (1, 1, 0)
    assert maker_fill_proxy(samples, trades[:2], "pair", "u", "d", True, 0.02, 60) == (1, 0, 1)
    # below min edge -> no quote
    assert maker_fill_proxy(samples, trades, "pair", "u", "d", True, 0.06, 60) == (0, 0, 0)


def test_maker_fill_proxy_single_book():
    # up bid 0.45 = YES bid; down bid 0.52 = YES offer at 0.48
    samples = [sample(0, 0.45, 0.52, 0.03)]
    trades = [{"slug": "y", "ts": 3, "price": 0.44}, {"slug": "y", "ts": 4, "price": 0.49}]
    assert maker_fill_proxy(samples, trades, "single", "y", None, True, 0.02, 60) == (1, 1, 0)


def populated_store():
    s = Store(":memory:")
    s.upsert_window({"key": "w1", "asset": "BTC", "duration": "5m", "start_ts": 100,
                     "end_ts": 400, "structure": "pair", "up_slug": "u", "down_slug": "d",
                     "long_is_up": 1, "title": "t"})
    s.update_window("w1", outcome="up",
                    open_ref={"twap_ending": 100.0, "first_tick_after": 100.0},
                    close_ref={"twap_ending": 101.0, "first_tick_after": 99.0})
    s.add_gap({"window_key": "w1", "asset": "BTC", "duration": "5m", "start_ts": 150,
               "end_ts": 150.5, "duration_ms": 500, "secs_to_close_at_start": 250,
               "max_gap": 0.03, "max_gap_ts": 150.2, "top_size_at_max": 10,
               "exec_size_at_max": 10, "exec_profit_at_max": 0.25, "depth_up_at_start": 10,
               "depth_down_at_start": 20, "updates": 3, "ended_by": "book"})
    s.add_sample({"ts": 150, "window_key": "w1", "secs_to_close": 250, "up_bid": 0.45,
                  "up_ask": 0.47, "down_bid": 0.50, "down_ask": 0.52, "taker_cost": 1.02,
                  "maker_edge": 0.05})
    s.add_trade({"ts": 160, "recv_ts": 160, "window_key": "w1", "slug": "u", "price": 0.44,
                 "qty": 5, "taker_intent": "ORDER_INTENT_SELL_LONG", "secs_into_window": 60})
    return s


def test_build_report_and_ptb_scoring():
    rep = build_report(populated_store())
    g = rep["groups"][0]
    assert g["windows"] == 1 and g["gap_episodes"] == 1
    assert g["windows_with_gap_pct"] == 100.0
    assert g["maker_quotes"] == 1 and g["maker_one_leg_only_pct"] == 100.0
    assert rep["ptb_rules"]["twap_ending"]["accuracy_pct"] == 100.0
    assert rep["ptb_rules"]["first_tick_after"]["accuracy_pct"] == 0.0
    assert "BTC 5m" in render_text(rep, 0.02, 60)


def test_empty_report_renders():
    assert "No windows" in render_text(build_report(Store(":memory:")), 0.02, 60)


def test_analyze_tape(tmp_path):
    out = tmp_path / "tape.csv"
    summary = analyze_tape(populated_store(), str(out))
    assert summary["trades"] == 1 and out.exists()


def test_window_of_slug():
    assert window_of("btc-updown-5m-1771535700") == ("5m", 1771535700.0, 1771536000.0)
    assert window_of("will-btc-hit-100k") is None


def test_wallet_fetch_marks_maker_vs_taker():
    t1 = {"transactionHash": "a", "asset": "1", "size": 10, "price": 0.4, "side": "BUY"}
    t2 = {"transactionHash": "b", "asset": "2", "size": 10, "price": 0.5, "side": "BUY"}

    def get(url, params):
        if params["offset"] > 0:
            return []
        return [t1] if params["takerOnly"] == "true" else [t1, t2]

    rows = fetch_wallet_trades("0xabc", get)
    assert [r["_role"] for r in rows] == ["taker", "maker"]


def test_summarize_wallet_both_sides_and_pnl():
    slug = "btc-updown-5m-1771535700"
    trades = [
        {"slug": slug, "outcome": "Up", "side": "BUY", "size": 10, "price": 0.45,
         "timestamp": 1771535730, "_role": "maker"},
        {"slug": slug, "outcome": "Down", "side": "BUY", "size": 10, "price": 0.50,
         "timestamp": 1771535760, "_role": "taker"},
    ]
    rows, summary = summarize_wallet(trades, {slug: "Up"})
    r = rows[0]
    assert r["bought_both_sides"] and r["combined_avg_price"] == 0.95
    assert r["paired_shares"] == 10
    assert r["first_entry_s"] == 30
    assert r["pnl"] == 0.5  # -4.5 - 5 + 10
    assert summary["win_rate_pct"] == 100.0 and summary["maker_fill_pct"] == 50.0
    assert json.loads(r["avg_buy_prices"]) == {"Up": 0.45, "Down": 0.5}
