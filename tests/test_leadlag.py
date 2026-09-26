import math
import random

from pmbot.config import Config
from pmbot.leadlag import analyze, render
from pmbot.model import annual_to_ps, prob_up
from pmbot.store import Store

T0 = 1790000100.0


def _db(delay: int, windows: int = 4, seed: int = 5):
    """Random-walk BTC; the book mid is the true fair value `delay` seconds ago."""
    rng = random.Random(seed)
    sig = annual_to_ps(0.6)
    s = Store(":memory:")
    p = 100_000.0
    prices = {}
    for i in range(-1800, 900 * windows + 60):
        prices[T0 + i] = p
        p *= math.exp(sig * rng.gauss(0, 1))
    for k in range(windows):
        start, end = T0 + 900 * k, T0 + 900 * (k + 1)
        key = f"w{k}"
        strike = prices[start]
        s.upsert_window({"key": key, "asset": "BTC", "duration": "15m", "start_ts": start,
                         "end_ts": end, "structure": "single", "up_slug": key,
                         "down_slug": None, "long_is_up": 1, "title": ""})
        s.update_window(key, outcome="up" if prices[end] >= strike else "down", ptb_api=strike)
        first = int(start) - (1800 if k == 0 else 0)
        for t in range(first, int(end)):
            tt = float(t)
            if tt >= start:
                lag_t = max(tt - delay, start)
                mid = prob_up(prices[lag_t], strike, sig, end - lag_t, 60)
            else:
                mid = 0.5
            mid = min(max(mid, 0.02), 0.98)
            s.add_sample({"ts": tt, "window_key": key, "up_bid": round(mid - 0.005, 4),
                          "up_ask": round(mid + 0.005, 4), "ref_price": prices[tt]})
    return s


def _peak(res):
    xc = {k: v for k, v in res["groups"]["all"]["xcorr"].items() if v is not None}
    return max(xc, key=xc.get)


def test_detects_a_lagging_book():
    res = analyze(Config(), _db(delay=3))
    assert res["groups"]["all"]["windows"] == 4
    assert _peak(res) == 3
    sig = [r for r in res["groups"]["all"]["signals"] if r["signals"]][-1]
    assert sig["follow_rate_pct"] > 60 and sig["mid_follow"] > 0
    assert "book LAGS spot by ~3 s" in render(res)


def test_no_lag_when_book_moves_with_spot():
    res = analyze(Config(), _db(delay=0))
    assert _peak(res) == 0
    assert "no lag" in render(res)


def test_empty():
    assert "No settled windows" in render(analyze(Config(), Store(":memory:")))
