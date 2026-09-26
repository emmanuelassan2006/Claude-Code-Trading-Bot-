"""Is our fair-value model a better predictor than the market?

Replays recorded monitor data (1 Hz book samples + reference price) and, every
`every_s` seconds of each settled window, compares:
  * p_model  - our P(Up) from the fair-value model (same code the engine uses)
  * p_market - the market mid (best bid + best ask) / 2
against the actual outcome. Lower Brier score / log loss = better predictor.

The disagreement table answers the question that matters for trading: when the
model and the market disagree, who is right, and what would trading at the mid
toward the model have earned per share (before fees)?

Samples within a window are highly correlated, so also look at `windows`: a
result backed by a handful of windows is weak evidence.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from pmbot.config import Config
from pmbot.feeds.history import PriceHistory, Tick
from pmbot.model import annual_to_ps, prob_up, realized_vol_ps
from pmbot.store import Store

GAP_BUCKETS = [(0.05, 0.10), (0.10, 0.20), (0.20, 1.01)]


def _brier(ps: list[float], ys: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def _logloss(ps: list[float], ys: list[int]) -> float:
    eps = 1e-4
    return -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps))
                for p, y in zip(ps, ys)) / len(ps)


def collect(cfg: Config, store: Store, since: float | None = None, every_s: float = 10.0,
            min_secs_left: float = 30.0) -> list[dict[str, Any]]:
    wins = {r["key"]: dict(r) for r in store.query(
        "SELECT * FROM windows WHERE structure='single' AND outcome IN ('up','down') "
        "AND start_ts >= ?", [since or 0])}
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
    last_eval: dict[str, float] = {}
    vol_cache: dict[str, tuple[float, float | None]] = {}
    rows: list[dict[str, Any]] = []
    for s in samples:
        w = wins[s["window_key"]]
        asset, ts = w["asset"], s["ts"]
        if s["ref_price"] is not None and ts > last_added.get(asset, -1):
            hist[asset].add(Tick(ts, float(s["ref_price"])))
            last_added[asset] = ts
        if s["up_bid"] is None or s["up_ask"] is None or s["ref_price"] is None:
            continue
        tau = w["end_ts"] - ts
        if ts < w["start_ts"] or tau < min_secs_left:
            continue
        if ts - last_eval.get(w["key"], -1e18) < every_s:
            continue
        strike = w.get("ptb_api")
        if strike is None:
            continue
        cached = vol_cache.get(asset)
        if cached and ts - cached[0] < 5:
            sigma = cached[1]
        else:
            sigma = realized_vol_ps(hist[asset], ts, c.vol_lookback_s, c.vol_sample_s,
                                    c.vol_min_returns)
            vol_cache[asset] = (ts, sigma)
        if sigma is None:
            continue
        sigma = min(max(sigma, lo), hi)
        last_eval[w["key"]] = ts
        settle_w = float(c.settle_twap_s.get(w["duration"], 0))
        realized = hist[asset].average(w["end_ts"] - settle_w, ts) \
            if settle_w > 0 and tau < settle_w else None
        rows.append({
            "window": w["key"], "duration": w["duration"], "tau": tau,
            "p_model": prob_up(float(s["ref_price"]), float(strike), sigma, tau, settle_w,
                               realized),
            "p_market": (s["up_bid"] + s["up_ask"]) / 2,
            "y": 1 if w["outcome"] == "up" else 0,
        })
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r["duration"]].append(r)
        groups["all"].append(r)
    for name, rs in groups.items():
        pm = [r["p_model"] for r in rs]
        pk = [r["p_market"] for r in rs]
        ys = [r["y"] for r in rs]
        g: dict[str, Any] = {
            "samples": len(rs), "windows": len({r["window"] for r in rs}),
            "brier_model": _brier(pm, ys), "brier_market": _brier(pk, ys),
            "brier_coinflip": _brier([0.5] * len(ys), ys),
            "logloss_model": _logloss(pm, ys), "logloss_market": _logloss(pk, ys),
            "disagreement": [],
        }
        for lo, hi in GAP_BUCKETS:
            sel = [r for r in rs if lo <= abs(r["p_model"] - r["p_market"]) < hi]
            if not sel:
                g["disagreement"].append({"gap": f"{lo:.2f}-{min(hi, 1):.2f}", "samples": 0})
                continue
            model_right = sum(abs(r["y"] - r["p_model"]) < abs(r["y"] - r["p_market"])
                              for r in sel)
            pnl = [(1 if r["p_model"] > r["p_market"] else -1) * (r["y"] - r["p_market"])
                   for r in sel]
            g["disagreement"].append({
                "gap": f"{lo:.2f}-{min(hi, 1):.2f}", "samples": len(sel),
                "windows": len({r["window"] for r in sel}),
                "model_right_pct": 100.0 * model_right / len(sel),
                "pnl_per_share_at_mid": sum(pnl) / len(pnl),
            })
        out[name] = g
    return out


def render(summary: dict[str, Any]) -> str:
    if not summary:
        return ("No settled windows with priceToBeat and reference prices yet. "
                "Run `pmbot monitor` or `pmbot run` first.")
    L = ["Model vs market calibration (lower Brier / log loss = better predictor)", "=" * 70]
    for name in sorted(summary, key=lambda k: (k != "all", k)):
        g = summary[name]
        better = "MODEL" if g["brier_model"] < g["brier_market"] else "MARKET"
        L += [
            "",
            f"[{name}] windows={g['windows']} samples={g['samples']}   better predictor: {better}",
            f"  Brier:    model={g['brier_model']:.4f}  market={g['brier_market']:.4f}  "
            f"coin-flip={g['brier_coinflip']:.4f}",
            f"  log loss: model={g['logloss_model']:.4f}  market={g['logloss_market']:.4f}",
            "  when they disagree (|model - market|):",
        ]
        for d in g["disagreement"]:
            if not d["samples"]:
                L.append(f"    gap {d['gap']}: no samples")
                continue
            L.append(
                f"    gap {d['gap']}: samples={d['samples']} windows={d['windows']} "
                f"model closer to outcome {d['model_right_pct']:.0f}%  "
                f"trade-at-mid toward model: {d['pnl_per_share_at_mid']:+.3f} $/share")
    L += ["", "How to read: if the market has the lower Brier and trading toward the model at "
          "large gaps loses money, the model should follow the market, not fight it."]
    return "\n".join(L)
