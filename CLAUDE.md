# CLAUDE.md — pmbot

Personal trading bot for **Polymarket US** (CFTC DCM, `*.polymarket.us`,
Ed25519 API keys via the official `polymarket-us` SDK). This is **not**
polymarket.com, so do not use Polygon wallets, private keys, USDC, the CTF, the
CLOB client or Gamma patterns for trading. (`pmbot/wallet.py` and the Chainlink
RTDS relay read polymarket.com *public data* only.)

## Phase status
- Phase 1 (monitor + research): built. Live probe confirmed that US lists **BTC 15m and 60m
  Up/Down only, one market per window** (no 5m, no other assets).
- Phase 2: strategy (`docs/STRATEGY.md`), risk engine, sizing, paper exchange, ledger,
  backtester and reports are **built and awaiting owner review**. Telegram is not built yet.
- Phase 3 (live order submission, reconciliation): not started.
- Stop for owner review after each phase.

## Architecture
- `config.py`: all tunables live in `config.toml`; secrets live only in `.env` and are wrapped in `Secret`.
- `fees.py`: the **only** fee function. Every edge calculation and the simulator must use it.
- `book.py`: books and Up/Down views. A window is "single" (one YES book; Down = short) or "pair" (two books).
- `edge.py`: taker pair quote and maker pair edge.
- `discovery.py`, `marketdata.py`, `feeds/chainlink.py`, then `monitor.py` (evaluates on every book update), which writes to `store.py` (SQLite, `mode` column).
- `model.py` (fair value), `strategy.py` (proposes quotes and takes), `risk.py` (decides), `sim.py`
  (paper exchange), `engine.py` (wires them; the same code path for paper/backtest/live), `backtest.py`.
- `reports.py`, `wallet.py`, `cli.py`.
- The engine works in "Up" terms (buy Up / sell Up). Live execution must translate to
  intents: buy with a short position = SELL_SHORT, otherwise BUY_LONG; sell with a long position =
  SELL_LONG, otherwise BUY_SHORT. Split orders that cross zero.

## Safety rules (non-negotiable)
1. **Never place a real order during development or testing.** If it seems necessary, ask the owner first.
2. Until Phase 3 is approved, the code has **no trading or private endpoints**. `tests/test_safety.py` enforces this by grepping; don't weaken the test to add them before Phase 3.
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
