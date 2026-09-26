from pmbot.calibrate import collect, render, summarize
from pmbot.config import Config
from pmbot.store import Store


def _db(market_is_smart: bool):
    """Two 15m windows; the reference price is flat at the strike (model says ~0.5)
    while the market mid leans toward the true outcome (or away from it)."""
    s = Store(":memory:")
    t0 = 1790000100.0
    for k, outcome in enumerate(("up", "down")):
        start, end = t0 + 900 * k, t0 + 900 * (k + 1)
        key = f"w{k}"
        s.upsert_window({"key": key, "asset": "BTC", "duration": "15m", "start_ts": start,
                         "end_ts": end, "structure": "single", "up_slug": key,
                         "down_slug": None, "long_is_up": 1, "title": ""})
        s.update_window(key, outcome=outcome, ptb_api=100000.0)
        lean = 0.8 if (outcome == "up") == market_is_smart else 0.2
        for t in range(int(start) - (1800 if k == 0 else 0), int(end)):
            wiggle = 100000.0 + (1 if t % 2 else -1)   # enough returns for a vol estimate
            s.add_sample({"ts": float(t), "window_key": key, "up_bid": lean - 0.01,
                          "up_ask": lean + 0.01, "ref_price": wiggle})
    return s


def test_market_wins_when_it_leans_right():
    summ = summarize(collect(Config(), _db(market_is_smart=True)))
    g = summ["all"]
    assert g["windows"] == 2
    assert g["brier_market"] < g["brier_model"]
    big = g["disagreement"][-1]            # gap >= 0.20 (model ~0.5 vs market 0.8/0.2)
    assert big["samples"] > 0 and big["pnl_per_share_at_mid"] < 0
    assert "better predictor: MARKET" in render(summ)


def test_model_wins_when_market_leans_wrong():
    g = summarize(collect(Config(), _db(market_is_smart=False)))["all"]
    assert g["brier_model"] < g["brier_market"]
    assert g["disagreement"][-1]["pnl_per_share_at_mid"] > 0


def test_empty_db():
    assert "No settled windows" in render(summarize(collect(Config(), Store(":memory:"))))
