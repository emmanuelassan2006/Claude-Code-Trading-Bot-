"""Phase 1 report: taker-gap stats, spreads / maker-edge stats, a maker fill
proxy, trade tape summary, and price-to-beat rule accuracy vs settlement."""

from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from pmbot.store import Store


def _q(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def _fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def maker_fill_proxy(
    samples: list[dict[str, Any]], trades: list[dict[str, Any]], structure: str,
    up_slug: str, down_slug: str | None, long_is_up: bool,
    min_edge: float, horizon_s: float, stride_s: float = 5.0,
) -> tuple[int, int, int]:
    """Estimate how often a two-sided maker quote at the current best bids fills.

    For each sample (every `stride_s`) whose maker edge >= min_edge, assume we
    join the best Up bid and best Down bid. A leg counts as filled only if a
    later trade within `horizon_s` prints strictly THROUGH our price (conservative
    queue assumption: at-price trades are assumed to fill the queue ahead of us).
    Returns (quotes, both_filled, one_filled).
    """
    quotes = both = one = 0
    last = -1e18
    up_trades = [t for t in trades if t["slug"] == up_slug]
    dn_trades = [t for t in trades if down_slug and t["slug"] == down_slug]
    for s in samples:
        if s["maker_edge"] is None or s["maker_edge"] < min_edge - 1e-12:
            continue
        if s["up_bid"] is None or s["down_bid"] is None or s["ts"] - last < stride_s:
            continue
        last = s["ts"]
        quotes += 1
        t0, t1 = s["ts"], s["ts"] + horizon_s
        ub, db = s["up_bid"], s["down_bid"]
        if structure == "pair":
            up_fill = any(t0 < t["ts"] <= t1 and t["price"] < ub for t in up_trades)
            dn_fill = any(t0 < t["ts"] <= t1 and t["price"] < db for t in dn_trades)
        else:
            # Single YES book. Trade prices are YES prices.
            # Up bid = YES bid if long_is_up else YES offer at 1-ub, etc.
            def through_bid(p: float) -> bool:
                return any(t0 < t["ts"] <= t1 and t["price"] < p for t in up_trades)

            def through_offer(p: float) -> bool:
                return any(t0 < t["ts"] <= t1 and t["price"] > p for t in up_trades)

            if long_is_up:
                up_fill, dn_fill = through_bid(ub), through_offer(1 - db)
            else:
                up_fill, dn_fill = through_offer(1 - ub), through_bid(db)
        if up_fill and dn_fill:
            both += 1
        elif up_fill or dn_fill:
            one += 1
    return quotes, both, one


def build_report(store: Store, since: float | None = None, min_edge: float = 0.02,
                 horizon_s: float = 60.0) -> dict[str, Any]:
    since = since or 0.0
    windows = [dict(r) for r in store.query(
        "SELECT * FROM windows WHERE mode='monitor' AND start_ts >= ?", [since])]
    gaps = [dict(r) for r in store.query(
        "SELECT * FROM taker_gaps WHERE mode='monitor' AND start_ts >= ?", [since])]
    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: defaultdict(list))
    wmap = {w["key"]: w for w in windows}

    for w in windows:
        g = groups[(w["asset"], w["duration"])]
        g["windows"].append(w)
    for gp in gaps:
        w = wmap.get(gp["window_key"])
        if w:
            groups[(w["asset"], w["duration"])]["gaps"].append(gp)

    out: dict[str, Any] = {"generated_at": time.time(), "groups": [], "ptb_rules": {}}
    rule_hits: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for (asset, dur), g in sorted(groups.items()):
        ws = g["windows"]
        gp = g["gaps"]
        spreads_up, spreads_dn, edges, costs = [], [], [], []
        quotes = both = one = 0
        n_trades = 0
        taker_intents: dict[str, int] = defaultdict(int)
        for w in ws:
            samples = [dict(r) for r in store.query(
                "SELECT * FROM book_samples WHERE window_key=? ORDER BY ts", [w["key"]])]
            trades = [dict(r) for r in store.query(
                "SELECT * FROM trades WHERE window_key=? ORDER BY ts", [w["key"]])]
            for s in samples:
                if s["up_bid"] is not None and s["up_ask"] is not None:
                    spreads_up.append(s["up_ask"] - s["up_bid"])
                if s["down_bid"] is not None and s["down_ask"] is not None:
                    spreads_dn.append(s["down_ask"] - s["down_bid"])
                if s["maker_edge"] is not None:
                    edges.append(s["maker_edge"])
                if s["taker_cost"] is not None:
                    costs.append(s["taker_cost"])
            qn, b, o = maker_fill_proxy(
                samples, trades, w["structure"], w["up_slug"], w["down_slug"],
                bool(w["long_is_up"]), min_edge, horizon_s)
            quotes, both, one = quotes + qn, both + b, one + o
            n_trades += len(trades)
            for t in trades:
                taker_intents[t["taker_intent"] or "?"] += 1
            # price-to-beat rule scoring
            if w["outcome"] in ("up", "down") and w["open_ref"] and w["close_ref"]:
                o_ref, c_ref = json.loads(w["open_ref"]), json.loads(w["close_ref"])
                for rule in o_ref:
                    ov, cv = o_ref.get(rule), c_ref.get(rule)
                    if ov is None or cv is None:
                        continue
                    pred = "up" if cv >= ov else "down"
                    rule_hits[rule][0] += int(pred == w["outcome"])
                    rule_hits[rule][1] += 1

        windows_with_gap = len({x["window_key"] for x in gp})
        max_gaps = [x["max_gap"] for x in gp]
        durs = [x["duration_ms"] for x in gp]
        out["groups"].append({
            "asset": asset, "duration": dur,
            "windows": len(ws),
            "settled": sum(1 for w in ws if w["outcome"]),
            "structures": sorted({w["structure"] for w in ws}),
            "gap_episodes": len(gp),
            "windows_with_gap_pct": 100.0 * windows_with_gap / len(ws) if ws else 0.0,
            "gap_max_median": _q(max_gaps, 0.5), "gap_max_max": max(max_gaps, default=None),
            "gap_duration_ms_median": _q(durs, 0.5), "gap_duration_ms_p90": _q(durs, 0.9),
            "gap_exec_size_median": _q([x["exec_size_at_max"] for x in gp], 0.5),
            "gap_exec_profit_total": sum(x["exec_profit_at_max"] for x in gp),
            "gap_secs_to_close_median": _q([x["secs_to_close_at_start"] for x in gp], 0.5),
            "spread_up_median": _q(spreads_up, 0.5), "spread_down_median": _q(spreads_dn, 0.5),
            "taker_cost_median": _q(costs, 0.5), "taker_cost_min": min(costs, default=None),
            "maker_edge_median": _q(edges, 0.5),
            "maker_edge_ge_min_pct": (100.0 * sum(e >= min_edge - 1e-12 for e in edges) / len(edges)
                                      if edges else None),
            "maker_quotes": quotes,
            "maker_both_filled_pct": 100.0 * both / quotes if quotes else None,
            "maker_one_leg_only_pct": 100.0 * one / quotes if quotes else None,
            "trades": n_trades,
            "taker_intents": dict(taker_intents),
        })

    out["ptb_rules"] = {
        r: {"correct": h[0], "n": h[1], "accuracy_pct": 100.0 * h[0] / h[1] if h[1] else None}
        for r, h in sorted(rule_hits.items())
    }
    return out


