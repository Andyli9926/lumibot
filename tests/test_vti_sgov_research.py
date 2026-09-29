"""Chronology and dividend invariants, independent of real market returns."""
from datetime import date

import pandas as pd
import pytest

from lumibot.components.vti_sgov.core import Calendar, Rules
from lumibot.components.vti_sgov.research import Engine, validate_frames, run_lumibot, metrics


def frames():
    cal = Calendar("2024-01-01", "2025-03-31")
    days = [d for d in cal.sessions if date(2024, 1, 1) <= d <= date(2025, 3, 31)]
    result = {}
    for symbol in ("VTI", "SGOV"):
        result[symbol] = pd.DataFrame({"open": 100., "high": 101., "low": 99.,
            "close": 100., "adj_close": 100., "dividend": 0., "stock_splits": 0.,
            "volume": 1_000_000}, index=pd.to_datetime(days))
    return result


def test_weekly_signal_cannot_fill_at_signal_close():
    data = frames()
    data["VTI"].loc["2025-01-10":, "adj_close"] = 110.
    engine = Engine(data, "2025-01-06", "2025-01-17", rules=Rules(200), cost_bps=0)
    engine.run()
    entered = [s for s in engine.signals if s["changed"]]
    assert entered[0]["date"] == "2025-01-10"
    buys = [t for t in engine.trades if t["symbol"] == "VTI" and t["side"] == "BUY"]
    assert buys
    assert all(t["date"] > "2025-01-10" for t in buys)
    assert min(t["date"] for t in buys) >= "2025-01-13"
    assert engine.ledger.cash >= 0


def test_dividend_entitlement_is_before_ex_date_trading_and_once_only():
    data = frames()
    data["VTI"].loc["2025-01-06", "dividend"] = 10.  # initial buy has no entitlement
    data["VTI"].loc["2025-01-07", "dividend"] = 1.
    engine = Engine(data, "2025-01-06", "2025-01-10", mode="buy_hold", cost_bps=0)
    engine.run()
    assert engine.ledger.lots["VTI"] == 100
    assert engine.ledger.receivables == 100
    assert engine.rows[-1]["equity"] == 10100
    with pytest.raises(ValueError, match="chronolog"):
        engine.step(date(2025, 1, 10))


def test_missing_bar_or_split_fails_closed():
    data = frames()
    data["SGOV"] = data["SGOV"].drop(pd.Timestamp("2025-01-08"))
    with pytest.raises(ValueError, match="Missing"):
        validate_frames(data, date(2025, 1, 6), date(2025, 1, 10), 200)
    data = frames()
    data["VTI"].loc["2025-01-07", "stock_splits"] = 2
    with pytest.raises(ValueError, match="split"):
        validate_frames(data, date(2025, 1, 6), date(2025, 1, 10), 200)


def test_confirmation_delay_and_costs_change_fills():
    data = frames()
    data["VTI"].loc["2025-01-10":, "adj_close"] = 110.
    base = Engine(data, "2025-01-06", "2025-01-24", cost_bps=0)
    delay = Engine(data, "2025-01-06", "2025-01-24", cost_bps=10, delay_sessions=2)
    base.run(); delay.run()
    assert min(t["date"] for t in delay.trades if t["symbol"] == "VTI") > min(
        t["date"] for t in base.trades if t["symbol"] == "VTI")
    assert delay.rows[-1]["equity"] < base.rows[-1]["equity"]


def test_metrics_include_initial_capital_and_unrecovered_drawdown():
    rows = [{"date": "2025-01-06", "equity": 9000},
            {"date": "2025-01-07", "equity": 9500}]
    result = metrics(rows, [], [], 10000)
    assert result["max_drawdown"] == pytest.approx(-.1)
    assert result["unrecovered"] is True
    assert result["rolling_5y_min"] is None


def test_lumibot_daily_clock_matches_direct_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    data = frames()
    direct = Engine(data, "2025-01-06", "2025-01-17", mode="static")
    direct.run()
    driven = Engine(data, "2025-01-06", "2025-01-17", mode="static")
    run_lumibot(driven, tmp_path)
    assert driven.rows == direct.rows
    assert driven.trades == direct.trades


def test_deferred_buy_completion_does_not_start_new_sell_legs():
    data = frames()
    data["VTI"].loc["2025-01-10":, "adj_close"] = 110.
    engine = Engine(data, "2025-01-06", "2025-01-17", cost_bps=0., sell_fee=.10)
    engine.run()
    sales = [t for t in engine.trades if t["side"] == "SELL"]
    assert [(t["date"], t["symbol"], t["quantity"]) for t in sales] == [
        ("2025-01-13", "SGOV", 80)]
    assert engine.ledger.lots["SGOV"] == 18
