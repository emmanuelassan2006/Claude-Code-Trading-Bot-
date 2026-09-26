"""Read-only probe of Polymarket US public market data (diagnostic version).

Answers the open research questions that could not be verified from the
build sandbox:
  - which crypto short-duration series/windows exist and how they are titled
  - whether a window is ONE market (Up = long/YES) or TWO markets (Up, Down)
  - what the market description says about the price to beat / resolution
  - what a live book looks like (tick size, depth)

Prints every request's status, then what exists BEFORE any filtering, so a
wording mismatch can't hide results. Unauthenticated GETs to the public
gateway only: no API keys, no orders, no trading endpoints.

    python scripts/probe_markets.py            # writes probe_output.json
"""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

GATEWAY = "https://gateway.polymarket.us"
CRYPTO_WORDS = ("bitcoin", "btc", "ethereum", "eth ", "solana", "xrp", "crypto",
                "up or down", "updown", "up/down")


def ssl_context():
    try:
        import certifi  # installed with the SDK; fixes macOS python.org cert errors

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


CTX = ssl_context()
LOG = []


def get(path, params=None):
    url = GATEWAY + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "pm-us-probe/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            data = json.loads(r.read() or b"{}")
            status = f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors="replace")
        data, status = {"_error": f"HTTP {e.code}: {body}"}, f"HTTP {e.code}"
    except Exception as e:
        data, status = {"_error": f"{type(e).__name__}: {e}"}, "FAILED"
    n = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    line = f"  {status:9s} {path} {params or ''} -> {n or data.get('_error', '')}"
    print(line)
    LOG.append(line)
    return data


def cryptoish(obj):
    t = json.dumps(obj).lower()
    return any(w in t for w in CRYPTO_WORDS)


def brief(e):
    mk = [(m.get("slug"), m.get("outcome"), m.get("title")) for m in e.get("markets") or []]
    return (f"    - {e.get('title')!r} slug={e.get('slug')} "
            f"{e.get('startTime')} -> {e.get('endTime')} "
            f"series={(e.get('series') or {}).get('slug')} markets={mk}")


def main():
    out = {}
    print("1) Requests")
    series = get("/v1/series", {"limit": 200})
    events_pages = []
    for offset in (0, 100, 200, 300, 400):
        page = get("/v1/events", {"limit": 100, "offset": offset, "active": "true",
                                  "closed": "false"})
        events_pages += page.get("events", []) or []
        if len(page.get("events", []) or []) < 100:
            break
    searches = {}
    for q in ("bitcoin", "btc", "up or down", "crypto", "ethereum", "solana", "xrp"):
        searches[q] = get("/v1/search", {"query": q, "limit": 50}).get("events", []) or []
    markets = get("/v1/markets", {"limit": 200, "active": "true", "closed": "false"})

    all_series = series.get("series", []) or []
    all_events = {e.get("slug"): e for e in events_pages if e.get("slug")}
    for evs in searches.values():
        for e in evs:
            if e.get("slug"):
                all_events.setdefault(e["slug"], e)
    crypto_series = [s for s in all_series if cryptoish(s)]
    crypto_events = [e for e in all_events.values() if cryptoish(e)]
    crypto_markets = [m for m in markets.get("markets", []) or [] if cryptoish(m)]

    print(f"\n2) Series: {len(all_series)} total, {len(crypto_series)} crypto-looking")
    for s in crypto_series[:40]:
        print(f"    - {s.get('title')!r} slug={s.get('slug')} recurrence={s.get('recurrence')} "
              f"active={s.get('active')}")
    if not crypto_series:
        for s in all_series[:15]:
            print(f"    (sample) {s.get('title')!r} slug={s.get('slug')} "
                  f"recurrence={s.get('recurrence')}")

    print(f"\n3) Events: {len(all_events)} total, {len(crypto_events)} crypto-looking")
    for e in sorted(crypto_events, key=lambda e: str(e.get("endTime")))[:40]:
        print(brief(e))
    if not crypto_events:
        for e in list(all_events.values())[:15]:
            print("    (sample)" + brief(e)[5:])

    print(f"\n4) Markets (active): {len(markets.get('markets', []) or [])} total, "
          f"{len(crypto_markets)} crypto-looking")
    for m in crypto_markets[:20]:
        print(f"    - {m.get('title')!r} slug={m.get('slug')} outcome={m.get('outcome')} "
              f"event={m.get('eventSlug')}")

    updown = [e for e in crypto_events
              if "up or down" in f"{e.get('title')} {e.get('slug')}".lower()
              or "updown" in str(e.get("slug"))]
    extra = get("/v1/search", {"query": "BTC Up or Down", "limit": 50}).get("events", []) or []
    for e in extra:
        if e.get("slug") and e["slug"] not in {u.get("slug") for u in updown}:
            updown.append(e)
    print(f"\n5) Up/Down windows found: {len(updown)}")
    for e in updown:
        print(brief(e))
    out["updown_events"] = updown

    for e in updown[:2]:
        mk = next((m for m in e.get("markets") or [] if m.get("slug")), None)
        if not mk:
            continue
        slug = mk["slug"]
        print(f"\n6) Deep-dive on {slug}")
        market = get(f"/v1/market/slug/{slug}")
        book = get(f"/v1/markets/{slug}/book")
        bbo = get(f"/v1/markets/{slug}/bbo")
        out.setdefault("updown_deep_dive", {})[slug] = {"market": market, "book": book, "bbo": bbo}
        m = market.get("market") or {}
        print("    market fields:", {k: v for k, v in m.items() if k != "description"})
        print("    description:", (m.get("description") or "(none)").replace("\n", " "))
        md = book.get("marketData") or {}
        bids = [(b.get("px", {}).get("value"), b.get("qty")) for b in (md.get("bids") or [])[:5]]
        offers = [(o.get("px", {}).get("value"), o.get("qty")) for o in (md.get("offers") or [])[:5]]
        print(f"    state={md.get('state')} bids(top5)={bids}")
        print(f"    offers(top5)={offers}")
        print(f"    bbo={bbo.get('marketData')}")

    out.update({
        "request_log": LOG,
        "crypto_series": crypto_series,
        "crypto_events": crypto_events[:60],
        "crypto_markets": crypto_markets[:60],
        "sample_series_titles": [s.get("title") for s in all_series[:100]],
        "sample_event_titles": [e.get("title") for e in list(all_events.values())[:200]],
    })
    with open("probe_output.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    errors = [l for l in LOG if "HTTP 2" not in l]
    print(f"\n{len(errors)} of {len(LOG)} requests failed." if errors else
          f"\nAll {len(LOG)} requests succeeded.")
    print("Full output written to probe_output.json (public data only; safe to share).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