def render_text(rep: dict[str, Any], min_edge: float, horizon_s: float) -> str:
    lines = ["Polymarket US crypto Up/Down monitor report", "=" * 44]
    if not rep["groups"]:
        lines.append("No windows recorded yet. Run `pmbot monitor` first.")
    for g in rep["groups"]:
        lines += [
            "",
            f"{g['asset']} {g['duration']}  windows={g['windows']} settled={g['settled']} "
            f"structure={','.join(g['structures'])}",
            f"  taker gaps (ask_up+ask_down+fees<1): episodes={g['gap_episodes']} "
            f"windows_with_gap={_fmt(g['windows_with_gap_pct'], 1)}%",
            f"    max gap median={_fmt(g['gap_max_median'])} max={_fmt(g['gap_max_max'])}  "
            f"lasted median={_fmt(g['gap_duration_ms_median'], 0)}ms "
            f"p90={_fmt(g['gap_duration_ms_p90'], 0)}ms",
            f"    exec size median={_fmt(g['gap_exec_size_median'], 0)}  "
            f"theoretical profit total=${_fmt(g['gap_exec_profit_total'], 2)}  "
            f"secs-to-close median={_fmt(g['gap_secs_to_close_median'], 0)}",
            f"  taker pair cost median={_fmt(g['taker_cost_median'])} min={_fmt(g['taker_cost_min'])}",
            f"  spreads median: up={_fmt(g['spread_up_median'])} down={_fmt(g['spread_down_median'])}",
            f"  maker edge (1-bid_up-bid_down) median={_fmt(g['maker_edge_median'])}  "
            f">= {min_edge}: {_fmt(g['maker_edge_ge_min_pct'], 1)}% of samples",
            f"  maker fill proxy ({horizon_s:.0f}s, trade-through): quotes={g['maker_quotes']} "
            f"both={_fmt(g['maker_both_filled_pct'], 1)}% one-leg={_fmt(g['maker_one_leg_only_pct'], 1)}%",
            f"  trades={g['trades']} taker intents={g['taker_intents']}",
        ]
    lines += ["", "Price-to-beat rule accuracy vs settlement:"]
    if not rep["ptb_rules"]:
        lines.append("  (no settled windows with Chainlink data yet)")
    for r, v in rep["ptb_rules"].items():
        lines.append(f"  {r:18s} {v['correct']}/{v['n']} = {_fmt(v['accuracy_pct'], 1)}%")
    return "\n".join(lines)


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def export_csv(store: Store, rep: dict[str, Any], out_dir: str) -> list[Path]:
    d = Path(out_dir)
    paths = [d / "summary.csv", d / "gaps.csv", d / "windows.csv"]
    write_csv(paths[0], rep["groups"])
    write_csv(paths[1], [dict(r) for r in store.query("SELECT * FROM taker_gaps")])
    write_csv(paths[2], [dict(r) for r in store.query("SELECT * FROM windows")])
    return paths

