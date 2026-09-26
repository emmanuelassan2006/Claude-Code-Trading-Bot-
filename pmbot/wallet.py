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
CLOB_API = "https://clob.polymarket.com"  # public market metadata only
_UPDOWN = re.compile(r"-updown-(5m|15m|1h|4h)-(\d{9,})$")
_DUR_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

Getter = Callable[[str, dict[str, Any]], Any]


def _http_get(url: str, params: dict[str, Any]) -> Any:
    import httpx

    import time

    for attempt in range(4):          # GETs are idempotent; back off on rate limits
        r = httpx.get(url, params=params, timeout=20)
        if r.status_code != 429 or attempt == 3:
            break
        time.sleep(2 ** attempt)
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


def _is_winner_row(x: Any) -> bool:
    return x is True or str(x).lower() == "true"


def _clob_winner(m: Any) -> str | None:
    """CLOB market: tokens[] carry a `winner` flag once the market resolves."""
    if not isinstance(m, dict):
        return None
    for tok in m.get("tokens") or []:
        if isinstance(tok, dict) and _is_winner_row(tok.get("winner")):
            return tok.get("outcome")
    return None


def _first_market(res: Any) -> dict[str, Any] | None:
    if isinstance(res, dict):
        res = res.get("data") or res.get("markets") or [res]
    return res[0] if isinstance(res, list) and res and isinstance(res[0], dict) else None


def fetch_resolutions(markets: dict[str, str | None], get: Getter = _http_get,
                      pause_s: float = 0.15, progress: Callable[[int, int], None] | None = None
                      ) -> tuple[dict[str, str | None], dict[str, Any]]:
    """slug -> winning outcome name (e.g. 'Up'), plus lookup diagnostics.

    `markets` maps slug -> conditionId. Sources are tried in order until one
    gives a winner: gamma /markets?slug, the same with closed=true (gamma can
    hide closed markets by default), gamma /events?slug, and the public CLOB
    market record by conditionId (its tokens carry a `winner` flag).
    Read-only public GETs, throttled; errors are counted, not silently dropped.
    """
    import time

    def sources(slug: str, cid: str | None) -> list[tuple[str, Callable[[], str | None]]]:
        out: list[tuple[str, Callable[[], str | None]]] = [
            ("gamma_slug", lambda: _winner(_first_market(
                get(f"{GAMMA_API}/markets", {"slug": slug})) or {})),
            ("gamma_closed", lambda: _winner(_first_market(
                get(f"{GAMMA_API}/markets", {"slug": slug, "closed": "true"})) or {})),
            ("gamma_event", lambda: _winner(_first_market(
                (_first_market(get(f"{GAMMA_API}/events", {"slug": slug})) or {})
                .get("markets")) or {})),
        ]
        if cid:
            out.append(("clob", lambda: _clob_winner(get(f"{CLOB_API}/markets/{cid}", {}))))
        return out

    winners: dict[str, str | None] = {}
    found: dict[str, int] = defaultdict(int)
    errors: dict[str, str] = {}
    error_counts: dict[str, int] = defaultdict(int)
    order = ["gamma_slug", "gamma_closed", "gamma_event", "clob"]
    dropped: set[str] = set()
    for i, (slug, cid) in enumerate(markets.items()):
        winners[slug] = None
        fns = dict(sources(slug, cid))
        for name in [n for n in order if n in fns and n not in dropped]:
            try:
                w = fns[name]()
            except Exception as e:  # noqa: BLE001 - diagnostics, keep going
                error_counts[name] += 1
                errors.setdefault(name, f"{type(e).__name__}: {e}"[:200])
                if error_counts[name] >= 10 and not found[name]:
                    dropped.add(name)        # consistently failing: stop asking it
                w = None
            if pause_s:
                time.sleep(pause_s)
            if w:
                winners[slug] = w
                found[name] += 1
                order.remove(name)
                order.insert(0, name)        # try what works first next time
                break
        if progress and (i + 1) % 50 == 0:
            progress(i + 1, len(markets))
    diag = {
        "markets": len(markets),
        "resolved": sum(1 for w in winners.values() if w),
        "found_by": dict(found),
        "errors_by_source": dict(error_counts),
        "first_error_by_source": errors,
        "dropped_sources": sorted(dropped),
    }
    return winners, diag


