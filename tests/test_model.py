import math
import random

from pmbot.feeds.history import PriceHistory, Tick
from pmbot.model import annual_to_ps, fair_value, prob_up, realized_vol_ps

SIG = annual_to_ps(0.5)


def test_at_the_money_is_half():
    assert abs(prob_up(100_000, 100_000, SIG, 900) - 0.5) < 1e-12
    assert abs(prob_up(100_000, 100_000, SIG, 900, twap_s=60) - 0.5) < 1e-12


def test_monotone_in_spot_and_time():
    lo = prob_up(100_000, 100_050, SIG, 900)
    hi = prob_up(100_050, 100_000, SIG, 900)
    assert lo < 0.5 < hi
    # same lead, less time -> more certain
    assert prob_up(100_050, 100_000, SIG, 60) > hi


def test_expiry_is_a_step():
    assert prob_up(100_001, 100_000, SIG, 0) == 1.0
    assert prob_up(100_000, 100_000, SIG, 0) == 1.0   # "at or above" wins
    assert prob_up(99_999, 100_000, SIG, 0) == 0.0


def test_twap_settlement_reduces_variance():
    point = prob_up(100_050, 100_000, SIG, 120, twap_s=0)
    twap = prob_up(100_050, 100_000, SIG, 120, twap_s=60)
    assert twap > point > 0.5


def test_inside_twap_window_uses_realized_average():
    # 10 s left of a 60 s average; 50 s already averaged at 100_200 -> Up nearly certain
    assert prob_up(99_990, 100_000, SIG, 10, twap_s=60, realized_avg=100_200) > 0.999
    assert prob_up(100_010, 100_000, SIG, 10, twap_s=60, realized_avg=99_800) < 0.001


def test_realized_vol_recovers_known_vol():
    rng = random.Random(1)
    sig = annual_to_ps(0.6)
    h = PriceHistory(max_age_s=10_000)
    p = 100_000.0
    for t in range(0, 3600):
        h.add(Tick(float(t), p))
        p *= math.exp(sig * rng.gauss(0, 1))
    est = realized_vol_ps(h, 3599, 1800, 5, 30)
    assert abs(est / sig - 1) < 0.15


def test_realized_vol_needs_data():
    h = PriceHistory()
    h.add(Tick(0, 1.0))
    assert realized_vol_ps(h, 10, 1800, 5, 30) is None


def test_fair_value_band():
    fv = fair_value(100_030, [100_000, 100_010], SIG, 600, 60, None, 0.25, 3)
    assert fv.low <= fv.p <= fv.high
    narrow = fair_value(100_030, [100_000], SIG, 600, 60, None, 0.0, 0)
    assert narrow.low == narrow.p == narrow.high
