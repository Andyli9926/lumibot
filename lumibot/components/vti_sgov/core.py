"""Pure rules, exchange dates, whole-share planning and cumulative fill ledger."""
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
import math

import pandas as pd
import pandas_market_calendars as mcal
from pandas.tseries.holiday import USFederalHolidayCalendar

SYMBOLS = ("VTI", "SGOV")
WEIGHTS = {"risk_on": {"VTI": .80, "SGOV": .18},
           "defensive": {"VTI": 0., "SGOV": .98},
           "buy_hold": {"VTI": 1., "SGOV": 0.}}


def positive(value):
    return math.isfinite(float(value)) and value > 0


@dataclass(frozen=True)
class Rules:
    lookback: int = 200
    buffer: float = .01
    drift: float = .05

    def __post_init__(self):
        if self.lookback < 2 or not 0 <= self.buffer < 1 or not 0 < self.drift < 1:
            raise ValueError("Invalid strategy parameters")

    def transition(self, close, average, state):
        if state not in ("defensive", "risk_on") or not positive(close) or not positive(average):
            raise ValueError("Invalid signal input/state")
        if state == "defensive" and close >= average * (1 + self.buffer):
            return "risk_on"
        if state == "risk_on" and close <= average * (1 - self.buffer):
            return "defensive"
        return state

    def signal(self, closes, state):
        values = list(closes)[-self.lookback:]
        if len(values) < self.lookback:
            raise ValueError(f"Need {self.lookback} completed sessions")
        if not all(positive(v) for v in values):
            raise ValueError("Invalid adjusted close")
        average = sum(values) / self.lookback
        return self.transition(values[-1], average, state), average

    def should_rebalance(self, old, new, monthly, vti_weight):
        return old != new or (monthly and
            abs(vti_weight - WEIGHTS[new]["VTI"]) >= self.drift - 1e-12)


class Calendar:
    """NYSE signal sessions; settlement also excludes federal bank holidays.

    This deliberately conservative research approximation is not evidence of
    actual live settlement. Live purchases require a separate broker attestation.
    """
    def __init__(self, start, end):
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        self.schedule = mcal.get_calendar("NYSE").schedule(
            start_date=start - pd.Timedelta(days=40), end_date=end + pd.Timedelta(days=40))
        self.sessions = [d.date() for d in self.schedule.index]
        self.session_set = set(self.sessions)
        self.bank_holidays = set(USFederalHolidayCalendar().holidays(
            start - pd.Timedelta(days=40), end + pd.Timedelta(days=40)).date)
        self.week_ends = {d for i, d in enumerate(self.sessions[:-1])
                         if d.isocalendar()[:2] != self.sessions[i + 1].isocalendar()[:2]}
        self.month_first = {}
        for d in sorted(self.week_ends):
            self.month_first.setdefault((d.year, d.month), d)

    def next_session(self, day, count=1):
        if count < 1:
            raise ValueError("Execution must be after the signal session")
        return self.sessions[bisect_right(self.sessions, day) + count - 1]

    def weekly(self, day):
        return day in self.week_ends

    def monthly(self, day):
        return self.month_first.get((day.year, day.month)) == day

    def settlement(self, traded):
        remaining = 1 if traded >= date(2024, 5, 28) else 2
        day = traded
        while remaining:
            day += timedelta(days=1)
            if day in self.session_set and day not in self.bank_holidays:
                remaining -= 1
            if day > self.sessions[-1]:
                raise ValueError("Settlement outside calendar")
        return day

    def completed(self, now):
        now = pd.Timestamp(now)
        if now.tzinfo is None:
            raise ValueError("as-of timestamp must include timezone")
        eligible = self.schedule[self.schedule.market_close < now]
        if eligible.empty:
            raise ValueError("No completed session")
        return eligible.index[-1].date()


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    reason: str

    def __post_init__(self):
        if (self.symbol not in SYMBOLS or self.side not in ("BUY", "SELL")
                or type(self.quantity) is not int or self.quantity <= 0
                or not positive(self.limit_price) or not self.reason):
            raise ValueError("Invalid whole-share intent")


