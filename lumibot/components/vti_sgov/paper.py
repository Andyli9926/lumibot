"""Persistent forward-only local paper portfolio, independent of real accounts."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pandas as pd

from .core import Calendar, Intent, Ledger, Rules, plan_orders
from .execution import atomic_private, load_private, state_lock
from .research import Engine


def implementation_fingerprint():
    root = Path(__file__).parent
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in
        ("core.py", "research.py", "paper.py"))).hexdigest()


def initialize(path, now=None, capital=10_000.):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("Paper start needs an aware timestamp")
    day = now.tz_convert("America/New_York").date()
    start = Calendar(day, day + timedelta(days=90)).next_session(day)
    ledger = Ledger(capital)
    state = {"schema": 1, "mode": "paper_only", "implementation": implementation_fingerprint(),
        "created_at": now.isoformat(), "start": start.isoformat(),
        "review_at": (now + pd.Timedelta(weeks=4)).isoformat(),
        "end_at": (now + pd.Timedelta(weeks=8)).isoformat(),
        "capital": capital, "rules": asdict(Rules()), "cost_bps": 5., "sell_fee": .10,
        "delay_sessions": 0, "dividend_delay": 5, "ledger": asdict(ledger),
        "state": "defensive", "batch": {"due": start.isoformat(),
            "reason": "initial_allocation", "signal_date": None},
        "batch_id": 0, "rows": [], "trades": [], "signals": []}
    with state_lock(path):
        if Path(path).exists():
            raise ValueError("Paper state already exists; never silently reset it")
        atomic_private(path, state)
    return state


def check(path, data, now=None):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    with state_lock(path):
        state = load_private(path)
        if (state["schema"] != 1 or state["mode"] != "paper_only"
                or state["implementation"] != implementation_fingerprint()):
            raise ValueError("Paper implementation/schema changed; review migration before resuming")
        start = date.fromisoformat(state["start"])
        boundary = min(now, pd.Timestamp(state["end_at"]))
        end = Calendar(start - timedelta(days=40), boundary.date()).completed(boundary)
        previous_rows, previous_trades, previous_signals = len(state["rows"]), len(state["trades"]), len(state["signals"])
        if end >= start:
            engine = Engine(data, start, end, capital=state["capital"], rules=Rules(**state["rules"]),
                **{k: state[k] for k in ("cost_bps", "sell_fee", "delay_sessions", "dividend_delay")})
            engine.ledger = Ledger(**state["ledger"])
            engine.state = state["state"]
            engine.batch = dict(state["batch"]) if state["batch"] else None
            if engine.batch:
                engine.batch["due"] = date.fromisoformat(engine.batch["due"])
            engine.batch_id = state["batch_id"]
            engine.rows, engine.trades, engine.signals = state["rows"], state["trades"], state["signals"]
            if engine.rows and engine.rows[-1]["date"] > end.isoformat():
                raise ValueError("Cannot move the paper clock backwards")
            engine.last_day = date.fromisoformat(engine.rows[-1]["date"]) if engine.rows else None
            engine.run()
            state.update(ledger=asdict(engine.ledger), state=engine.state, batch_id=engine.batch_id,
                         rows=engine.rows, trades=engine.trades, signals=engine.signals,
                         batch={**engine.batch, "due": engine.batch["due"].isoformat()} if engine.batch else None)
            atomic_private(path, state)
        prices = {s: float(data[s].loc[:pd.Timestamp(end), "close"].iloc[-1]) for s in ("VTI", "SGOV")}
        preview = [asdict(o) for o in plan_orders(Ledger(**state["ledger"]), prices, state["state"],
                    "paper_preview", state["cost_bps"], state["sell_fee"],
                    buy_only=state["batch"].get("started", False))] if state["batch"] else []
        result = {"mode": "paper_only", "start": state["start"],
            "last_session": state["rows"][-1]["date"] if state["rows"] else None,
            "new_sessions": len(state["rows"]) - previous_rows, "state": state["state"],
            "equity": state["rows"][-1]["equity"] if state["rows"] else state["capital"],
            "new_simulated_fills": state["trades"][previous_trades:],
            "new_signals": state["signals"][previous_signals:], "pending_batch": state["batch"],
            "preview_at_last_close": preview,
            "review_due": now >= pd.Timestamp(state["review_at"]),
            "observation_ended": now >= pd.Timestamp(state["end_at"]),
            "review_at": state["review_at"], "end_at": state["end_at"],
            "execution_assumption": "Completed daily bars replay next-session open plus modeled costs. No real orders."}
        atomic_private(Path(path).with_name("paper_status.json"), result)
        return result


def replay_exercise():
    """Deterministic lifecycle exercise, not a historical investment return."""
    cal = Calendar("2025-01-01", "2025-03-01")
    ledger = Ledger(10000)
    events = []
    buy = Intent("VTI", "BUY", 26, 300., "exercise_entry")
    ledger.apply_fill("entry", buy, 26, 7800., 0., date(2025, 1, 6), cal)
    defense = Intent("SGOV", "BUY", 18, 100., "exercise_entry")
    ledger.apply_fill("defense", defense, 18, 1800., 0., date(2025, 1, 6), cal)
    events.append({"event": "bought", "cash": ledger.cash, "lots": dict(ledger.lots)})
    sale = Intent("VTI", "SELL", 26, 290., "exercise_exit")
    ledger.apply_fill("exit", sale, 10, 2900., .1, date(2025, 1, 10), cal)
    partial = dict(ledger.lots)
    ledger.apply_fill("exit", sale, 10, 2900., .1, date(2025, 1, 10), cal)
    duplicate_ignored = ledger.lots == partial
    blocked = False
    try:
        ledger.apply_fill("too_early", Intent("SGOV", "BUY", 50, 100., "exercise"),
                          50, 5000., 0., date(2025, 1, 10), cal)
    except ValueError:
        blocked = True
    ledger.apply_fill("exit", sale, 26, 7540., .2, date(2025, 1, 10), cal)
    events.append({"event": "sale_filled_unsettled", "cash": ledger.cash, "receivables": ledger.receivables})
    ledger.settle(date(2025, 1, 13))
    orders = plan_orders(ledger, {"VTI": 290., "SGOV": 100.}, "defensive", "exercise_settled")
    for i, intent in enumerate(orders):
        ledger.apply_fill(f"defense-after-{i}", intent, intent.quantity,
                          intent.quantity * intent.limit_price, 0., date(2025, 1, 13), cal)
    events.append({"event": "defensive_complete", "cash": ledger.cash, "lots": dict(ledger.lots)})
    return {"synthetic_exercise": True, "duplicate_fill_ignored": duplicate_ignored,
            "unsettled_purchase_blocked": blocked, "final_lots": ledger.lots, "events": events}
