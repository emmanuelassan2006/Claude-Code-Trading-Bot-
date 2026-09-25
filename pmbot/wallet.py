"""Research tools (no trading).

Polymarket US is a centralized exchange: there are no public accounts or
per-account trade histories, and its public trade stream is anonymous. So:

* `analyze_tape`   — summarizes the anonymous US trade tape recorded by the
                     monitor (entry timing within windows, prices, sizes,
                     aggressor intent).
* `analyze_wallet` — pulls a PUBLIC polymarket.com (international) wallet's
                     trade history from its public data API, read-only, and
                     summarizes the same things plus both-sides buying, maker vs
                     taker, win rate and P&L. Useful for studying strategies;
                     it is a different venue from the one this bot trades.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from pmbot.reports import write_csv
from pmbot.store import Store

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
_UPDOWN = re.compile(r"-updown-(5m|15m|1h|4h)-(\d{9,})$")
_DUR_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

Getter = Callable[[str, dict[str, Any]], Any]


def _http_get(url: str, params: dict[str, Any]) -> Any:
    import httpx

    r = httpx.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def window_of(slug: str) -> tuple[str, float, float] | None:
    """(duration, start_ts, end_ts) for polymarket.com up/down slugs."""
    m = _UPDOWN.search(slug or "")
    if not m:
        return None
    start = float(m.group(2))
    return m.group(1), start, start + _DUR_S[m.group(1)]


def fetch_wallet_trades(address: str, get: Getter = _http_get, page: int = 500,
                        max_rows: int = 10000) -> list[dict[str, Any]]:
    """All trades (maker and taker) plus a set of taker-only trade keys."""
    def pull(taker_only: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < max_rows:
            batch = get(f"{DATA_API}/trades", {
                "user": address, "limit": page, "offset": offset,
                "takerOnly": str(taker_only).lower(),
            })
            if not batch:
                break
            rows += batch
            if len(batch) < page:
                break
            offset += page
        return rows

    all_rows = pull(False)
    taker_keys = {_trade_key(t) for t in pull(True)}
    for t in all_rows:
        t["_role"] = "taker" if _trade_key(t) in taker_keys else "maker"
    return all_rows


def _trade_key(t: dict[str, Any]) -> tuple[Any, ...]:
    return (t.get("transactionHash"), t.get("asset"), t.get("size"), t.get("price"), t.get("side"))


def fetch_resolutions(slugs: list[str], get: Getter = _http_get) -> dict[str, str | None]:
    """slug -> winning outcome name (e.g. 'Up') for closed markets."""
    out: dict[str, str | None] = {}
    for slug in slugs:
        try:
            res = get(f"{GAMMA_API}/markets", {"slug": slug})
        except Exception:
            out[slug] = None
            continue
        m = res[0] if isinstance(res, list) and res else None
        out[slug] = _winner(m) if m else None
    return out


def _winner(m: dict[str, Any]) -> str | None:
    try:
        outcomes = m["outcomes"]
        prices = m["outcomePrices"]
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = [float(p) for p in (json.loads(prices) if isinstance(prices, str) else prices)]
    except (KeyError, ValueError, TypeError):
        return None
    if not m.get("closed") or 1.0 not in prices:
        return None
    return outcomes[prices.index(1.0)]


def summarize_wallet(trades: list[dict[str, Any]], winners: dict[str, str | None]
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Per-market rows + overall summary."""
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_market[t.get("slug") or t.get("conditionId") or "?"].append(t)

    rows: list[dict[str, Any]] = []
    for slug, ts in by_market.items():
        win = window_of(slug)
        shares: dict[str, float] = defaultdict(float)
        cost: dict[str, float] = defaultdict(float)
        bought: dict[str, float] = defaultdict(float)
        cash = 0.0
        roles: dict[str, int] = defaultdict(int)
        entries = []
        for t in ts:
            size, price = float(t.get("size", 0)), float(t.get("price", 0))
            outcome = str(t.get("outcome", "?"))
            sign = 1 if str(t.get("side", "")).upper() == "BUY" else -1
            shares[outcome] += sign * size
            cash -= sign * size * price
            if sign > 0:
                cost[outcome] += size * price
                bought[outcome] += size
            roles[t.get("_role", "?")] += 1
            if win and t.get("timestamp") is not None:
                entries.append(float(t["timestamp"]) - win[1])
        avg = {o: cost[o] / bought[o] for o in bought if bought[o] > 0}
        both = len(avg) >= 2
        winner = winners.get(slug)
        pnl = None
        if winner is not None:
            pnl = cash + max(0.0, shares.get(winner, 0.0))
        outcomes = sorted(avg)
        rows.append({
            "market": slug,
            "title": ts[0].get("title"),
            "duration": win[0] if win else None,
            "trades": len(ts),
            "first_entry_s": min(entries) if entries else None,
            "median_entry_s": statistics.median(entries) if entries else None,
            "outcomes_bought": ",".join(outcomes),
            "avg_buy_prices": json.dumps({o: round(p, 4) for o, p in avg.items()}),
            "bought_both_sides": both,
            "combined_avg_price": round(sum(avg.values()), 4) if both else None,
            "paired_shares": min(bought.values()) if both else 0.0,
            "volume_usd": round(sum(cost.values()), 2),
            "maker_fills": roles.get("maker", 0),
            "taker_fills": roles.get("taker", 0),
            "winner": winner,
            "pnl": round(pnl, 4) if pnl is not None else None,
        })

    resolved = [r for r in rows if r["pnl"] is not None]
    sizes = [float(t.get("size", 0)) for t in trades]
    prices = [float(t.get("price", 0)) for t in trades]
    entries = [r["median_entry_s"] for r in rows if r["median_entry_s"] is not None]
    both_rows = [r for r in rows if r["bought_both_sides"]]
    total_fills = sum(r["maker_fills"] + r["taker_fills"] for r in rows) or 1
    summary = {
        "markets": len(rows),
        "trades": len(trades),
        "updown_markets": sum(1 for r in rows if r["duration"]),
        "median_trade_size": statistics.median(sizes) if sizes else None,
        "median_trade_price": statistics.median(prices) if prices else None,
        "median_entry_secs_into_window": statistics.median(entries) if entries else None,
        "both_sides_pct": 100.0 * len(both_rows) / len(rows) if rows else None,
        "both_sides_median_combined_price": (
            statistics.median([r["combined_avg_price"] for r in both_rows]) if both_rows else None),
        "maker_fill_pct": 100.0 * sum(r["maker_fills"] for r in rows) / total_fills,
        "resolved_markets": len(resolved),
        "win_rate_pct": (100.0 * sum(1 for r in resolved if r["pnl"] > 0) / len(resolved)
                         if resolved else None),
        "total_pnl": round(sum(r["pnl"] for r in resolved), 2) if resolved else None,
        "note": "P&L = trade cash flows + $1 per winning share held; ignores fees/rebates.",
    }
    return rows, summary


