"""Read-only probe of Polymarket US public market data.

Answers the open research questions that could not be verified from the
build sandbox (which cannot reach *.polymarket.us):
  - which crypto Up/Down series/windows exist (5m? 15m? hourly? which assets)
  - whether a window is ONE market (Up = long/YES) or TWO markets (Up, Down)
  - what the market description says about the price to beat / resolution
  - what a live book looks like (tick size, depth)

Uses only unauthenticated GET requests to the public gateway. No API keys,
no orders, no trading endpoints. Standard library only.

    python scripts/probe_markets.py            # writes probe_output.json
"""

import json
import sys
import urllib.parse
import urllib.request

GATEWAY = "https://gateway.polymarket.us"
KEYWORDS = ("up or down", "updown", "up-or-down", "5m", "15m", "hourly")


def get(path, params=None):
    url = GATEWAY + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "pm-us-probe/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # report and keep going
        return {"_error": f"{type(e).__name__}: {e}", "_url": url}


def matches(obj):
    text = json.dumps(obj).lower()
    return any(k in text for k in KEYWORDS) and any(
        a in text for a in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp")
    )


def main():
    out = {}
    series = get("/v1/series", {"limit": 200, "active": "true"})
    out["series_crypto_updown"] = [s for s in series.get("series", []) if matches(s)]
    out["series_error"] = series.get("_error")

    found = get("/v1/search", {"query": "up or down", "limit": 50, "status": "active"})
    events = [e for e in found.get("events", []) if matches(e)]
    out["search_events"] = events[:20]
    out["search_error"] = found.get("_error")

    ev = get("/v1/events", {"limit": 200, "active": "true", "categories": "crypto"})
    out["active_crypto_events"] = [e for e in ev.get("events", []) if matches(e)][:20]
    out["events_error"] = ev.get("_error")

    # Deep-dive one market: detail (description = resolution rules), book, bbo.
    sample = next(
        (m for e in events + out["active_crypto_events"] for m in e.get("markets", [])), None
    )
    if sample and sample.get("slug"):
        slug = sample["slug"]
        out["sample_market"] = get(f"/v1/market/slug/{slug}")
        out["sample_book"] = get(f"/v1/markets/{slug}/book")
        out["sample_bbo"] = get(f"/v1/markets/{slug}/bbo")

    with open("probe_output.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"crypto up/down series: {len(out['series_crypto_updown'])}")
    for s in out["series_crypto_updown"]:
        print(f"  - {s.get('title')} | slug={s.get('slug')} | recurrence={s.get('recurrence')}")
    print(f"matching events: {len(events)} (search) / {len(out['active_crypto_events'])} (events)")
    for e in (events or out["active_crypto_events"])[:10]:
        mk = [(m.get("slug"), m.get("outcome")) for m in e.get("markets", [])]
        print(f"  - {e.get('title')} | {e.get('startTime')} -> {e.get('endTime')} | markets={mk}")
    print("full output written to probe_output.json (contains no secrets; safe to share)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
