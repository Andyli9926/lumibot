"""Offline behavioral contracts for the segregated weekly portfolio."""
from datetime import date

import pytest

from lumibot.components.vti_sgov.core import (
    Calendar, Intent, Ledger, Rules, plan_orders,
)


def test_hysteresis_inclusive_thresholds_and_initial_defense():
    rules = Rules()
    assert rules.transition(101, 100, "defensive") == "risk_on"
    assert rules.transition(99, 100, "risk_on") == "defensive"
    assert rules.transition(100, 100, "risk_on") == "risk_on"
    assert rules.transition(100, 100, "defensive") == "defensive"
    with pytest.raises(ValueError, match="200"):
        rules.signal([100] * 199, "defensive")


def test_signal_rejects_nonfinite_and_unknown_state():
    rules = Rules()
    with pytest.raises(ValueError):
        rules.signal([float("nan")] * 200, "defensive")
    with pytest.raises(ValueError):
        rules.transition(100, 100, "typo")


def test_good_friday_week_and_monthly_checkpoint():
    cal = Calendar("2024-01-01", "2026-12-31")
    assert cal.weekly(date(2024, 3, 28))
    assert not cal.weekly(date(2024, 3, 29))
    assert cal.next_session(date(2024, 3, 28)) == date(2024, 4, 1)
    assert cal.monthly(date(2024, 4, 5))
    assert not cal.monthly(date(2024, 4, 12))
    assert cal.weekly(date(2024, 11, 29))  # shortened session


def test_historical_settlement_and_bank_holiday_are_conservative():
    cal = Calendar("2024-01-01", "2026-12-31")
    assert cal.settlement(date(2024, 5, 23)) == date(2024, 5, 28)
    assert cal.settlement(date(2024, 5, 28)) == date(2024, 5, 29)
    assert cal.settlement(date(2024, 10, 11)) == date(2024, 10, 15)


def test_rebalance_gate_is_vti_drift_and_transition_always_wins():
    rules = Rules()
    assert rules.should_rebalance("defensive", "risk_on", False, 0)
    assert not rules.should_rebalance("risk_on", "risk_on", False, .5)
    assert not rules.should_rebalance("risk_on", "risk_on", True, .76)
    assert rules.should_rebalance("risk_on", "risk_on", True, .75)
    assert not rules.should_rebalance("defensive", "defensive", True, 0)


def test_plan_uses_strategy_lots_and_whole_shares_and_reserve():
    ledger = Ledger(10_000)
    orders = plan_orders(ledger, {"VTI": 300, "SGOV": 100}, "risk_on", "entry")
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [
        ("VTI", "BUY", 26), ("SGOV", "BUY", 18)]
    assert sum(o.quantity * o.limit_price for o in orders) <= 9800
    ledger = Ledger(200, {"VTI": 26, "SGOV": 18})
    orders = plan_orders(ledger, {"VTI": 300, "SGOV": 100}, "defensive", "exit")
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [("VTI", "SELL", 26)]
    # Manual holdings in the account never enter this isolated ledger.
    assert all(o.quantity <= ledger.lots[o.symbol] for o in orders if o.side == "SELL")


def test_partial_fill_idempotency_and_no_unsettled_spending():
    cal = Calendar("2025-01-01", "2026-12-31")
    ledger = Ledger(200, {"VTI": 10, "SGOV": 0})
    sale = Intent("VTI", "SELL", 10, 300, "exit")
    ledger.apply_fill("order-a", sale, 4, 1200, .1, date(2025, 1, 6), cal)
    ledger.apply_fill("order-a", sale, 4, 1200, .1, date(2025, 1, 6), cal)
    assert ledger.lots["VTI"] == 6
    assert ledger.cash == 200
    assert ledger.receivables == pytest.approx(1199.9)
    ledger.apply_fill("order-a", sale, 10, 3000, .2, date(2025, 1, 6), cal)
    assert ledger.lots["VTI"] == 0
    assert ledger.receivables == pytest.approx(2999.8)
    with pytest.raises(ValueError, match="cash"):
        ledger.apply_fill("buy-a", Intent("SGOV", "BUY", 5, 100, "entry"),
                          5, 500, 0, date(2025, 1, 6), cal)
    ledger.settle(date(2025, 1, 7))
    assert ledger.cash == pytest.approx(3199.8)
    assert ledger.receivables == 0


def test_fill_regression_and_oversell_fail_without_mutation():
    cal = Calendar("2025-01-01", "2026-12-31")
    ledger = Ledger(100, {"VTI": 2, "SGOV": 0})
    with pytest.raises(ValueError):
        ledger.apply_fill("bad", Intent("VTI", "SELL", 5, 100, "exit"),
                          5, 500, 0, date(2025, 1, 6), cal)
    assert ledger.lots["VTI"] == 2
    assert ledger.cash == 100