def analyze_wallet(address: str, out_csv: str, get: Getter = _http_get
                   ) -> dict[str, Any]:
    trades = fetch_wallet_trades(address, get)
    slugs = sorted({t["slug"] for t in trades if t.get("slug")})
    rows, summary = summarize_wallet(trades, fetch_resolutions(slugs, get))
    write_csv(Path(out_csv), rows)
    return summary


def analyze_tape(store: Store, out_csv: str) -> dict[str, Any]:
    """Summarize the anonymous US trade tape recorded by the monitor."""
    rows = []
    for w in store.query("SELECT * FROM windows ORDER BY start_ts"):
        trades = [dict(t) for t in store.query(
            "SELECT * FROM trades WHERE window_key=? ORDER BY ts", [w["key"]])]
        if not trades:
            continue
        span = max(1.0, w["end_ts"] - w["start_ts"])
        fracs = [t["secs_into_window"] / span for t in trades if t["secs_into_window"] is not None]
        qtys = [t["qty"] for t in trades if t["qty"] is not None]
        intents: dict[str, int] = defaultdict(int)
        for t in trades:
            intents[t["taker_intent"] or "?"] += 1
        rows.append({
            "window": w["key"], "asset": w["asset"], "duration": w["duration"],
            "trades": len(trades),
            "volume": sum(qtys),
            "median_qty": statistics.median(qtys) if qtys else None,
            "max_qty": max(qtys) if qtys else None,
            "pct_in_first_third": 100.0 * sum(f < 1 / 3 for f in fracs) / len(fracs) if fracs else None,
            "pct_in_last_third": 100.0 * sum(f >= 2 / 3 for f in fracs) / len(fracs) if fracs else None,
            "first_price": trades[0]["price"], "last_price": trades[-1]["price"],
            "taker_intents": json.dumps(dict(intents)),
            "outcome": w["outcome"],
        })
    write_csv(Path(out_csv), rows)
    return {
        "windows_with_trades": len(rows),
        "trades": sum(r["trades"] for r in rows),
        "median_trades_per_window": statistics.median([r["trades"] for r in rows]) if rows else None,
        "median_qty": statistics.median([r["median_qty"] for r in rows if r["median_qty"]])
        if rows else None,
    }