@dataclass
class Ledger:
    cash: float
    lots: dict = field(default_factory=lambda: dict.fromkeys(SYMBOLS, 0))
    pending: list = field(default_factory=list)
    applied: dict = field(default_factory=dict)

    def __post_init__(self):
        if not math.isfinite(self.cash) or self.cash < 0:
            raise ValueError("Invalid settled cash")
        if set(self.lots) - set(SYMBOLS):
            raise ValueError("Unsupported strategy lot")
        self.lots = {s: self.lots.get(s, 0) for s in SYMBOLS}
        if any(type(q) is not int or q < 0 for q in self.lots.values()):
            raise ValueError("Strategy lots must be nonnegative whole shares")

    @property
    def receivables(self):
        return sum(p["amount"] for p in self.pending)

    def value(self, prices):
        return self.cash + self.receivables + sum(self.lots[s] * prices[s] for s in SYMBOLS)

    def settle(self, day):
        due = [p for p in self.pending if date.fromisoformat(p["due"]) <= day]
        self.cash += sum(p["amount"] for p in due)
        self.pending = [p for p in self.pending if date.fromisoformat(p["due"]) > day]

    def credit_later(self, amount, due, kind):
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Invalid receivable")
        self.pending.append({"amount": amount, "due": due.isoformat(), "kind": kind})

    def apply_fill(self, order_id, intent, cumulative_qty, cumulative_notional,
                   cumulative_fees, traded, calendar):
        """Apply only new execution deltas. Repeated snapshots are harmless."""
        prev = self.applied.get(order_id, {"quantity": 0, "notional": 0., "fees": 0.,
                                          "intent": intent.__dict__})
        if prev["intent"] != intent.__dict__:
            raise ValueError("Order identity changed")
        q = cumulative_qty - prev["quantity"]
        gross = cumulative_notional - prev["notional"]
        fees = cumulative_fees - prev["fees"]
        if (type(cumulative_qty) is not int or q < 0 or cumulative_qty > intent.quantity
                or not math.isfinite(gross) or gross < 0
                or not math.isfinite(fees) or fees < 0):
            raise ValueError("Invalid cumulative fill/regression")
        if q == 0:
            if gross != 0 or fees != 0:
                raise ValueError("Fee corrections require explicit reconciliation")
            return
        if gross <= 0:
            raise ValueError("Missing fill price")
        if intent.side == "BUY":
            if gross + fees > self.cash + 1e-8:
                raise ValueError("Insufficient settled strategy cash")
            self.cash = max(0., self.cash - gross - fees)
            self.lots[intent.symbol] += q
        else:
            if q > self.lots[intent.symbol] or fees > gross:
                raise ValueError("Cannot sell unallocated strategy lots")
            due = calendar.settlement(traded)
            self.lots[intent.symbol] -= q
            self.credit_later(gross - fees, due, "sale")
            self.pending[-1]["order_id"] = order_id
        self.applied[order_id] = {"quantity": cumulative_qty,
                                 "notional": cumulative_notional,
                                 "fees": cumulative_fees, "intent": intent.__dict__}


def plan_orders(ledger, prices, state, reason, cost_bps=0., sell_fee=.0, *, buy_only=False):
    """Sell excess first; buy only with already settled allocated cash.

    Quotes/limits are inputs, not a prediction of actual execution prices.
    Re-plan deferred buys after settlement and a fresh account/price check.
    """
    if state not in WEIGHTS or not all(positive(prices[s]) for s in SYMBOLS):
        raise ValueError("Invalid target or price")
    if not 0 <= cost_bps < 1000 or sell_fee < 0:
        raise ValueError("Invalid execution cost")
    weights = WEIGHTS[state]
    equity = ledger.value(prices)
    target = {s: math.floor(equity * weights[s] / prices[s]) for s in SYMBOLS}
    sells = [Intent(s, "SELL", ledger.lots[s] - target[s], prices[s], reason)
             for s in SYMBOLS if not buy_only and ledger.lots[s] > target[s]]
    reserve = equity * (1 - sum(weights.values()))
    available = max(0., ledger.cash - reserve - len(sells) * sell_fee)
    buys = []
    for s in SYMBOLS:
        quantity = min(max(0, target[s] - ledger.lots[s]),
                       math.floor(available / (prices[s] * (1 + cost_bps / 10_000))))
        if quantity:
            buys.append(Intent(s, "BUY", quantity, prices[s], reason))
            available -= quantity * prices[s] * (1 + cost_bps / 10_000)
    return sells + buys