def _winner(m: dict[str, Any]) -> str | None:
    try:
        outcomes = m["outcomes"]
        prices = m["outcomePrices"]
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = [float(p) for p in (json.loads(prices) if isinstance(prices, str) else prices)]
    except (KeyError, ValueError, TypeError):
        return None
    if not prices or len(prices) != len(outcomes):
        return None
    resolved = m.get("closed") or str(m.get("umaResolutionStatus", "")).lower() == "resolved"
    top = max(prices)
    if not resolved or top < 0.99:
        return None
    return outcomes[prices.index(top)]


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


PRICE_BUCKETS = [(0.0, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65),
                 (0.65, 0.80), (0.80, 0.90), (0.90, 1.01)]
PHASES = [(0.0, 0.2, "first 20%"), (0.2, 0.5, "20-50%"), (0.5, 0.8, "50-80%"),
          (0.8, 1.01, "last 20%")]


def _agg(items: list[tuple[float, float, float]]) -> dict[str, Any]:
    """items: (shares, price, payout per share) -> win rate and edge per share."""
    sh = sum(q for q, _, _ in items)
    if sh <= 0:
        return {"fills": len(items), "shares": 0}
    cost = sum(q * p for q, p, _ in items)
    paid = sum(q * w for q, _, w in items)
    return {
        "fills": len(items), "shares": round(sh, 2),
        "avg_price": round(cost / sh, 4),
        "win_rate_pct": round(100.0 * paid / sh, 1),
        "edge_per_share": round((paid - cost) / sh, 4),
        "pnl": round(paid - cost, 2),
    }


def fill_breakdown(trades: list[dict[str, Any]], winners: dict[str, str | None]
                   ) -> dict[str, Any]:
    """Every resolved BUY fill held to resolution: does buying at price p win
    more than p of the time? Split by price, by time in the window, and by role.
    edge_per_share = win rate - average price (before fees/rebates)."""
    by_price: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    by_phase: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    by_role: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    cheap_late: list[tuple[float, float, float]] = []
    for t in trades:
        slug = t.get("slug")
        w = winners.get(slug) if slug else None
        if not w or str(t.get("side", "")).upper() != "BUY":
            continue
        q, p = float(t.get("size", 0)), float(t.get("price", 0))
        item = (q, p, 1.0 if str(t.get("outcome")) == w else 0.0)
        for lo, hi in PRICE_BUCKETS:
            if lo <= p < hi:
                by_price[f"{lo:.2f}-{min(hi, 1.0):.2f}"].append(item)
        by_role[t.get("_role", "?")].append(item)
        win = window_of(slug)
        if win and t.get("timestamp") is not None:
            frac = (float(t["timestamp"]) - win[1]) / (win[2] - win[1])
            for lo, hi, name in PHASES:
                if lo <= frac < hi:
                    by_phase[name].append(item)
            if p <= 0.15 and frac >= 0.5:
                cheap_late.append(item)
    return {
        "by_price": {k: _agg(v) for k, v in sorted(by_price.items())},
        "by_time_in_window": {name: _agg(by_phase[name]) for _, _, name in PHASES
                              if by_phase.get(name)},
        "by_role": {k: _agg(v) for k, v in by_role.items()},
        "cheap_late_buys (<=15c, 2nd half)": _agg(cheap_late),
    }


def analyze_wallet(address: str, out_csv: str, get: Getter = _http_get,
                   pause_s: float = 0.15, progress: Callable[[int, int], None] | None = None
                   ) -> dict[str, Any]:
    trades = fetch_wallet_trades(address, get)
    markets: dict[str, str | None] = {}
    for t in trades:
        if t.get("slug"):
            markets.setdefault(t["slug"], t.get("conditionId"))
    winners, diag = fetch_resolutions(dict(sorted(markets.items())), get, pause_s, progress)
    rows, summary = summarize_wallet(trades, winners)
    both = [r for r in rows if r["pnl"] is not None and r["bought_both_sides"]]
    one = [r for r in rows if r["pnl"] is not None and not r["bought_both_sides"]]
    summary["pnl_both_sides_markets"] = round(sum(r["pnl"] for r in both), 2) if both else None
    summary["pnl_one_side_markets"] = round(sum(r["pnl"] for r in one), 2) if one else None
    summary["resolution_lookup"] = diag
    summary["fills_held_to_resolution"] = fill_breakdown(trades, winners)
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
