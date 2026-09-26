# pmbot — Polymarket US crypto Up/Down monitor (→ arbitrage bot)

Personal bot for **Polymarket US** (the CFTC-regulated exchange, `polymarket.us`),
not polymarket.com. Target: short-duration crypto "Up or Down" windows.

**Current status: monitor + strategy with paper trading and backtesting.
This code cannot place, modify or cancel real orders. There are no trading
endpoints in it at all.** Live order submission (Phase 3) comes after review.

What Polymarket US actually lists (confirmed 2026-09-26): **BTC Up/Down, 15-minute
and 60-minute windows only**, each a single market. They settle on CF Benchmarks' BRTI (the average of 60 prices
in the minute before the start and before the end). The API publishes the price to beat, and the taker fee
coefficient is 0.0695. The strategy is
fair-value market making plus opportunistic taking; see **`docs/STRATEGY.md`**.

Read `docs/RESEARCH.md` first: it covers the API, fees, and one important
structural finding. Polymarket US has **one order book per market**. If a
window is one market, "Down" is the short side of "Up", so
`ask_up + ask_down = 1 + spread`. Taker "arbitrage" then can't exist, and
maker "arbitrage" is two-sided market making. The monitor detects which
layout each window uses and measures both.

## Setup

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml     # optional; defaults are built in
cp .env.example .env                   # then fill in your key locally
pytest                                 # all tests are offline
```

`.env` (git-ignored) holds `POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY` from
<https://polymarket.us/developer>. Polymarket US requires auth even for the
market-data WebSocket, so Phase 1 needs a key. Phase 1 only uses it to open that
socket. Without a key, the monitor falls back to REST polling of public books.
Secrets are never logged: the `Secret` wrapper masks them and a log filter
redacts them.

First, confirm what's listed (public, read-only, no key):

```bash
python scripts/probe_markets.py        # writes probe_output.json (no secrets)
```

## Commands

| Command | What it does |
|---|---|
| `pmbot monitor [-v]` | Discover windows, subscribe to books + trades, stream the BRTI-proxy BTC price (Coinbase + Kraken), record everything to SQLite. |
| `pmbot run` | **Paper-trade** the strategy on live data (monitor + strategy + risk engine + simulated exchange). Logs a `SIZING` line before every simulated order. |
| `pmbot run --live` | Refused: it requires `dry_run = false` in config **and** this flag, and live submission isn't built yet. |
| `pmbot backtest [--since 24h] [--out file.db]` | Replay recorded monitor data through the exact same strategy and risk engine. |
| `pmbot calibrate [--since 24h]` | Is the model a better predictor than the market? Brier score, log loss, and who was right when they disagreed. **Check this before trusting any edge.** |
| `pmbot leadlag [--lookback 5] [--horizon 10]` | Does the book lag BTC moves? Cross-correlation by delay, plus simulated taker P&L (real ask/bid + fee) when the book hasn't caught up. No trading. |
| `pmbot longshot [--since 24h]` | Do cheap (≤15¢) or late contracts win more often than their price? Win rate vs price by checkpoint, edge after the taker fee. No trading. |
| `pmbot ladder [--levels 0.05,0.15,...] [--shares 1] [--place-until S]` | Replay a two-sided resting-bid ladder (the 98euf98a wallet pattern) on the recorded US tape: fills on trade-through, held to resolution. No trading. |
| `pmbot favorite [--range 0.85,0.97] [--secs-left 120,60] [--shares 20] [--out file.csv]` | Buy the favourite once per window late, at the ask plus the rounded taker fee, and hold to resolution. Sweeps price ranges and entry times. Shows the loss rate vs break-even, a 95% worst-case loss rate, t-stat, and older-vs-newer half. No trading. |
| `pmbot report --strategy [--since 24h]` | Paper-trading P&L by component, market and day; fees, rebates, markouts (adverse selection), carried inventory, drawdown. |
| `pmbot report [--since 24h] [--min-edge 0.02] [--horizon 60] [--csv DIR] [--json]` | Gap/spread/fill-proxy/tape stats per asset and duration, plus price-to-beat rule accuracy. |
| `pmbot analyze-tape [--out reports/tape.csv]` | Summarize the anonymous US trade tape. |
| `pmbot analyze-wallet 0xADDR [--out reports/wallet.csv]` | Read-only analysis of a **public polymarket.com** wallet (US accounts are not public): per-market P&L, plus win rate vs price, time in window and maker/taker for every fill held to resolution. `resolution_lookup` shows which sources answered and any errors. |
| `pmbot kill` | Create the `KILL` file. A running monitor stops within 1 s, and nothing starts while it exists. |

### What the monitor records

- **Taker gaps.** An episode is logged every time `ask_up + ask_down + taker fees < 1 - min_gap`. Each episode records max gap, executable size and profit (both ladders walked, rounded fees), depth on both sides, seconds to close, and how long it lasted (ms).
- **Book samples** (1/s per window by default): best bid/ask and size on both sides, taker pair cost, maker edge `1 - bid_up - bid_down`, reference BTC price, and price to beat.
- **Maker fill proxy** (in the report). For each moment the maker edge ≥ `--min-edge`, a quote joining both best bids counts as filled only if a later trade prints *through* the price within `--horizon` seconds. This is a conservative queue assumption. The report shows both-legs vs one-leg-only rates; one-leg-only is the leg risk.
- **Trades** (the public tape).
- **Price to beat and settlement.** Read from the API (`assetPriceTerms.priceToBeat` and `settlementPrice`). The monitor also computes our own 60-second average at each boundary from the price feed, and the report shows the basis versus the official values.

## Configuration

Everything tunable is in `config.toml`. `config.example.toml` lists every key
with its default, including fee rate, rebate rate, rounding, tick, minimum
size, assets, durations, rate limits, and the TWAP windows. Unknown keys are
rejected.

## Windows

Same steps in **PowerShell**, with these differences:
- Install Python 3.11+ from python.org and tick **"Add python.exe to PATH"**. Install Git for Windows.
- Create the venv with `py -m venv .venv` and activate it with `.venv\Scripts\Activate.ps1`. If PowerShell blocks
  the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
- Create `.env` with `copy .env.example .env`, then `notepad .env`.
- Stop with **Ctrl+C** (clean shutdown) or `pmbot kill` from a second window. `del KILL` allows a restart.
- Keep the PC awake while it runs: Settings → System → Power → Sleep = Never.

## Running 24/7

- **Linux VPS:** `deploy/pmbot-monitor.service` (systemd; restarts on failure, but not while `KILL` exists).
- **Laptop:** `nohup pmbot monitor >/dev/null 2>&1 &` or tmux/screen.
- **Logs:** `logs/monitor.log`, size-rotated by the app (20 MB × 10 by default).
- **Data:** `data/pmbot.db` (SQLite, WAL mode).

## Kill switch

`pmbot kill` (or `touch KILL`) cancels all (simulated) open orders and stops the
process within 1 s. Delete `KILL` to allow a restart.

## Layout

```
pmbot/config.py      config.toml + .env (Secret wrapper)
pmbot/fees.py        THE fee function (taker/maker/rebate, banker's rounding, multi-fill cap)
pmbot/book.py        local books; Up/Down views for single- or two-market windows
pmbot/edge.py        taker pair quote (walks both ladders), maker pair edge
pmbot/discovery.py   window discovery + classification (asset, duration, layout)
pmbot/marketdata.py  read-only gateway REST + markets WebSocket (reconnect, REST fallback)
pmbot/feeds/exchanges.py  Coinbase + Kraken composite (BRTI proxy)
pmbot/feeds/history.py    price history, TWAP, candidate references
pmbot/feeds/chainlink.py  optional Chainlink relay (not the settlement source)
pmbot/monitor.py     Phase 1 engine (evaluates on every book update)
pmbot/store.py       SQLite
pmbot/model.py       fair value P(Up) (digital option, TWAP-aware) + realized vol
pmbot/strategy.py    fair-value quoting + taking (proposes only)
pmbot/risk.py        risk engine (worst-case exposure, caps, cutoff, halts, locks, rates)
pmbot/sim.py         paper exchange (latency, post-only, trade-through fills, IOC)
pmbot/engine.py      strategy -> risk -> executor -> ledger; shared by paper/backtest/live
pmbot/backtest.py    replay recorded data through the engine
pmbot/reports.py     reports + CSV export
pmbot/wallet.py      tape + public-wallet analysis
pmbot/patterns.py    longshot check + bid-ladder replay on recorded US data
pmbot/cli.py         entry point
```
