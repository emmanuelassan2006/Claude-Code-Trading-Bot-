"""Lead-lag research: does the Polymarket book lag BTC moves enough to trade?

No trading. Replays recorded monitor data (1 Hz book samples + reference BTC
price) and, for each settled window, builds a 1-second grid of:
  fair(t) - our model's P(Up) from the reference price (spot-driven)
  bid(t), ask(t), mid(t) - the recorded book
Then:
  1. Cross-correlation of 1 s changes: corr(dfair(t), dmid(t+L)) for L in -5..+10.
     A peak at L > 0 means the book follows BTC with an L-second delay.
  2. Signal test: lag(t) = [fair(t) - fair(t-h)] - [mid(t) - mid(t-h)], the part of
     the recent spot-driven move the book has not priced yet. When |lag| exceeds
     a threshold, simulate TAKING at the real ask/bid plus the taker fee and
     mark the result at mid(t+k) and at resolution. After a signal, the window
     is skipped for k seconds so overlapping samples are not double counted.

Caveats: the recording is 1 Hz and was taken from a home connection, so a
real bot sees the same information no sooner (and must still beat others).
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import Any

from pmbot.config import Config
from pmbot.fees import taker_fee_per_share
from pmbot.feeds.history import PriceHistory, Tick
from pmbot.model import annual_to_ps, prob_up, realized_vol_ps
from pmbot.store import Store

LAGS = list(range(-5, 11))
THRESHOLDS = [0.01, 0.02, 0.03, 0.05, 0.08]


def build_grids(cfg: Config, store: Store, since: float | None = None,
                edge_s: float = 30.0) -> list[dict[str, Any]]:
    """Per window: 1 s grid of fair/bid/ask plus outcome (forward-filled)."""
    wins = {r["key"]: dict(r) for r in store.query(
        "SELECT * FROM windows WHERE structure='single' AND outcome IN ('up','down') "
        "AND ptb_api IS NOT NULL AND start_ts >= ?", [since or 0])}
    if not wins:
        return []
    keys = list(wins)
    qs = ",".join("?" * len(keys))
    samples = store.query(
        f"SELECT ts, window_key, up_bid, up_ask, ref_price FROM book_samples "
        f"WHERE window_key IN ({qs}) ORDER BY ts", keys)
    c = cfg.strategy
    lo, hi = annual_to_ps(c.vol_floor_annual), annual_to_ps(c.vol_cap_annual)
    hist: dict[str, PriceHistory] = defaultdict(lambda: PriceHistory(cfg.price_feed.history_s))
    last_added: dict[str, float] = {}
    vol: dict[str, tuple[float, float | None]] = {}
    state: dict[str, dict[str, Any]] = {}
    grids: dict[str, dict[str, Any]] = {}

    def fill_to(key: str, until: float) -> None:
        """Emit grid points for every whole second up to `until` using the last state."""
        g, st = grids[key], state[key]
        w = wins[key]
        t = g["next_t"]
        while t <= until:
            if w["start_ts"] + edge_s <= t <= w["end_ts"] - edge_s and st.get("fair") is not None:
                g["t"].append(t)
                g["fair"].append(st["fair"])
                g["bid"].append(st["bid"])
                g["ask"].append(st["ask"])
            t += 1.0
        g["next_t"] = t

    for s in samples:
        key, ts = s["window_key"], s["ts"]
        w = wins[key]
        asset = w["asset"]
        if s["ref_price"] is not None and ts > last_added.get(asset, -1):
            hist[asset].add(Tick(ts, float(s["ref_price"])))
            last_added[asset] = ts
        if key not in grids:
            grids[key] = {"key": key, "duration": w["duration"],
                          "y": 1 if w["outcome"] == "up" else 0,
                          "t": [], "fair": [], "bid": [], "ask": [],
                          "next_t": math.ceil(max(ts, w["start_ts"]))}
            state[key] = {}
        fill_to(key, ts)
        if s["up_bid"] is None or s["up_ask"] is None or s["ref_price"] is None:
            continue
        if ts < w["start_ts"]:
            continue
        cached = vol.get(asset)
        if cached and ts - cached[0] < c.vol_refresh_s:
            sigma = cached[1]
        else:
            sigma = realized_vol_ps(hist[asset], ts, c.vol_lookback_s, c.vol_sample_s,
                                    c.vol_min_returns)
            vol[asset] = (ts, sigma)
        if sigma is None:
            continue
        sigma = min(max(sigma, lo), hi)
        tau = w["end_ts"] - ts
        settle_w = float(c.settle_twap_s.get(w["duration"], 0))
        realized = hist[asset].average(w["end_ts"] - settle_w, ts) \
            if settle_w > 0 and tau < settle_w else None
        state[key] = {
            "fair": prob_up(float(s["ref_price"]), float(w["ptb_api"]), sigma, tau, settle_w,
                            realized),
            "bid": float(s["up_bid"]), "ask": float(s["up_ask"]),
        }
    for key in grids:
        fill_to(key, wins[key]["end_ts"])
    return [g for g in grids.values() if len(g["t"]) > 20]


def _corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def cross_correlation(grids: list[dict[str, Any]]) -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    for lag in LAGS:
        xs: list[float] = []
        ys: list[float] = []
        for g in grids:
            f = g["fair"]
            m = [(b + a) / 2 for b, a in zip(g["bid"], g["ask"])]
            n = len(f)
            for i in range(1, n):
                j = i + lag
                if 1 <= j < n:
                    xs.append(f[i] - f[i - 1])
                    ys.append(m[j] - m[j - 1])
        out[lag] = _corr(xs, ys)
    return out


def signal_test(cfg: Config, grids: list[dict[str, Any]], lookback: int = 5,
                horizon: int = 10) -> list[dict[str, Any]]:
    rows = []
    for thr in THRESHOLDS:
        n = 0
        wins: set[str] = set()
        moved: list[float] = []
        pnl_h: list[float] = []
        pnl_res: list[float] = []
        for g in grids:
            f, b, a = g["fair"], g["bid"], g["ask"]
            m = [(x + y) / 2 for x, y in zip(b, a)]
            i, N = lookback, len(f)
            while i < N - horizon:
                lag = (f[i] - f[i - lookback]) - (m[i] - m[i - lookback])
                if abs(lag) < thr:
                    i += 1
                    continue
                sign = 1.0 if lag > 0 else -1.0
                entry = a[i] if sign > 0 else b[i]
                fee = float(taker_fee_per_share(Decimal(str(entry)), cfg.fees))
                n += 1
                wins.add(g["key"])
                moved.append(sign * (m[i + horizon] - m[i]))
                pnl_h.append(sign * (m[i + horizon] - entry) - fee)
                pnl_res.append(sign * (g["y"] - entry) - fee)
                i += horizon          # don't double count the same move
        rows.append({
            "threshold": thr, "signals": n, "windows": len(wins),
            "mid_follow": sum(moved) / n if n else None,
            "follow_rate_pct": 100.0 * sum(x > 0 for x in moved) / n if n else None,
            "taker_pnl_at_horizon": sum(pnl_h) / n if n else None,
            "taker_pnl_to_resolution": sum(pnl_res) / n if n else None,
        })
    return rows


def analyze(cfg: Config, store: Store, since: float | None = None, lookback: int = 5,
            horizon: int = 10) -> dict[str, Any]:
    grids = build_grids(cfg, store, since)
    out: dict[str, Any] = {"lookback": lookback, "horizon": horizon, "groups": {}}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in grids:
        groups[g["duration"]].append(g)
        groups["all"].append(g)
    for name, gs in groups.items():
        out["groups"][name] = {
            "windows": len(gs), "seconds": sum(len(g["t"]) for g in gs),
            "xcorr": cross_correlation(gs),
            "signals": signal_test(cfg, gs, lookback, horizon),
        }
    return out


def render(res: dict[str, Any]) -> str:
    if not res["groups"]:
        return ("No settled windows with priceToBeat and reference prices yet. "
                "Run `pmbot monitor` or `pmbot run` first.")
    h, k = res["lookback"], res["horizon"]
    L = ["Lead-lag: does the Polymarket book lag BTC moves?", "=" * 50]
    for name in sorted(res["groups"], key=lambda x: (x != "all", x)):
        g = res["groups"][name]
        xc = g["xcorr"]
        valid = {lag: v for lag, v in xc.items() if v is not None}
        peak = max(valid, key=lambda lag: valid[lag]) if valid else None
        L += ["", f"[{name}] windows={g['windows']} seconds={g['seconds']}",
              "  corr(1s change in model fair, 1s change in book mid L seconds later):"]
        L.append("   " + "  ".join(f"L{lag:+d}:{'-' if v is None else f'{v:.2f}'}"
                                    for lag, v in xc.items()))
        if peak is not None:
            verdict = ("book LAGS spot by ~%d s" % peak) if peak > 0 else \
                ("book moves WITH spot (no lag)" if peak == 0 else
                 "book LEADS our spot feed by ~%d s" % -peak)
            L.append(f"  peak at L={peak:+d} (corr {valid[peak]:.2f}): {verdict}")
        L.append(f"  signal: model moved but book didn't (last {h}s); take at real ask/bid "
                 f"+ fee; measured {k}s later and at resolution:")
        for r in g["signals"]:
            if not r["signals"]:
                L.append(f"    |lag|>={r['threshold']:.2f}: no signals")
                continue
            L.append(
                f"    |lag|>={r['threshold']:.2f}: signals={r['signals']} "
                f"windows={r['windows']} book followed {r['follow_rate_pct']:.0f}% "
                f"(avg {r['mid_follow']:+.3f})  taker P&L/share: "
                f"{k}s={r['taker_pnl_at_horizon']:+.3f} "
                f"resolution={r['taker_pnl_to_resolution']:+.3f}")
    L += ["", "How to read: an edge needs BOTH a peak at L>0 AND positive taker P&L after fees "
          "across many windows. Anything else means the book is not slow enough to beat."]
    return "\n".join(L)
