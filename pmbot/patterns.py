"""Research on recorded US data for patterns seen in public polymarket.com
wallets. No trading: both functions only read the monitor database.

longshot - Favorite/longshot check. At fixed checkpoints before each window
           closes, take the recorded book and ask: when a side's ask was p, did
           that side win more or less than p of the time? A cheap contract that
           wins more often than its price is an edge (after the taker fee).
           Each window contributes at most one observation per side per
           checkpoint, so one window can't be counted hundreds of times.

ladder   - Replays a static resting-bid ladder (default: bids at 5c, 15c, ..., 95c
           on both Up and Down, the pattern of wallet 98euf98a) against the
           recorded US trade tape. On US one book serves both sides: a Down bid at
           g is an Up offer at 1-g. A level is placed only once it would rest
           (below the ask), fills once when a trade prints THROUGH it
           (conservative about the queue), and is held to resolution. Makers pay
           no fee; the rebate is ignored (also conservative).

Both depend on the settled outcome. Rows within a window are correlated, so
read `windows` too: an edge seen in a handful of windows is noise.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from decimal import Decimal
from typing import Any

from pmbot.config import Config
from pmbot.fees import taker_fee_per_share
from pmbot.store import Store

CHECKPOINTS = {
    "15m": [600, 420, 300, 240, 180, 120, 90, 60, 45, 30],
    "1h": [2700, 1800, 1200, 900, 600, 300, 180, 120, 60, 30],
}
PRICE_BUCKETS = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 0.40),
                 (0.40, 0.60), (0.60, 0.75), (0.75, 0.85), (0.85, 0.90), (0.90, 0.95),
                 (0.95, 1.01)]
CHEAP = 0.15


def _settled_windows(store: Store, since: float | None) -> dict[str, dict[str, Any]]:
    return {r["key"]: dict(r) for r in store.query(
        "SELECT * FROM windows WHERE structure='single' AND outcome IN ('up','down') "
        "AND start_ts >= ?", [since or 0])}


def _bucket(p: float) -> str | None:
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return None


# ---------------------------------------------------------------- longshot


def longshot_obs(store: Store, since: float | None = None, tolerance_s: float = 5.0
                 ) -> list[dict[str, Any]]:
    """One row per (window, checkpoint, side): ask paid, bid, and whether it won."""
    wins = _settled_windows(store, since)
    if not wins:
        return []
    keys = list(wins)
    qs = ",".join("?" * len(keys))
    by_win: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for s in store.query(
            f"SELECT window_key, secs_to_close, up_bid, up_ask FROM book_samples "
            f"WHERE window_key IN ({qs}) AND up_bid IS NOT NULL AND up_ask IS NOT NULL "
            f"ORDER BY ts", keys):
        by_win[s["window_key"]].append((s["secs_to_close"], s["up_bid"], s["up_ask"]))
    rows: list[dict[str, Any]] = []
    for key, samples in by_win.items():
        w = wins[key]
        up_won = w["outcome"] == "up"
        for cp in CHECKPOINTS.get(w["duration"], []):
            best = min(samples, key=lambda x: abs(x[0] - cp))
            if abs(best[0] - cp) > tolerance_s:
                continue
            _, bid, ask = best
            if not (0 < bid < ask < 1):
                continue
            for side, a, b, won in (("up", ask, bid, up_won),
                                    ("down", 1 - bid, 1 - ask, not up_won)):
                rows.append({"window": key, "duration": w["duration"], "secs_left": cp,
                             "side": side, "ask": round(a, 4), "bid": round(b, 4),
                             "won": 1 if won else 0})
    return rows


def _stats(cfg: Config, rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    wr = sum(r["won"] for r in rows) / n
    ask = sum(r["ask"] for r in rows) / n
    mid = sum((r["ask"] + r["bid"]) / 2 for r in rows) / n
    fee = sum(float(taker_fee_per_share(Decimal(str(r["ask"])), cfg.fees)) for r in rows) / n
    return {
        "n": n, "windows": len({r["window"] for r in rows}),
        "avg_ask": round(ask, 4), "win_rate": round(wr, 4),
        "se": round(math.sqrt(max(wr * (1 - wr), 1e-9) / n), 4),
        "edge_taker": round(wr - ask - fee, 4),     # buy at the ask, pay the fee
        "edge_at_mid": round(wr - mid, 4),          # upper bound: resting bid filled at mid
    }


def longshot(cfg: Config, store: Store, since: float | None = None) -> dict[str, Any]:
    obs = longshot_obs(store, since)
    out: dict[str, Any] = {"groups": {}}
    for dur in sorted({r["duration"] for r in obs}):
        rs = [r for r in obs if r["duration"] == dur]
        by_price: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rs:
            b = _bucket(r["ask"])
            if b:
                by_price[b].append(r)
        cheap_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in rs:
            if r["ask"] <= CHEAP:
                cheap_by_time[r["secs_left"]].append(r)
        half = (900 if dur == "15m" else 3600) / 2
        out["groups"][dur] = {
            "windows": len({r["window"] for r in rs}),
            "by_price": {b: _stats(cfg, v) for b, v in sorted(by_price.items())},
            "cheap_by_secs_left": {cp: _stats(cfg, cheap_by_time[cp])
                                   for cp in CHECKPOINTS[dur] if cheap_by_time.get(cp)},
            "cheap_late": _stats(cfg, [r for r in rs
                                       if r["ask"] <= CHEAP and r["secs_left"] <= half]),
        }
    return out


def _fmt(st: dict[str, Any]) -> str:
    if not st.get("n"):
        return "no data"
    return (f"n={st['n']:<5} windows={st['windows']:<4} avg ask={st['avg_ask']:.3f} "
            f"won={st['win_rate']:.3f}±{st['se']:.3f}  edge/share: taker={st['edge_taker']:+.3f} "
            f"at-mid={st['edge_at_mid']:+.3f}")


def render_longshot(res: dict[str, Any]) -> str:
    if not res["groups"]:
        return "No settled windows with book samples yet. Run `pmbot monitor` first."
    L = ["Longshot check: does a side priced at p win more than p of the time?", "=" * 66]
    for dur, g in res["groups"].items():
        L += ["", f"[{dur}] windows={g['windows']}", "  by ask price (all checkpoints):"]
        for b, st in g["by_price"].items():
            L.append(f"    {b}: {_fmt(st)}")
        L.append(f"  cheap sides (ask <= {CHEAP:.2f}) by seconds left:")
        for cp, st in g["cheap_by_secs_left"].items():
            L.append(f"    {cp:>5}s: {_fmt(st)}")
        L.append(f"  CHEAP + LATE (ask <= {CHEAP:.2f}, second half): {_fmt(g['cheap_late'])}")
    L += ["", "How to read: edge = win rate - price (taker also pays the fee). An edge is real only if",
          "it is positive by more than ~2 standard errors (±) AND backed by many windows.",
          "at-mid is an upper bound: a resting bid gets filled mostly when the price is moving against it."]
    return "\n".join(L)


# ---------------------------------------------------------------- ladder


def ladder(store: Store, since: float | None = None, levels: list[float] | None = None,
           shares: float = 1.0, start_delay_s: float = 3.0, cutoff_s: float = 30.0,
           place_until_s: float | None = None) -> dict[str, Any]:
    """Replay a static two-sided bid ladder against the recorded trade tape.

    place_until_s: stop placing NEW levels this many seconds into the window
    (None = until the cutoff). Placed levels rest until filled or the cutoff.
    """
    grid = levels or [round(0.05 + 0.10 * i, 2) for i in range(10)]
    wins = _settled_windows(store, since)
    per_window: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for key, w in wins.items():
        start, end = w["start_ts"], w["end_ts"]
        long_up = w["long_is_up"] is None or bool(w["long_is_up"])
        events: list[tuple[float, int, float, float]] = []   # (ts, kind 0=book 1=trade, a, b)
        for s in store.query("SELECT ts, up_bid, up_ask FROM book_samples WHERE window_key=? "
                             "AND up_bid IS NOT NULL AND up_ask IS NOT NULL", [key]):
            events.append((s["ts"], 0, s["up_bid"], s["up_ask"]))
        for t in store.query("SELECT ts, price FROM trades WHERE window_key=? "
                             "AND price IS NOT NULL", [key]):
            p = t["price"] if long_up else 1 - t["price"]
            events.append((t["ts"], 1, p, 0.0))
        if not any(e[1] == 1 for e in events):
            continue
        events.sort()
        placed: dict[tuple[str, float], bool] = {}           # (side, level) -> filled?
        cash = 0.0
        pos = {"up": 0.0, "down": 0.0}
        for ts, kind, a, b in events:
            if ts < start + start_delay_s or ts > end - cutoff_s:
                continue
            if kind == 0:
                if place_until_s is not None and ts > start + place_until_s:
                    continue
                bid, ask = a, b
                for g in grid:
                    # post-only: an Up bid must sit below the Up ask; a Down bid at g
                    # is an Up offer at 1-g and must sit above the Up bid.
                    if ("up", g) not in placed and g < ask:
                        placed[("up", g)] = False
                    if ("down", g) not in placed and 1 - g > bid:
                        placed[("down", g)] = False
                continue
            p = a
            for (side, g), done in list(placed.items()):
                if done:
                    continue
                through = p < g if side == "up" else p > 1 - g
                if through:
                    placed[(side, g)] = True
                    cash -= g * shares
                    pos[side] += shares
                    fills.append({"window": key, "duration": w["duration"], "side": side,
                                  "price": g, "secs_in": ts - start,
                                  "won": 1 if w["outcome"] == side else 0})
        payout = pos[w["outcome"]]
        both = pos["up"] > 0 and pos["down"] > 0
        worst = cash + min(pos["up"], pos["down"])
        per_window.append({"window": key, "duration": w["duration"], "pnl": cash + payout,
                           "fills": sum(1 for d in placed.values() if d), "both_sides": both,
                           "cost": -cash, "worst_case": worst})
    return {"grid": grid, "shares": shares, "windows": per_window, "fills": fills}


def _level_stats(fs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(fs)
    wr = sum(f["won"] for f in fs) / n
    px = sum(f["price"] for f in fs) / n
    return {"fills": n, "windows": len({f["window"] for f in fs}), "win_rate": wr,
            "avg_price": px, "edge": wr - px,
            "se": math.sqrt(max(wr * (1 - wr), 1e-9) / n)}


def render_ladder(res: dict[str, Any]) -> str:
    ws = res["windows"]
    if not ws:
        return "No settled windows with recorded trades yet. Run `pmbot monitor` first."
    L = [f"Ladder replay: resting bids at {', '.join(f'{g:.2f}' for g in res['grid'])} on BOTH "
         f"sides, {res['shares']:g} share(s) per level, fill on trade-through, held to resolution.",
         "=" * 78]
    for dur in sorted({w["duration"] for w in ws}) + ["all"]:
        sel = [w for w in ws if dur == "all" or w["duration"] == dur]
        fs = [f for f in res["fills"] if dur == "all" or f["duration"] == dur]
        pn = [w["pnl"] for w in sel]
        L += ["", f"[{dur}] windows={len(sel)} fills={len(fs)} "
                  f"both-sides filled in {sum(w['both_sides'] for w in sel)} windows",
              f"  P&L: total={sum(pn):+.2f}  per window mean={statistics.mean(pn):+.3f} "
              f"sd={statistics.pstdev(pn):.3f}  best={max(pn):+.2f} worst={min(pn):+.2f}  "
              f"windows>0: {100 * sum(p > 0 for p in pn) / len(pn):.0f}%",
              f"  avg cost/window={statistics.mean(w['cost'] for w in sel):.2f}  "
              f"worst-case/window (at fill)={min(w['worst_case'] for w in sel):+.2f}"]
        if fs:
            L.append("  by level (bought side at price g; edge = win rate - g):")
            for g in res["grid"]:
                lf = [f for f in fs if abs(f["price"] - g) < 1e-9]
                if lf:
                    st = _level_stats(lf)
                    L.append(f"    {g:.2f}: fills={st['fills']:<4} windows={st['windows']:<4} "
                             f"won={st['win_rate']:.3f}±{st['se']:.3f} edge={st['edge']:+.3f}")
    L += ["", "How to read: positive total P&L with a large share of windows > 0 and positive "
          "edge at several levels would justify a paper-trading strategy. Negative edge at a level "
          "= adverse selection: those bids fill mostly when the price keeps moving through them."]
    return "\n".join(L)
