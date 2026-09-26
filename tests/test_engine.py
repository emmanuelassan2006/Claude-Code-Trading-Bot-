import logging
import math
import random
from decimal import Decimal as D

from pmbot.backtest import run_backtest
from pmbot.book import Level
from pmbot.config import Config
from pmbot.discovery import Window
from pmbot.engine import TradingEngine
from pmbot.feeds.history import PriceHistory, Tick
from pmbot.model import annual_to_ps
from pmbot.reports import build_strategy_report, render_strategy_text
from pmbot.store import Store

START = 1790000100.0  # 2026-09-21T14:15:00Z
END = START + 900


def setup(price=100_000.0, **cfg_overrides):
    cfg = Config()
    cfg.sim.latency_ms = 100
    for k, v in cfg_overrides.items():
        setattr(cfg.strategy, k, v)
    h = PriceHistory(max_age_s=10_000)
    rng = random.Random(7)
    sig = annual_to_ps(0.5)
    p = price
    for t in range(-1800, 1):  # 30 min of history before the open
        h.add(Tick(START + t, p))
        p *= math.exp(sig * rng.gauss(0, 1))
    h.add(Tick(START, price))
    store = Store(":memory:", mode="dry_run")
    eng = TradingEngine(cfg, store, {"BTC": h}, mode="dry_run")
    w = Window("btc-w", "BTC", "15m", START, END, "single", "cpc-btc", None, True)
    eng.add_window(w)
    return eng, store, h


def L(*pq):
    return [Level(D(str(p)), D(str(q))) for p, q in pq]


def test_refuses_live_mode():
    import pytest
    with pytest.raises(ValueError):
        TradingEngine(Config(), Store(":memory:"), {}, mode="live")


def test_quotes_both_sides_with_sizing_log(caplog):
    eng, store, h = setup()
    now = START + 60
    h.add(Tick(now, 100_000.0))
    with caplog.at_level(logging.INFO):
        eng.on_book("btc-w", L((0.40, 100)), L((0.60, 100)), now)
    sizing = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SIZING")]
    assert len(sizing) == 2
    assert all("stake=$" in s and "exp_edge=$" in s and "window_budget_left=$" in s
               and "balance_left=$" in s for s in sizing)
    rows = store.query("SELECT side, price FROM orders ORDER BY side")
    assert rows[0]["side"] == "buy" and rows[0]["price"] < 0.5 < rows[1]["price"]


def test_fill_then_settlement_pnl_matches_cash_and_position():
    eng, store, h = setup()
    now = START + 60
    h.add(Tick(now, 100_000.0))
    eng.on_book("btc-w", L((0.40, 100)), L((0.60, 100)), now)
    bid = [o for o in eng.exec.open_orders("btc-w") if o.side == "buy"][0]
    eng.exec.process(now + 1)
    eng.on_trade("btc-w", bid.price - D("0.01"), 4, now + 1)
    wr = eng.risk.windows["btc-w"]
    assert wr.pos == 4
    cash = wr.cash
    eng.on_window_end("btc-w", END)
    eng.on_settled("btc-w", "up", END + 5)
    r = store.query("SELECT * FROM window_results")[0]
    assert r["outcome"] == "up" and r["final_pos"] == 4
    assert abs(r["pnl"] - float(cash + 4)) < 1e-9
    assert r["maker_fills"] == 1 and r["carried_inventory"] == 1


def test_entry_cutoff_cancels_and_blocks():
    eng, store, h = setup()
    now = START + 60
    h.add(Tick(now, 100_000.0))
    eng.on_book("btc-w", L((0.40, 100)), L((0.60, 100)), now)
    assert len(eng.exec.open_orders("btc-w")) == 2
    t = END - 29
    h.add(Tick(t, 100_000.0))
    eng.tick(t)
    eng.exec.process(t + 1)
    assert eng.exec.open_orders("btc-w") == []
    n = store.query("SELECT COUNT(*) c FROM orders")[0]["c"]
    eng.on_book("btc-w", L((0.10, 100)), L((0.11, 100)), t + 2)   # juicy, but past cutoff
    assert store.query("SELECT COUNT(*) c FROM orders")[0]["c"] == n


def test_taker_fires_when_book_is_far_from_fair():
    eng, store, h = setup(maker_enabled=False)
    now = START + 300
    h.add(Tick(now, 100_600.0))     # well above the strike -> Up very likely
    eng.on_book("btc-w", L((0.30, 50)), L((0.35, 50)), now)
    takes = store.query("SELECT * FROM orders WHERE kind='taker'")
    assert takes and takes[0]["side"] == "buy"
    eng.exec.process(now + 1)
    assert eng.risk.windows["btc-w"].pos > 0


def test_stale_feed_means_no_quotes():
    eng, store, h = setup()
    eng.on_book("btc-w", L((0.40, 100)), L((0.60, 100)), START + 60)  # last tick at START
    assert store.query("SELECT COUNT(*) c FROM orders")[0]["c"] == 0
    assert eng.windows["btc-w"].note == "price feed stale"


