"""Explicitly confirmed execution boundary with a durable submission journal.

Research and paper CLIs never import or call submit. This module is a separate
operator-facing integration, not an unattended trading service. All live state
is private, account-specific and must be initialized with an explicit allocation.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from .core import Calendar, Intent, Ledger, SYMBOLS


def load_private(path):
    return json.loads(Path(path).read_text())


def atomic_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("State cannot be a symlink")
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Snapshot:
    account: str
    account_type: str
    as_of: datetime
    settled_cash: float | None
    positions: dict
    open_orders: list
    settlement_source: str
    orders_complete: bool = True


@dataclass(frozen=True)
class Approval:
    account: str
    digest: str
    issued: datetime
    expires: datetime
    confirmation_id: str

    @staticmethod
    def fingerprint(account, intent):
        payload = json.dumps({"account": account, "intent": asdict(intent)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def create(cls, account, intent, now, confirmation_id):
        """Call only after a person reviews this exact order; not in paper mode."""
        return cls(account, cls.fingerprint(account, intent), now,
                   now + timedelta(minutes=10), confirmation_id)

    def validate(self, account, intent, now):
        if (self.account != account or self.digest != self.fingerprint(account, intent)
                or not self.confirmation_id or not self.issued <= now < self.expires
                or self.expires - self.issued > timedelta(minutes=10)):
            raise ValueError("Missing, altered, or expired per-order confirmation")


TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


class ConfirmedExecutor:
    def __init__(self, path, broker, account, allocation):
        self.path, self.broker, self.account = Path(path), broker, account
        if not account:
            raise ValueError("An exact account is mandatory")
        self.initial = {"schema": 1, "account": account, "ledger": asdict(allocation), "orders": []}

    def _state(self):
        state = load_private(self.path) if self.path.exists() else json.loads(json.dumps(self.initial))
        if state["account"] != self.account or state["schema"] != 1:
            raise ValueError("Wrong account or state schema")
        return state

    def _snapshot(self, now, *, cash_required=False):
        snapshot = self.broker.snapshot()
        if (snapshot.account != self.account or snapshot.account_type != "CASH"
                or not -timedelta(seconds=5) <= now - snapshot.as_of <= timedelta(seconds=60)):
            raise ValueError("Wrong account/type or stale snapshot")
        if snapshot.open_orders or not snapshot.orders_complete:
            raise ValueError("Existing orders must be reconciled first")
        if cash_required and (snapshot.settled_cash is None or not snapshot.settlement_source
                or not math.isfinite(snapshot.settled_cash) or snapshot.settled_cash < 0):
            raise ValueError("Independent current settled-cash evidence required")
        return snapshot

    def submit(self, intent, approval, now=None):
        now = now or datetime.now(timezone.utc)
        if abs(intent.limit_price * 100 - round(intent.limit_price * 100)) > 1e-7:
            raise ValueError("Live limit must be confirmed to the exact cent")
        approval.validate(self.account, intent, now)
        with state_lock(self.path):
            state = self._state()
            if any(o["status"] not in TERMINAL for o in state["orders"]):
                raise ValueError("Unresolved submission: reconcile before another order")
            if any(o["confirmation_id"] == approval.confirmation_id for o in state["orders"]):
                raise ValueError("Confirmation was already used")
            ledger = Ledger(**state["ledger"])
            snapshot = self._snapshot(now, cash_required=intent.side == "BUY")
            if any(not math.isfinite(snapshot.positions.get(s, 0))
                   or snapshot.positions.get(s, 0) < ledger.lots[s] for s in SYMBOLS):
                raise ValueError("Account/allocated holdings reconciliation required")
            if intent.side == "SELL" and intent.quantity > ledger.lots[intent.symbol]:
                raise ValueError("Cannot sell unallocated holdings")
            if intent.side == "BUY":
                amount = intent.quantity * intent.limit_price
                if amount > min(ledger.cash, snapshot.settled_cash) + 1e-8:
                    raise ValueError("Insufficient settled cash")
            record = {"confirmation_id": approval.confirmation_id, "intent": asdict(intent),
                "digest": approval.digest, "submitted_at": now.isoformat(),
                "status": "SUBMITTING", "broker_order_id": None}
            state["orders"].append(record)
            atomic_private(self.path, state)  # commit intent BEFORE the network call
            try:
                oid = str(self.broker.submit(intent))
                if not oid or oid == "None":
                    raise ValueError("No broker order id")
            except Exception as exc:
                record["status"] = "UNKNOWN"
                atomic_private(self.path, state)
                raise RuntimeError("Submission outcome unknown; query broker, never blindly retry") from exc
            record.update(status="SUBMITTED", broker_order_id=oid)
            atomic_private(self.path, state)
            return oid

    def reconcile(self, order_id, cumulative_qty, cumulative_notional, cumulative_fees, day, status):
        if status not in TERMINAL | {"WORKING", "PARTIALLY_FILLED"}:
            raise ValueError("Unrecognized order status")
        with state_lock(self.path):
            state = self._state()
            matches = [o for o in state["orders"] if o["broker_order_id"] == str(order_id)]
            if len(matches) != 1:
                raise ValueError("Unknown/ambiguous order; reconcile identity first")
            order = matches[0]
            intent = Intent(**order["intent"])
            if status == "FILLED" and cumulative_qty != intent.quantity:
                raise ValueError("Filled status and quantity disagree")
            if order["status"] in TERMINAL and status != order["status"]:
                raise ValueError("Terminal order state regression")
            ledger = Ledger(**state["ledger"])
            ledger.apply_fill(str(order_id), intent, cumulative_qty, cumulative_notional,
                               cumulative_fees, day, Calendar(day, day + timedelta(days=40)))
            order["status"] = status
            state["ledger"] = asdict(ledger)
            atomic_private(self.path, state)
            # Never call Ledger.settle here. A modeled date does not prove that
            # the live broker has released these specific sale proceeds.

    def confirm_settlement(self, order_id, broker_evidence, now=None):
        """Operator attests THIS sale settled, after checking its broker record.

        The fresh settled balance alone cannot identify which sale settled.
        Both evidence for this specific sale and sufficient account cash are
        mandatory. An estimated due date alone never releases live funds.
        """
        now = now or datetime.now(timezone.utc)
        if not broker_evidence:
            raise ValueError("Specific broker settlement evidence required")
        with state_lock(self.path):
            state = self._state()
            snapshot = self._snapshot(now, cash_required=True)
            ledger = Ledger(**state["ledger"])
            pending = [p for p in ledger.pending if p.get("order_id") == str(order_id)]
            if not pending or any(p["due"] > now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                                  for p in pending):
                raise ValueError("No eligible unsettled sale for this order")
            amount = sum(p["amount"] for p in pending)
            if ledger.cash + amount > snapshot.settled_cash + 1e-8:
                raise ValueError("Broker settled cash is insufficient")
            ledger.cash += amount
            ledger.pending = [p for p in ledger.pending if p.get("order_id") != str(order_id)]
            state["ledger"] = asdict(ledger)
            state.setdefault("settlements", []).append({"order_id": str(order_id), "amount": amount,
                "confirmed_at": now.isoformat(), "broker_evidence": broker_evidence,
                "balance_evidence": snapshot.settlement_source})
            atomic_private(self.path, state)

    def refresh(self, order_id, *, cumulative_fees):
        """GET broker executions; operator supplies cumulative confirmed fees.

        No assumptions about unreported fees. A mismatched execution sum or a
        multi-day fill for a DAY order requires manual investigation.
        """
        order = self.broker.order(order_id)
        state = self._state()
        matches = [o for o in state["orders"] if o["broker_order_id"] == str(order_id)]
        if len(matches) != 1:
            raise ValueError("No unique bound broker order")
        intent = Intent(**matches[0]["intent"])
        if any(order[k] != getattr(intent, k) for k in ("symbol", "side", "quantity", "limit_price")):
            raise ValueError("Broker order differs from approved details")
        legs = [leg for activity in order["raw"].get("orderActivityCollection", [])
                if activity.get("executionType") == "FILL"
                for leg in activity.get("executionLegs", [])]
        quantity = sum(float(leg["quantity"]) for leg in legs)
        if quantity != int(quantity) or quantity != float(order["raw"].get("filledQuantity", 0)):
            raise ValueError("Execution quantities do not reconcile")
        total = sum(float(leg["price"]) * float(leg["quantity"]) for leg in legs)
        dates = {datetime.fromisoformat(leg["time"].replace("Z", "+00:00"))
                 .astimezone(ZoneInfo("America/New_York")).date() for leg in legs}
        if len(dates) > 1:
            raise ValueError("Unexpected multi-session DAY order executions")
        if ((intent.side == "BUY" and total > quantity * intent.limit_price + 1e-6)
                or (intent.side == "SELL" and total < quantity * intent.limit_price - 1e-6)):
            raise ValueError("Execution violates confirmed limit")
        status = order["status"]
        if status == "WORKING" and quantity:
            status = "PARTIALLY_FILLED"
        self.reconcile(order_id, int(quantity), total, cumulative_fees,
                       next(iter(dates)) if dates else datetime.now(ZoneInfo("America/New_York")).date(), status)

    def bind_uncertain(self, confirmation_id, order_id, now=None):
        """Operator supplies the broker id AFTER querying order history.

        Verify exact order details through a fresh broker GET before binding;
        a missing/ambiguous response leaves UNKNOWN and prevents resubmission.
        """
        now = now or datetime.now(timezone.utc)
        with state_lock(self.path):
            state = self._state()
            matches = [o for o in state["orders"] if o["confirmation_id"] == confirmation_id]
            if len(matches) != 1 or matches[0]["status"] not in {"UNKNOWN", "SUBMITTING"}:
                raise ValueError("No unique uncertain submission")
            record = matches[0]
            broker_order = self.broker.order(order_id)
            intent = Intent(**record["intent"])
            for key in ("symbol", "side", "quantity", "limit_price"):
                if broker_order[key] != getattr(intent, key):
                    raise ValueError("Broker order does not match confirmed intent")
            entered = datetime.fromisoformat(broker_order["entered_at"].replace("Z", "+00:00"))
            submitted = datetime.fromisoformat(record["submitted_at"])
            if not submitted - timedelta(seconds=5) <= entered <= min(now, submitted + timedelta(minutes=2)):
                raise ValueError("Broker order timestamp does not match submission")
            if any(o["broker_order_id"] == str(order_id) for o in state["orders"]):
                raise ValueError("Broker order id already bound")
            record.update(broker_order_id=str(order_id), status="SUBMITTED")
            atomic_private(self.path, state)
