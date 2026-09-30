"""Reconciliation between this system's ledger/order store and the platform's reported state."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from propguard.execution.interfaces import BrokerAdapter, OrderStatus
from propguard.execution.stores import LedgerStore, OrderStore
from propguard.risk.models import AccountSnapshot
from propguard.risk.policy import KillSwitchKind


@dataclass
class ReconResult:
    ok: bool
    issues: list[tuple[KillSwitchKind, str]] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)


class ReconciliationService:
    def __init__(self, orders: OrderStore, ledger: LedgerStore) -> None:
        self.orders = orders
        self.ledger = ledger

    def reconcile(self, account_id: str, snapshot: AccountSnapshot, adapter: BrokerAdapter) -> ReconResult:
        res = ReconResult(ok=True)
        ledger = self.ledger.positions(account_id)
        broker_ids = {p.position_id for p in snapshot.positions}
        for p in snapshot.positions:
            if p.client_order_id is None:
                res.issues.append((KillSwitchKind.UNEXPECTED_MANUAL_TRADE,
                                   f"position {p.position_id} {p.side} {p.lots} {p.symbol} not opened by system"))
                continue
            rec = self.orders.get(p.client_order_id)
            if rec is None:
                res.issues.append((KillSwitchKind.UNKNOWN_POSITION,
                                   f"position {p.position_id} has unknown client id {p.client_order_id}"))
                continue
            if p.position_id not in ledger:
                # late fill (e.g. response lost): the order is ours, adopt it and record it
                self.ledger.upsert_position(account_id, p.position_id,
                                            {"symbol": p.symbol, "lots": str(p.lots), "side": p.side.value,
                                             "client_order_id": p.client_order_id})
                res.healed.append(f"adopted {p.position_id} from own order {p.client_order_id}")
            elif ledger[p.position_id].get("lots") not in (None, str(p.lots)):
                known = Decimal(ledger[p.position_id]["lots"])
                if p.lots > known:  # exposure grew without our order: never trust it
                    res.issues.append((KillSwitchKind.UNEXPECTED_MANUAL_TRADE,
                                       f"position {p.position_id} grew from {known} to {p.lots} lots outside the system"))
                else:  # partial close / stop-out: shrinking exposure is safe to adopt
                    self.ledger.upsert_position(account_id, p.position_id, {**ledger[p.position_id], "lots": str(p.lots)})
                    res.healed.append(f"lots of {p.position_id} reduced to {p.lots}")
        for pid in ledger:
            if pid not in broker_ids:
                res.issues.append((KillSwitchKind.RECONCILIATION_MISMATCH,
                                   f"ledger position {pid} missing at platform without close event"))
        for po in snapshot.pending_orders:
            if po.client_order_id is None or self.orders.get(po.client_order_id) is None:
                res.issues.append((KillSwitchKind.UNKNOWN_POSITION, f"unknown pending order {po.broker_order_id}"))
        for rec in self.orders.by_status(account_id, {OrderStatus.UNKNOWN.value, OrderStatus.SUBMITTING.value}):
            found = adapter.find_order(rec["client_order_id"])
            if found is None:
                res.issues.append((KillSwitchKind.AMBIGUOUS_EXECUTION_EVENT,
                                   f"order {rec['client_order_id']} status unknown and not found at platform"))
            else:
                self.orders.update(rec["client_order_id"], status=found.status.value,
                                   broker_order_id=found.broker_order_id, filled_lots=str(found.filled_lots))
                res.healed.append(f"resolved {rec['client_order_id']} -> {found.status.value}")
        res.ok = not res.issues
        return res
