"""No network: fail-closed execution and durable uncertain-order handling."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from lumibot.components.vti_sgov.core import Intent, Ledger
from lumibot.components.vti_sgov.execution import (
    Snapshot, Approval, ConfirmedExecutor, load_private, atomic_private,
)
from lumibot.brokers.schwab_confirmed import SchwabConfirmedAdapter
from lumibot.brokers.schwab_confirmed import OperatorEvidence


NOW = datetime(2026, 9, 28, 15, tzinfo=timezone.utc)
ORDER = Intent("VTI", "BUY", 2, 300., "trend_change")


class Broker:
    def __init__(self):
        self.view = Snapshot("exact-account", "CASH", NOW, 10_000.,
                             {"VTI": 100., "SGOV": 50.}, [], "broker settled-cash evidence")
        self.calls = 0
        self.timeout = False

    def snapshot(self):
        return self.view

    def submit(self, intent):
        self.calls += 1
        if self.timeout:
            raise TimeoutError("unknown outcome")
        return "broker-order-1"


def executor(tmp_path, broker):
    return ConfirmedExecutor(tmp_path / "live.json", broker, "exact-account", Ledger(1000))


def approval():
    return Approval.create("exact-account", ORDER, NOW, "human-confirmed-one-order")


@pytest.mark.parametrize("change", [
    {"account": "wrong"}, {"account_type": "MARGIN"}, {"settled_cash": None},
    {"settled_cash": 100.}, {"as_of": NOW - timedelta(minutes=5)},
    {"settlement_source": ""}, {"open_orders": [{"orderId": "pending"}]},
    {"orders_complete": False},
])
def test_preflight_blocks_before_any_submit(tmp_path, change):
    broker = Broker()
    broker.view = replace(broker.view, **change)
    with pytest.raises(ValueError):
        executor(tmp_path, broker).submit(ORDER, approval(), NOW)
    assert broker.calls == 0


def test_expired_or_altered_confirmation_rejected(tmp_path):
    broker = Broker()
    runner = executor(tmp_path, broker)
    with pytest.raises(ValueError):
        runner.submit(ORDER, approval(), NOW + timedelta(minutes=11))
    with pytest.raises(ValueError):
        runner.submit(replace(ORDER, quantity=3), approval(), NOW)
    assert broker.calls == 0


def test_timeout_persisted_and_restart_does_not_resend(tmp_path):
    broker = Broker()
    broker.timeout = True
    runner = executor(tmp_path, broker)
    with pytest.raises(RuntimeError, match="unknown"):
        runner.submit(ORDER, approval(), NOW)
    assert load_private(tmp_path / "live.json")["orders"][0]["status"] == "UNKNOWN"
    broker.timeout = False
    restarted = executor(tmp_path, broker)
    with pytest.raises(ValueError, match="reconcil"):
        restarted.submit(ORDER, approval(), NOW)
    assert broker.calls == 1


def test_human_lots_never_become_strategy_sell_capacity(tmp_path):
    broker = Broker()
    sale = Intent("VTI", "SELL", 1, 300., "exit")
    approve = Approval.create("exact-account", sale, NOW, "human")
    with pytest.raises(ValueError, match="allocated"):
        executor(tmp_path, broker).submit(sale, approve, NOW)
    assert broker.calls == 0


def test_live_partial_fill_reconciliation_does_not_auto_settle_sales(tmp_path):
    broker = Broker()
    runner = ConfirmedExecutor(tmp_path / "live.json", broker, "exact-account", Ledger(200, {"VTI": 10}))
    sale = Intent("VTI", "SELL", 10, 300., "exit")
    ap = Approval.create("exact-account", sale, NOW, "human")
    oid = runner.submit(sale, ap, NOW)
    runner.reconcile(oid, 4, 1200., .1, NOW.date(), "PARTIALLY_FILLED")
    runner.reconcile(oid, 4, 1200., .1, NOW.date(), "PARTIALLY_FILLED")
    state = load_private(tmp_path / "live.json")
    assert state["ledger"]["lots"]["VTI"] == 6
    assert state["ledger"]["cash"] == 200
    assert len(state["ledger"]["pending"]) == 1


def test_private_file_atomic_and_restrictive(tmp_path):
    path = tmp_path / "state.json"
    atomic_private(path, {"a": 1})
    assert load_private(path) == {"a": 1}
    assert path.stat().st_mode & 0o777 == 0o600


class Response:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


def test_schwab_never_falls_back_to_first_account():
    class Client:
        def get_account_numbers(self):
            return Response([{"accountNumber": "other", "hashValue": "hash"}])
    with pytest.raises(ValueError, match="exact"):
        SchwabConfirmedAdapter(Client(), "wanted")


def test_cash_balance_is_not_settled_cash():
    class Client:
        def get_account_numbers(self):
            return Response([{"accountNumber": "wanted", "hashValue": "hash"}])
        def get_account(self, *args, **kwargs):
            return Response({"securitiesAccount": {"accountNumber": "wanted", "type": "CASH",
                "positions": [], "currentBalances": {"cashBalance": 1000, "cashAvailableForTrading": 1000}}})
        def get_orders_for_account(self, *args, **kwargs): return Response([])
    snapshot = SchwabConfirmedAdapter(Client(), "wanted").snapshot()
    assert snapshot.settled_cash is None


def test_live_limit_must_match_exact_cent_confirmation(tmp_path):
    broker = Broker()
    order = replace(ORDER, limit_price=300.001)
    ap = Approval.create("exact-account", order, NOW, "human")
    with pytest.raises(ValueError, match="cent"):
        executor(tmp_path, broker).submit(order, ap, NOW)
    assert broker.calls == 0


def test_live_sale_requires_specific_broker_settlement_confirmation(tmp_path):
    broker = Broker()
    runner = ConfirmedExecutor(tmp_path / "live.json", broker, "exact-account", Ledger(200, {"VTI": 10}))
    sale = Intent("VTI", "SELL", 10, 300., "exit")
    oid = runner.submit(sale, Approval.create("exact-account", sale, NOW, "human"), NOW)
    runner.reconcile(oid, 10, 3000., .1, NOW.date(), "FILLED")
    with pytest.raises(ValueError):
        runner.confirm_settlement(oid, "", NOW)
    tomorrow = NOW + timedelta(days=1)
    broker.view = replace(broker.view, as_of=tomorrow, settled_cash=10000.)
    runner.confirm_settlement(oid, "operator reviewed this sale as settled at broker", tomorrow)
    state = load_private(tmp_path / "live.json")
    assert state["ledger"]["cash"] == pytest.approx(3199.9)
    assert state["ledger"]["pending"] == []
    with pytest.raises(ValueError):
        runner.confirm_settlement(oid, "same evidence", tomorrow)


def test_future_snapshot_seconds_of_network_latency_allowed(tmp_path):
    broker = Broker()
    broker.view = replace(broker.view, as_of=NOW + timedelta(seconds=1))
    assert executor(tmp_path, broker).submit(ORDER, approval(), NOW) == "broker-order-1"


def test_refresh_reads_broker_executions_and_reconciles_cumulative_fills(tmp_path):
    broker = Broker()
    runner = executor(tmp_path, broker)
    oid = runner.submit(ORDER, approval(), NOW)
    broker.order = lambda order_id: {"symbol": "VTI", "side": "BUY", "quantity": 2,
        "limit_price": 300., "status": "WORKING", "raw": {
            "filledQuantity": 1, "orderActivityCollection": [{"activityId": "a",
                "executionType": "FILL", "executionLegs": [
                    {"quantity": 1, "price": 299., "time": NOW.isoformat()}]}]}}
    runner.refresh(oid, cumulative_fees=0.)
    runner.refresh(oid, cumulative_fees=0.)
    state = load_private(tmp_path / "live.json")
    assert state["ledger"]["lots"]["VTI"] == 1
    assert state["ledger"]["cash"] == 701.
    assert state["orders"][0]["status"] == "PARTIALLY_FILLED"


def test_older_gtc_found_in_complete_human_review_blocks_submission(tmp_path):
    class Client:
        def get_account_numbers(self):
            return Response([{"accountNumber": "wanted", "hashValue": "hash"}])
        def get_account(self, *args, **kwargs):
            return Response({"securitiesAccount": {"accountNumber": "wanted", "type": "CASH",
                "positions": [], "currentBalances": {"cashBalance": 1000}}})
        def get_orders_for_account(self, *args, **kwargs): return Response([])  # older GTC absent
        def place_order(self, *args, **kwargs): pytest.fail("Must never send with an older GTC")
    now = datetime.now(timezone.utc)
    evidence = OperatorEvidence("wanted", 1000., now, "broker full open-order page", True,
        open_orders=({"orderId": "older-gtc", "status": "WORKING"},))
    adapter = SchwabConfirmedAdapter(Client(), "wanted", operator_evidence=evidence)
    view = adapter.snapshot()
    assert view.orders_complete
    assert view.open_orders
    runner = ConfirmedExecutor(tmp_path / "live.json", adapter, "wanted", Ledger(1000))
    with pytest.raises(ValueError, match="orders"):
        runner.submit(ORDER, Approval.create("wanted", ORDER, now, "human"), now)


def test_bind_uncertain_verifies_details_and_cannot_duplicate_binding(tmp_path):
    broker = Broker()
    broker.timeout = True
    runner = executor(tmp_path, broker)
    with pytest.raises(RuntimeError): runner.submit(ORDER, approval(), NOW)
    broker.order = lambda oid: {**ORDER.__dict__, "entered_at": NOW.isoformat()}
    runner.bind_uncertain(approval().confirmation_id, "found-order", NOW)
    assert load_private(tmp_path / "live.json")["orders"][0]["broker_order_id"] == "found-order"
    with pytest.raises(ValueError):
        runner.bind_uncertain(approval().confirmation_id, "found-order", NOW)


def test_bind_uncertain_rejects_different_broker_quantity(tmp_path):
    broker = Broker()
    broker.timeout = True
    runner = executor(tmp_path, broker)
    with pytest.raises(RuntimeError): runner.submit(ORDER, approval(), NOW)
    broker.order = lambda oid: {**ORDER.__dict__, "quantity": 3, "entered_at": NOW.isoformat()}
    with pytest.raises(ValueError): runner.bind_uncertain(approval().confirmation_id, "wrong", NOW)
    assert load_private(tmp_path / "live.json")["orders"][0]["status"] == "UNKNOWN"
