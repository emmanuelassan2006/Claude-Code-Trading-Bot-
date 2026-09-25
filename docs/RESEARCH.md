# Polymarket US — Research Findings (pre-Phase 1)

Status: **awaiting owner approval**. No bot code has been written yet.

Sources: the official Python SDK `polymarket-us` 1.0.2 (source read directly from
PyPI; repo `github.com/Polymarket/polymarket-us-python`) and search-indexed
excerpts of `docs.polymarket.us`. The build sandbox's network policy blocks
`*.polymarket.us`, so **nothing here was checked against the live API**. Items
marked **UNVERIFIED** must be confirmed (e.g. with `scripts/probe_markets.py`).

---

## 1. The finding that changes the design: one book per market

Polymarket US is a CFTC DCM with a **single instrument per market** (the YES /
"long" side). There are no separate NO or Down shares:

- Order intents: `ORDER_INTENT_BUY_LONG`, `SELL_LONG`, `BUY_SHORT`, `SELL_SHORT`.
- "Buy Down at q" == "short YES" == an offer on the same book at `1 - q`.
- Positions are **netted**: `netPosition` per market slug.

If a crypto Up/Down window is one market (Up = long, Down = short; this is
**UNVERIFIED**, but it's what the docs say about US markets in general):

| Your spec assumes | What it becomes on a single netted book |
|---|---|
| Taker arb: `ask_up + ask_down + fees < 1` | `ask_down = 1 - bid_up`, so `ask_up + ask_down = 1 + spread ≥ 1 + tick`. **This can never trigger** unless the book is crossed. |
| Maker arb: `bid_up + bid_down ≤ 1 - min_edge` | Bid YES at `b` and offer YES at `1 - bid_down ≥ b + min_edge`. This is **two-sided market making** on one book. The math, leg-risk logic, inventory caps and rebates all still apply. |
| Merge a matched pair for $1 early? | Unnecessary. Long 10 then short 10 nets to 0 immediately, and cash is released at fill. Edge is realized at the second fill, not at resolution. |

If a window turns out to be **two separate markets** (like per-team sports
markets), your spec applies as written, and pairs are held to settlement
(US has no merge/redeem endpoint).

## 2. Market availability — UNVERIFIED

- `polymarket.us/category/crypto` exists and indexes BTC Up/Down at "15 Minutes"
  and "Hourly". Search snippets for **5-minute** markets all point to
  polymarket.com (international: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB).
- I **could not confirm** that Polymarket US lists 5-minute windows or which assets it covers.

## 3. Auth

- Ed25519. Keys are created at `polymarket.us/developer` (key id = UUID, secret = base64 Ed25519 key).
- Headers: `X-PM-Access-Key`, `X-PM-Timestamp` (ms), `X-PM-Signature = b64(sign(timestamp + METHOD + path))`.
- Public REST: `https://gateway.polymarket.us`. Authenticated REST and all WebSockets: `https://api.polymarket.us` / `wss://api.polymarket.us`.
- **Market-data WebSockets need auth.** Phase 1 therefore needs an API key (read-only use).

## 4. Endpoints (SDK 1.0.2)

Public (gateway): `GET /v1/events`, `/v1/events/{id}`, `/v1/events/slug/{slug}`,
`/v1/markets`, `/v1/market/id/{id}`, `/v1/market/slug/{slug}`,
`/v1/markets/{slug}/book`, `/v1/markets/{slug}/bbo`, `/v1/markets/{slug}/settlement`,
`/v1/series`, `/v1/series/id/{id}`, `/v1/search`.

Authenticated (api): `POST /v1/orders`, `GET /v1/orders/open`, `GET /v1/order/{id}`,
`POST /v1/order/{id}/cancel`, `POST /v1/order/{id}/modify`,
`POST /v1/orders/open/cancel` (cancel-all, optional `slugs`), `POST /v1/order/preview`,
`POST /v1/order/close-position`, `GET /v1/portfolio/positions`,
`GET /v1/portfolio/activities`, `GET /v1/account/balances`.

## 5. WebSockets

- `wss://api.polymarket.us/v1/ws/markets`: `SUBSCRIPTION_TYPE_MARKET_DATA` (full book: `bids`/`offers` of `{px, qty}`, `state`, `stats`), `MARKET_DATA_LITE` (BBO + last), `TRADE` (price, qty, time, maker/taker side+intent, no account ids).
- `wss://api.polymarket.us/v1/ws/private`: `ORDER` (executions), `ORDER_SNAPSHOT` (ends with `eof: true`), `POSITION`, `ACCOUNT_BALANCE`.
- Subscribe message: `{"subscribe": {"requestId", "subscriptionType", "marketSlugs"}}`. Heartbeats are sent.
- UNVERIFIED: whether `MARKET_DATA` sends full snapshots every time or deltas. The type suggests full snapshots.

## 6. Rate limits

- Docs excerpt: **20 requests/second per API key, global across endpoints.**
- The API sends **no** `Retry-After` or `X-RateLimit-*` headers, so backoff is client-side.
- `POST` has **no idempotency key**, so order creation must never be auto-retried.
- `modify` replaces an order in one call, so repricing costs one request instead of cancel + new.

## 7. Orders

- Types: `LIMIT`, `MARKET` (with `slippageTolerance`).
- TIF: `GOOD_TILL_CANCEL`, `GOOD_TILL_DATE`, `IMMEDIATE_OR_CANCEL`, `FILL_OR_KILL`.
- **Post-only: `participateDontInitiate: true`.**
- `synchronousExecution`/`maxBlockTime` return fills in the create response.
- `quantity` is an integer number of contracts. Price is `{value: "0.55", currency: "USD"}`.
- States: `NEW`, `PENDING_*`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REPLACED`, `REJECTED`, `EXPIRED`.
- Tick: contract specs allow $0.001–$0.01, prices $0.001–$0.999. The actual tick per market is UNVERIFIED (read it from the book).
- Minimum order size for US is **UNVERIFIED** ("5 shares" is an international figure).
- Position accountability level: $25,000 notional.

## 8. Fees (all go into config)

- Taker: `fee = θ × C × p × (1 − p)` with θ = **0.06**, which matches your belief. Max $1.50 per 100 contracts at p = 0.50.
- Maker: **no fee**, plus a rebate. Sources conflict:
  - "25% of the matched taker fee" implies 0.015 × C × p(1−p).
  - "0.0125 × C × p(1−p)" implies about 20.8%.
  - Both values go in config; the correct one is to be confirmed.
- Rounding: **per fill, to $0.01, banker's rounding (half-to-even).** A multi-fill aggressive order is capped so the sum of fills ≤ round(total exact fee); the adjustment only reduces. Small fills can round to $0.00. Rebates are rounded per fill independently.
- Timing: fees are deducted and rebates credited at fill.
- Orders also carry `commissionsBasisPoints` / `makerCommissionsBasisPoints`. The ledger will reconcile against the `commissionNotionalCollected` returned per execution.
- Worked example: 100 @ 0.50 taker gives 0.06 × 100 × 0.25 = **$1.50**; 1 @ 0.50 gives $0.015, which rounds to **$0.02**.

## 9. Settlement

- Automatic cash settlement by Polymarket Clearing ($1 / $0). No redeem call.
- `GET /v1/markets/{slug}/settlement`; activity type `ACTIVITY_TYPE_POSITION_RESOLUTION`.
- Timing is "shortly after the resolution source confirms" (not specified precisely).

## 10. Price to beat and the Chainlink feed

- There is no API field for the price to beat, even on international. It has to be derived from the resolution source.
- **Resolution changed on international Aug 7, 2026:** 5m/15m/4h Up/Down settle on a **Chainlink TWAP** (30 s window for 5m, 60 s for 15m/4h). Both open and close come from the TWAP feed. Whether US uses the same rule is **UNVERIFIED**. Each market's `description` should state it, and the bot will log it per window.
- Feed options:
  - **Chainlink Data Streams** directly. Needs Chainlink credentials.
  - **polymarket.com RTDS** WebSocket, which relays the Chainlink streams publicly. It is read-only, but it is international infrastructure.
  - An on-chain Chainlink aggregator. This is a different product with coarser updates, so a signal only.

## 11. Wallet-pattern analysis

Polymarket US is a centralized exchange. There are **no public wallets or
per-account trade histories**, and the public `TRADE` stream is anonymous. The
tool as specified cannot be built for US accounts. Alternatives:
- (a) Analyze the anonymous US tape: timing, sizes, and aggressor side per window.
- (b) Analyze a public **polymarket.com** wallet via its public data API. This is read-only, but it's the international venue.

---

## Proposed structure (pending approval)

```
pmbot/
  config.py        # pydantic-free dataclass config from config.toml + .env (keys never logged)
  client.py        # thin wrapper over polymarket-us SDK; rate limiter; NO trading in Phase 1
  discovery.py     # window discovery (series/events), schedule of opens/closes
  book.py          # local order book per market; Up/Down views derived from one or two books
  feeds/chainlink.py  # price-to-beat + live price (pluggable source)
  fees.py          # single fee/rebate function, banker's rounding, multi-fill cap
  monitor.py       # Phase 1: gap/spread/depth/duration logging
  strategies/{maker_pair.py, taker_pair.py, directional.py}
  risk.py          # budgets, exposure, unhedged caps, P&L locks, cutoff, kill switch
  execution/{simulator.py, live.py}   # one interface; live gated by config + --live
  ledger.py        # SQLite
  reports.py, notify.py (Telegram), wallet_analysis.py
  cli.py           # monitor | run | report | kill | analyze
tests/             # mocked, no network
deploy/            # systemd unit + logrotate
```
Dependencies: `polymarket-us` (httpx, pynacl, websockets), `python-dotenv`, `pytest`. SQLite and logging come from the standard library.
