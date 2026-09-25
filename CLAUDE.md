# CLAUDE.md — pmbot

Personal trading bot for **Polymarket US** (CFTC DCM, `*.polymarket.us`,
Ed25519 API keys via the official `polymarket-us` SDK). This is **not**
polymarket.com, so do not use Polygon wallets, private keys, USDC, the CTF, the
CLOB client or Gamma patterns for trading. (`pmbot/wallet.py` and the Chainlink
RTDS relay read polymarket.com *public data* only.)

## Phase status
- Phase 1 (monitor + research): **built, awaiting owner review.**
- Phase 2 (strategies, risk engine, sizing, dry-run simulator, ledger, Telegram): not started.
- Phase 3 (live order submission): not started.
- Stop for owner review after each phase.

## Architecture
- `config.py`: all tunables live in `config.toml`; secrets live only in `.env` and are wrapped in `Secret`.
- `fees.py`: the **only** fee function. Every edge calculation and the simulator must use it.
- `book.py`: books and Up/Down views. A window is "single" (one YES book; Down = short) or "pair" (two books).
- `edge.py`: taker pair quote and maker pair edge.
- `discovery.py`, `marketdata.py`, `feeds/chainlink.py`, then `monitor.py` (evaluates on every book update), which writes to `store.py` (SQLite, `mode` column).
- `reports.py`, `wallet.py`, `cli.py`.

## Safety rules (non-negotiable)
1. **Never place a real order during development or testing.** If it seems necessary, ask the owner first.
2. Phase 1 code has **no trading or private endpoints**. `tests/test_safety.py` enforces this by grepping; don't weaken the test to add them before Phase 3.
3. Live trading will require **both** `dry_run = false` in config **and** a `--live` CLI flag. Dry-run is the default. Dry-run and live share one code path; only final submission differs.
4. Secrets only in `.env` (git-ignored). Never print, log, commit, or ask the owner to paste keys. Logging redacts secret values.
5. Order-creating `POST`s are never auto-retried: the API has no idempotency key. Only idempotent GET/DELETE may be retried.
6. Respect rate limits: documented 20 req/s per key globally. The client default is 10/s token bucket. Prefer WebSockets; poll only as fallback.
7. The risk engine (Phase 2) sits outside the strategies and they cannot override it:
   - per-window budget ($20), total exposure ($60), and unhedged caps;
   - P&L locks and a daily loss limit ($20) that halts trading until the next day;
   - no entries in the final 30 s, and all resting orders cancelled at the cutoff;
   - order/cancel rate caps;
   - a kill switch (`KILL` file / `pmbot kill`) that cancels everything and stops;
   - reconcile orders and positions on startup and reconnect.
8. Everything unverified against the live API is marked UNVERIFIED in `docs/RESEARCH.md`. Don't silently assume it; make it configurable or detect it at runtime.

## Dev
`pip install -e ".[dev]" && pytest`. Tests are offline, with mocked API responses. Python 3.11+.
