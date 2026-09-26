from datetime import datetime, timezone
from decimal import Decimal as D

from pmbot.config import RiskConfig, SizingConfig
from pmbot.risk import RiskEngine, worst_loss

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc).timestamp()


def engine(**kw):
    sizing = kw.pop("sizing", SizingConfig())
    r = RiskEngine(RiskConfig(**kw), sizing)
    r.register_window("w", T0 + 900)
    return r


def test_worst_loss_examples():
    assert worst_loss(D("-4"), 10) == D("4")          # long 10 @ 0.40
    assert worst_loss(D("6"), -10) == D("4")          # short 10 @ 0.60 (sold Up)
    assert worst_loss(D("0.3"), 0) == D("0")          # round trip: locked +0.30
    # resting bid for 10 @ 0.5 while flat -> 5 at risk
    assert worst_loss(D("0"), 0, [(D("0.5"), 10)]) == D("5")
    # resting bid and ask: worst of each scenario
    assert worst_loss(D("0"), 0, [(D("0.4"), 10)], [(D("0.6"), 10)]) == D("4")


def test_check_cuts_quantity_to_window_budget():
    r = engine()
    q, why = r.check("w", "buy", D("0.50"), 100, T0)
    assert q == 40 and "cut" in why      # 40 * 0.5 = $20 budget


def test_total_exposure_cap_across_windows():
    r = engine(max_total_exposure=D("25"), daily_loss_hard_cap=False)
    r.register_window("w2", T0 + 900)
    r.on_fill("w", None, "buy", D("0.5"), 40, D("0"))     # $20 at risk
    q, _ = r.check("w2", "buy", D("0.5"), 40, T0)
    assert q == 10                                         # only $5 left in total


def test_trade_scale_scales_budget():
    r = engine(sizing=SizingConfig(trade_scale=D("0.5")))
    assert r.window_budget == D("10")
    assert r.check("w", "buy", D("0.5"), 100, T0)[0] == 20


def test_entry_cutoff_blocks_orders():
    r = engine()
    assert r.check("w", "buy", D("0.5"), 1, T0 + 900 - 30)[0] == 0
    assert r.in_cutoff("w", T0 + 871)
    assert r.check("w", "buy", D("0.5"), 1, T0 + 869)[0] == 1


def test_daily_loss_limit_halts_until_next_utc_day():
    r = engine()
    r.on_fill("w", None, "buy", D("0.5"), 40, D("0"))
    r.update_mark("w", 0.0)                 # marked to zero -> -$20
    newly, halted = r.check_pnl_locks(T0)
    assert halted and "daily loss" in r.halt_reason
    assert r.check("w", "buy", D("0.1"), 1, T0 + 10)[0] == 0
    tomorrow = T0 + 13 * 3600
    assert not r.halted(tomorrow)


def test_window_take_profit_and_stop_loss_lock():
    r = engine(daily_loss_limit=D("1000"))
    r.on_fill("w", None, "buy", D("0.4"), 20, D("0"))
    r.update_mark("w", 0.7)                 # +6 >= 5 take-profit
    newly, _ = r.check_pnl_locks(T0)
    assert newly == ["w"] and "take-profit" in r.windows["w"].locked
    assert r.check("w", "sell", D("0.7"), 1, T0)[0] == 0
    r2 = engine(daily_loss_limit=D("1000"))
    r2.on_fill("w", None, "buy", D("0.5"), 40, D("0"))
    r2.update_mark("w", 0.2)                # -12 <= -10
    assert r2.check_pnl_locks(T0)[0] == ["w"]


def test_rate_limits():
    r = engine(max_actions_per_s=2, max_orders_per_min=3)
    r.note_order(T0)
    r.note_order(T0)
    assert "actions/s" in r.check("w", "buy", D("0.1"), 1, T0)[1]
    r.note_order(T0 + 2)
    assert "orders/min" in r.check("w", "buy", D("0.1"), 1, T0 + 3)[1]
    assert r.check("w", "buy", D("0.1"), 1, T0 + 61)[0] == 1


def test_fills_update_resting_and_exposure():
    r = engine()
    r.on_order_live("w", "o1", "buy", D("0.5"), 10)
    assert r.windows["w"].exposure() == D("5")
    r.on_fill("w", "o1", "buy", D("0.5"), 4, D("0.02"))
    w = r.windows["w"]
    assert w.pos == 4 and w.resting["o1"][2] == 6 and w.cash == D("-2.02")
    r.on_order_done("w", "o1")
    assert w.exposure() == D("2.02")


def test_daily_loss_hard_cap_limits_open_risk_to_remaining_budget():
    r = engine()                                   # limit $20, hard cap on
    r.register_window("w2", T0 + 900)
    r.on_fill("w", None, "buy", D("0.5"), 30, D("0"))     # $15 at risk
    assert r.check("w2", "buy", D("0.5"), 40, T0)[0] == 10  # only $5 of the $20 left
    r.realized_today = D("-12")                    # after a $12 realized loss
    assert r.total_cap() == D("8")


def test_hard_cap_off_restores_total_exposure_limit():
    r = engine(daily_loss_hard_cap=False)
    assert r.total_cap() == D("60")
