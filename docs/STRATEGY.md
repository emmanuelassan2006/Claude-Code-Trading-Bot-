# Strategy: fair-value market making + opportunistic taking

## Why not arbitrage

Polymarket US lists BTC Up/Down in 15-minute and 60-minute windows. Each
window is **one market** (one YES book), so Up and Down share a single order
book. Down's ask is always `1 - Up's bid`, so `ask_up + ask_down = 1 + spread > 1`.
Taker arbitrage cannot exist. "Maker arbitrage" (a bid on Up plus a bid on
Down) is really a bid and an offer on the same book, i.e. market making.
Any edge must come from **pricing the contract better than the book does**.

## The fair value

Each window is a cash-or-nothing digital option: Up pays $1 if the settlement
price ≥ the price to beat K. Over minutes, BTC is close to driftless Brownian
motion, so

    P(Up) = Φ((E[settle] − K) / sd)

Here `sd` comes from realized volatility of the Chainlink stream (30-minute
lookback, clipped to 20%–250% annualized). If settlement is a TWAP over the
final `w` seconds (polymarket.com uses 60 s for 15m windows; **unverified for
US**, so it's configurable), the variance of the average is used instead:
`σ²S²(τ − 2w/3)` before the averaging window, and
`σ²S²τ³/(3w²)` around the already-realized part inside it
(`pmbot/model.py`).

The model returns a **band** [low, high], not a point. The band covers
volatility error (±25%), feed lag/basis (±3 bps), and every candidate
price-to-beat rule until the monitor proves which one Polymarket US uses. It
also returns `p_sd`, how far P(Up) typically moves over 5 s.

## Maker quotes (main component)

- Bid = `low − margin − half`, Ask = `high + margin + half`. Here
  `half = max(1¢, p_sd)`: the quote is at least one 5-second fair-value move
  away. Near-the-money with 10 minutes left, P(Up) moves ~1.6¢ per second, so
  fixed tight quotes get picked off.
- **Inventory skew:** both quotes shift down 0.1¢ per Up share held (up when
  short), so fills tend to flatten the position.
- **Post-only:** never crosses the book. Makers pay no fee and earn the rebate.
- **Hysteresis:**
  - A resting quote stays while its edge versus the band is ≥ 0.5¢.
  - Below that it's repriced or pulled immediately.
  - It's only moved *closer* when that gains ≥ 2 ticks.
  - This cut simulated order traffic by ~60% without keeping stale quotes.
- **No quotes:** in the first 5 s, in the last 60 s (15m) / 90 s (1h), outside 5¢–95¢,
  when the price feed is > 5 s stale, or before the price to beat is known.

## Taker (secondary component)

When the book is clearly through the band after the taker fee (`0.06·p(1−p)`),
buy asks ≤ `low − fee − 2¢` or sell bids ≥ `high + fee + 2¢`. Walk levels while
the edge holds, max 20 shares per signal, 2 s cooldown. This catches books that
lag BTC moves. Fees are smallest near 0 and 1, so late-window mispricings are
cheapest to take.

## Risk engine (cannot be overridden)

Exposure is **worst-case loss at resolution** across the position and every
resting order (all fill subsets are checked):

- $20 per window (× `trade_scale`, capped by `max_window_exposure`) and $60 in total.
- Daily loss limit $20 (realized + marked), which halts until the next UTC day. Optional daily take-profit.
- Per-window lock: at +$5 or −$10 marked P&L the window stops trading.
- Final 30 s: no orders, and everything resting is cancelled. Inventory is held to resolution and logged as carried.
- Rate caps on orders/min, cancels/min and actions/s. Pulling a quote is always allowed.
- Kill switch: the `KILL` file cancels everything and stops.

In a single book every net position is "unhedged", so the unhedged caps are
the exposure caps above.

## What the reports measure

`pmbot report --strategy` (paper) and `pmbot backtest` show:
- P&L by component (maker/taker, attributed per fill against the final outcome), by market and by day;
- fees and rebates;
- edge at fill;
- **markouts** at 10 s and 60 s. Negative means adverse selection: we were picked off;
- inventory carried to resolution, locked windows, max drawdown, order/cancel counts.

## How to tune

1. Run `pmbot monitor` or `pmbot run` for a day or more.
2. `pmbot backtest` replays that data through this exact strategy.
   - Read the maker markouts first. If they are negative, widen `spread_vol_mult` or `model_margin`, or stop quoting earlier.
   - If fills are rare but markouts are healthy, tighten.
3. Change one parameter at a time and re-run the backtest on the same data.

## Known limitations

- Price-to-beat and settlement rules are unverified for US. The monitor scores the candidate rules against settlements. Set `ptb_rule` and `settle_twap_s` once the data is in.
- The recorded data is 1 Hz top-of-book, so backtests see less depth and more delay than live paper trading.
- Paper maker fills need a trade *through* our price, which understates fills at our exact price (conservative).
- Live execution (Phase 3) still needs the order translation (buy Up vs short/close) and startup reconciliation.
