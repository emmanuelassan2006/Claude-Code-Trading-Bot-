"""Fair value of a crypto Up/Down window.

Up wins if the settlement price S_T >= the price to beat K. Over minutes, BTC is
well approximated by driftless arithmetic Brownian motion with volatility
sigma*S per sqrt(second), so P(Up) = Phi((E[settle] - K) / sd).

If settlement is an average over the final `w` seconds (a TWAP), the variance
of the average is smaller than that of a point price:
  before the averaging window (tau > w):  var = (sigma S)^2 * (tau - 2w/3)
  inside it (tau <= w), with the realized part R = integral of price so far:
      mean = (R + S*tau) / w,   var = (sigma S)^2 * tau^3 / (3 w^2)
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from pmbot.feeds.chainlink import PriceHistory

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(spot: float, strike: float, sigma_ps: float, tau: float, twap_s: float = 0.0,
            realized_avg: float | None = None) -> float:
    """P(settlement >= strike).

    sigma_ps: volatility per sqrt(second) (fraction of price).
    tau: seconds until the window closes.
    twap_s: settlement averaging window (0 = last price).
    realized_avg: average price over [close - twap_s, now] when inside the window.
    """
    sd_unit = sigma_ps * spot
    if twap_s <= 0:
        mean, var = spot, sd_unit ** 2 * max(tau, 0.0)
    elif tau > twap_s:
        mean, var = spot, sd_unit ** 2 * (tau - 2.0 * twap_s / 3.0)
    else:
        tau = max(tau, 0.0)
        elapsed = twap_s - tau
        realized = (realized_avg if realized_avg is not None else spot) * elapsed
        mean = (realized + spot * tau) / twap_s
        var = sd_unit ** 2 * tau ** 3 / (3.0 * twap_s ** 2)
    if var <= 0:
        return 1.0 if mean >= strike else 0.0
    return norm_cdf((mean - strike) / math.sqrt(var))


def realized_vol_ps(history: PriceHistory, now: float, lookback_s: float, sample_s: float,
                    min_returns: int) -> float | None:
    """Realized volatility per sqrt(second) from log returns on a fixed grid."""
    ts, vals = history.series()
    if len(ts) < 2:
        return None
    start = max(ts[0], now - lookback_s)
    grid: list[float] = []
    t = start
    while t <= now:
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0:
            grid.append(vals[i])
        t += sample_s
    rets = [math.log(b / a) for a, b in zip(grid, grid[1:]) if a > 0 and b > 0]
    if len(rets) < min_returns:
        return None
    var = sum(r * r for r in rets) / len(rets)
    return math.sqrt(var / sample_s)


def annual_to_ps(vol_annual: float) -> float:
    return vol_annual / math.sqrt(SECONDS_PER_YEAR)


@dataclass(frozen=True)
class FairValue:
    p: float            # P(Up) at the central inputs
    low: float          # lower edge of the uncertainty band
    high: float         # upper edge
    spot: float
    strike: float
    sigma_ps: float
    tau: float
    p_sd: float = 0.0   # 1-sd change in p over the quote horizon


def fair_value(spot: float, strikes: list[float], sigma_ps: float, tau: float, twap_s: float,
               realized_avg: float | None, vol_uncertainty: float,
               price_uncertainty_bps: float, horizon_s: float = 0.0) -> FairValue:
    """Central P(Up) plus a band over sigma, spot and strike uncertainty.

    p_sd is how much P(Up) moves for a 1-sd spot move over `horizon_s`
    (symmetric finite difference, time decay ignored).
    """
    k0 = strikes[0]
    p = prob_up(spot, k0, sigma_ps, tau, twap_s, realized_avg)
    p_sd = 0.0
    if horizon_s > 0:
        d = sigma_ps * math.sqrt(horizon_s)
        up = prob_up(spot * (1 + d), k0, sigma_ps, tau, twap_s, realized_avg)
        dn = prob_up(spot * (1 - d), k0, sigma_ps, tau, twap_s, realized_avg)
        p_sd = abs(up - dn) / 2
    e = price_uncertainty_bps / 1e4
    probs = [p]
    for sig in (sigma_ps * (1 - vol_uncertainty), sigma_ps * (1 + vol_uncertainty)):
        for s in (spot * (1 - e), spot * (1 + e)):
            for k in strikes:
                probs.append(prob_up(s, k, sig, tau, twap_s, realized_avg))
    return FairValue(p, min(probs), max(probs), spot, k0, sigma_ps, tau, p_sd)