def test_halt_cancels_everything():
    eng, store, h = setup()
    now = START + 60
    h.add(Tick(now, 100_000.0))
    eng.on_book("btc-w", L((0.40, 100)), L((0.60, 100)), now)
    eng.risk.realized_today = D("-25")
    eng.tick(now + 1)
    eng.exec.process(now + 2)
    assert eng.risk.halted(now + 2) and eng.exec.open_orders("btc-w") == []


def _synthetic_monitor_db(path):
    """A few 15m windows with a random-walk BTC and a noisy, sometimes-stale book."""
    from pmbot.model import prob_up

    src = Store(path, mode="monitor")
    rng = random.Random(3)
    sig = annual_to_ps(0.5)
    p = 100_000.0
    t0 = START - 1800
    hist = []
    for i in range(1800 + 3 * 900 + 60):
        hist.append((t0 + i, p))
        p *= math.exp(sig * rng.gauss(0, 1))
    prices = dict(hist)
    for k in range(3):
        s, e = START + 900 * k, START + 900 * (k + 1)
        key = f"btc-updown-15m-{k}"
        strike = prices[s]
        src.upsert_window({"key": key, "asset": "BTC", "duration": "15m", "start_ts": s,
                           "end_ts": e, "structure": "single", "up_slug": f"cpc-{key}",
                           "down_slug": None, "long_is_up": 1, "title": "BTC Up or Down: 15 min"})
        src.update_window(key, outcome="up" if prices[e] >= strike else "down")
        for t in range(int(s) - (1800 if k == 0 else 0), int(e)):
            fair = prob_up(prices[t], strike, sig, e - t, 60) if t >= s else 0.5
            lag = prob_up(prices[t - 5], strike, sig, e - t, 60) if t >= s + 5 else fair
            mid = min(max(lag + rng.gauss(0, 0.01), 0.03), 0.97)
            src.add_sample({"ts": float(t), "window_key": key, "secs_to_close": e - t,
                            "up_bid": round(mid - 0.02, 2), "up_bid_qty": 50,
                            "up_ask": round(mid + 0.02, 2), "up_ask_qty": 50,
                            "down_bid": None, "down_bid_qty": None, "down_ask": None,
                            "down_ask_qty": None, "taker_cost": None, "maker_edge": None,
                            "ref_price": prices[t], "ptb": None})
            if t >= s and rng.random() < 0.3:
                tp = round(fair + rng.gauss(0, 0.03), 2)
                src.add_trade({"ts": t + 0.5, "recv_ts": t + 0.5, "window_key": key,
                               "slug": f"cpc-{key}", "price": min(max(tp, 0.01), 0.99),
                               "qty": rng.randint(1, 30), "secs_into_window": t - s})
    return src


def test_backtest_end_to_end_respects_risk(tmp_path):
    src = _synthetic_monitor_db(str(tmp_path / "m.db"))
    cfg = Config()
    out = Store(":memory:", mode="backtest")
    info = run_backtest(cfg, src, out)
    assert info["windows"] == 3 and info["settled"] == 3
    rep = build_strategy_report(out, "backtest")
    assert rep["windows"] == 3 and rep["orders"] > 0
    assert "Strategy report" in render_strategy_text(rep)
    # risk: no window ever lost more than the per-window budget
    for r in out.query("SELECT pnl FROM window_results"):
        assert r["pnl"] >= -float(cfg.risk.max_window_exposure) - 1e-9
    # no order was sent inside the entry cutoff
    late = out.query("""SELECT COUNT(*) c FROM orders o JOIN window_results w
                        ON o.window_key = w.window_key WHERE o.sent_ts >= w.end_ts - 30""")
    assert late[0]["c"] == 0


def test_quote_kept_on_small_moves_and_pulled_when_edge_gone():
    eng, store, h = setup()
    now = START + 60
    h.add(Tick(now, 100_000.0))
    eng.on_book("btc-w", L((0.30, 100)), L((0.70, 100)), now)
    n0 = store.query("SELECT COUNT(*) c FROM orders")[0]["c"]
    bid = [o for o in eng.exec.open_orders("btc-w") if o.side == "buy"][0]
    # tiny move: quotes stay put
    h.add(Tick(now + 1, 100_001.0))
    eng.on_book("btc-w", L((0.30, 100)), L((0.70, 100)), now + 1)
    assert store.query("SELECT COUNT(*) c FROM orders")[0]["c"] == n0
    # large drop: fair falls below our bid -> bid repriced immediately
    h.add(Tick(now + 2, 99_900.0))
    eng.on_book("btc-w", L((0.10, 100)), L((0.70, 100)), now + 2)
    ev = store.query("SELECT detail FROM order_events WHERE order_id=?", [bid.id])
    assert ev and ev[0]["detail"] == "requote"
    new_bid = [o for o in eng.exec.open_orders("btc-w") if o.side == "buy" and o.id != bid.id]
    assert new_bid and new_bid[0].price < bid.price
