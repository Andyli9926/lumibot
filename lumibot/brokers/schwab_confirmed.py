"""Strict Schwab boundary for the separate, explicitly confirmed workflow.

No fallback account and no interactive/default OAuth callback. Construct the
client with the operator's own app and existing token. A read-only snapshot
does not translate cashBalance or buying power into settled cash.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import math

from lumibot.components.vti_sgov.execution import Snapshot, TERMINAL


@dataclass(frozen=True)
class OperatorEvidence:
    """Fresh human check of the exact account's settled cash and ALL open orders.

    Capture from Schwab's account/settlement pages, never infer from buying power.
    This evidence is private and intentionally expires after 60 seconds.
    """
    account: str
    settled_cash: float
    checked_at: datetime
    reference: str
    all_open_orders_reviewed: bool
    open_orders: tuple | None = None


class SchwabConfirmedAdapter:
    def __init__(self, client, account_number, operator_evidence=None):
        self.client, self.account = client, str(account_number)
        self.operator_evidence = operator_evidence
        if not account_number:
            raise ValueError("Explicit exact account required")
        response = client.get_account_numbers()
        response.raise_for_status()
        matches = [a for a in response.json() if str(a["accountNumber"]) == self.account]
        if len(matches) != 1:
            raise ValueError("No unique exact Schwab account match")
        self.account_hash = matches[0]["hashValue"]

    def snapshot(self):
        from schwab.client import Client
        response = self.client.get_account(self.account_hash, fields=[Client.Account.Fields.POSITIONS])
        response.raise_for_status()
        account = response.json()["securitiesAccount"]
        if str(account["accountNumber"]) != self.account:
            raise ValueError("Account response mismatch")
        now = datetime.now(timezone.utc)
        response = self.client.get_orders_for_account(self.account_hash,
            from_entered_datetime=now - timedelta(days=60), to_entered_datetime=now)
        response.raise_for_status()
        orders = response.json()
        positions = {}
        for p in account.get("positions", []):
            symbol = p["instrument"]["symbol"]
            positions[symbol] = positions.get(symbol, 0.) + float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
        # Absence is intentional. Current Schwab balance payloads do not provide
        # a documented universally reliable settled-cash field for this model.
        # Live buying stays blocked until a broker-verified evidence adapter is
        # supplied and separately validated; cashAvailableForTrading is not it.
        settled_cash, source, complete, reviewed_orders = None, "", False, []
        proof = self.operator_evidence
        if proof is not None:
            if (proof.account != self.account or not proof.reference
                    or not timedelta(0) <= now - proof.checked_at <= timedelta(seconds=60)
                    or not math.isfinite(proof.settled_cash) or proof.settled_cash < 0):
                raise ValueError("Invalid/expired operator broker evidence")
            settled_cash, source = proof.settled_cash, "operator: " + proof.reference
            if proof.all_open_orders_reviewed and proof.open_orders is None:
                raise ValueError("Full open-order review results required, not just a reviewed flag")
            reviewed_orders = list(proof.open_orders or ())
            complete = proof.all_open_orders_reviewed
        # The API only exposes a 60-day entry-date window; older GTC orders
        # require an explicit full open-order review on the broker platform.
        return Snapshot(self.account, account["type"], now, settled_cash, positions,
                        [o for o in orders if o.get("status") not in TERMINAL] + reviewed_orders,
                        source, complete)

    def submit(self, intent):
        """Only called by ConfirmedExecutor after per-order human approval."""
        from schwab.orders.equities import equity_buy_limit, equity_sell_limit
        builder = equity_buy_limit if intent.side == "BUY" else equity_sell_limit
        # These templates use NORMAL session, DAY duration and LIMIT type.
        spec = builder(intent.symbol, intent.quantity, f"{intent.limit_price:.2f}").build()
        response = self.client.place_order(self.account_hash, spec)
        response.raise_for_status()
        location = response.headers.get("Location", "")
        order_id = location.rsplit("/", 1)[-1]
        if not order_id.isdigit():
            raise ValueError("Accepted order response lacks valid id")
        return order_id

    def order(self, order_id):
        response = self.client.get_order(order_id, self.account_hash)
        response.raise_for_status()
        order = response.json()
        legs = order.get("orderLegCollection", [])
        if len(legs) != 1 or order.get("orderType") != "LIMIT":
            raise ValueError("Not a supported single ETF limit order")
        leg = legs[0]
        return {"symbol": leg["instrument"]["symbol"], "side": leg["instruction"],
            "quantity": int(leg["quantity"]), "limit_price": float(order["price"]),
            "entered_at": order["enteredTime"], "status": order["status"], "raw": order}
