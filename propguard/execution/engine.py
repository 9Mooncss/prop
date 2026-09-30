"""ExecutionEngine: idempotent, audited order execution strictly through PreTradeGuard."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from propguard.execution.guard import GuardResult, PreTradeGuard
from propguard.execution.interfaces import (
    BrokerAdapter,
    BrokerDisconnected,
    BrokerError,
    BrokerTimeout,
    ExecutionReport,
    OrderStatus,
)
from propguard.execution.live_gate import PAPER_ONLY_GATE, LiveGate
from propguard.execution.stores import Stores
from propguard.risk.engine import RiskContext
from propguard.risk.models import OrderRequest
from propguard.risk.policy import KillSwitchKind, RiskDecision


def make_client_order_id(account_id: str, strategy_id: str, signal_id: str, intent: str) -> str:
    """Deterministic id: the same signal can never produce two different orders."""
    h = hashlib.sha256(f"{account_id}|{strategy_id}|{signal_id}|{intent}".encode()).hexdigest()[:20]
    return f"pg-{h}"


def order_to_dict(o: OrderRequest) -> dict:
    return {
        "client_order_id": o.client_order_id, "account_id": o.account_id, "symbol": o.symbol,
        "side": o.side.value, "lots": str(o.lots), "intent": o.intent.value, "order_type": o.order_type.value,
        "price": None if o.price is None else str(o.price),
        "stop_loss": None if o.stop_loss is None else str(o.stop_loss),
        "take_profit": None if o.take_profit is None else str(o.take_profit),
        "position_id": o.position_id, "target_broker_order_id": o.target_broker_order_id,
        "strategy_id": o.strategy_id, "emergency": o.emergency,
    }


@dataclass(frozen=True)
class ExecutionOutcome:
    client_order_id: str
    status: OrderStatus
    decision: RiskDecision | None
    report: ExecutionReport | None = None
    duplicate: bool = False
    message: str = ""


class ExecutionEngine:
    def __init__(self, account_id: str, adapter: BrokerAdapter, stores: Stores,
                 context_fn: Callable[[], RiskContext], guard: PreTradeGuard | None = None,
                 live_gate: LiveGate = PAPER_ONLY_GATE, max_send_retries: int = 2) -> None:
        self.account_id = account_id
        self._adapter = adapter  # private: never handed to strategies
        self.stores = stores
        self.context_fn = context_fn
        self.guard = guard or PreTradeGuard()
        self.live_gate = live_gate
        self.max_send_retries = max_send_retries
        live_gate.check(account_id, adapter.capabilities.is_live)

    @property
    def adapter_name(self) -> str:
        return self._adapter.capabilities.name

    def execute(self, order: OrderRequest) -> ExecutionOutcome:
        st = self.stores
        if not st.orders.reserve(order.client_order_id, self.account_id, order_to_dict(order)):
            existing = st.orders.get(order.client_order_id) or {}
            st.audit.record("order.duplicate_blocked", self.account_id, {"client_order_id": order.client_order_id,
                                                                          "existing_status": existing.get("status")})
            return ExecutionOutcome(order.client_order_id, OrderStatus(existing.get("status", "UNKNOWN")), None,
                                    duplicate=True, message="duplicate client_order_id: not resubmitted")
        return self._evaluate_and_send(order, attempt=0)

    def _evaluate_and_send(self, order: OrderRequest, attempt: int) -> ExecutionOutcome:
        st = self.stores
        ctx = self.context_fn()
        # the id was just reserved by us; exclude it from the duplicate check for this evaluation
        ctx = RiskContext(**{**ctx.__dict__, "seen_client_order_ids": ctx.seen_client_order_ids - {order.client_order_id}})
        gr: GuardResult = self.guard.authorize(order, ctx, adapter_name=self.adapter_name)
        d = gr.decision
        st.audit.record("risk.decision", self.account_id, {
            "decision_id": gr.decision_id, "request": order_to_dict(order), "decision": d.to_dict(),
            "state_snapshot": _snapshot_summary(ctx), "attempt": attempt})
        if gr.approved is None:
            st.orders.update(order.client_order_id, status=OrderStatus.REJECTED_BY_RISK.value,
                             decision=d.to_dict())
            return ExecutionOutcome(order.client_order_id, OrderStatus.REJECTED_BY_RISK, d, message=d.explanation)
        st.orders.update(order.client_order_id, status=OrderStatus.SUBMITTING.value, decision=d.to_dict(),
                         approved_lots=str(gr.approved.order.lots))
        try:
            self.live_gate.check(self.account_id, self._adapter.capabilities.is_live)
            rep = self._adapter.submit(gr.approved)
        except BrokerTimeout as exc:
            return self._resolve_timeout(order, d, str(exc), attempt)
        except BrokerDisconnected as exc:
            st.audit.record("order.send_failed", self.account_id, {"client_order_id": order.client_order_id,
                                                                    "error": str(exc), "attempt": attempt})
            if attempt < self.max_send_retries:
                try:
                    self._adapter.connect()
                except Exception:  # noqa: BLE001 - connection errors end the retry loop below
                    pass
                if self._adapter.is_connected():
                    try:
                        found = self._adapter.find_order(order.client_order_id)
                    except (BrokerError, BrokerTimeout) as lexc:
                        return self._resolve_timeout(order, d, f"lookup after reconnect failed: {lexc}",
                                                     self.max_send_retries)
                    if found is not None:
                        return self._apply_report(order, d, found)
                    return self._evaluate_and_send(order, attempt + 1)  # fresh state, fresh approval
            st.orders.update(order.client_order_id, status=OrderStatus.FAILED.value, error=str(exc))
            return ExecutionOutcome(order.client_order_id, OrderStatus.FAILED, d, message=str(exc))
        except BrokerError as exc:
            st.orders.update(order.client_order_id, status=OrderStatus.REJECTED_BY_BROKER.value, error=str(exc))
            st.audit.record("order.broker_error", self.account_id, {"client_order_id": order.client_order_id,
                                                                     "error": str(exc)})
            return ExecutionOutcome(order.client_order_id, OrderStatus.REJECTED_BY_BROKER, d, message=str(exc))
        return self._apply_report(order, d, rep)

    def _resolve_timeout(self, order: OrderRequest, d: RiskDecision, err: str, attempt: int) -> ExecutionOutcome:
        st = self.stores
        st.orders.update(order.client_order_id, status=OrderStatus.UNKNOWN.value, error=err)
        st.audit.record("order.timeout", self.account_id, {"client_order_id": order.client_order_id, "error": err})
        found = None
        lookup_ok = False
        try:
            found = self._adapter.find_order(order.client_order_id)
            lookup_ok = True
        except (BrokerError, BrokerTimeout) as exc:
            st.audit.record("order.lookup_failed", self.account_id, {"client_order_id": order.client_order_id,
                                                                      "error": str(exc)})
        if found is not None:
            return self._apply_report(order, d, found)
        caps = self._adapter.capabilities
        # resend ONLY on a definitive negative lookup; a failed lookup is ambiguous -> never resend
        if lookup_ok and caps.supports_client_order_id and attempt < self.max_send_retries:
            # platform confirms it does not know this client id -> safe to re-evaluate and resend
            return self._evaluate_and_send(order, attempt + 1)
        st.kill_switches.activate(self.account_id, KillSwitchKind.AMBIGUOUS_EXECUTION_EVENT,
                                  f"order {order.client_order_id} outcome unknown after timeout")
        return ExecutionOutcome(order.client_order_id, OrderStatus.UNKNOWN, d,
                                message="outcome unknown; kill switch raised; reconciliation required")

    def _apply_report(self, order: OrderRequest, d: RiskDecision, rep: ExecutionReport) -> ExecutionOutcome:
        st = self.stores
        st.orders.update(order.client_order_id, status=rep.status.value, broker_order_id=rep.broker_order_id,
                         filled_lots=str(rep.filled_lots),
                         avg_price=None if rep.avg_price is None else str(rep.avg_price),
                         position_id=rep.position_id, message=rep.message)
        if rep.position_id and rep.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            if order.intent.value == "OPEN":
                st.ledger.upsert_position(self.account_id, rep.position_id, {
                    "symbol": order.symbol, "lots": str(rep.filled_lots), "side": order.side.value,
                    "client_order_id": order.client_order_id})
        st.audit.record("order.report", self.account_id, {
            "client_order_id": order.client_order_id, "status": rep.status.value,
            "filled_lots": str(rep.filled_lots), "avg_price": None if rep.avg_price is None else str(rep.avg_price),
            "broker_order_id": rep.broker_order_id, "position_id": rep.position_id})
        return ExecutionOutcome(order.client_order_id, rep.status, d, rep)


def _snapshot_summary(ctx: RiskContext) -> dict:
    s = ctx.snapshot
    st = ctx.state
    return {
        "now": ctx.now.isoformat(),
        "balance": str(s.balance) if s else None,
        "equity": str(s.equity) if s else None,
        "positions": [{"id": p.position_id, "sym": p.symbol, "side": p.side.value, "lots": str(p.lots),
                       "sl": None if p.stop_loss is None else str(p.stop_loss)} for p in (s.positions if s else ())],
        "pending_orders": len(s.pending_orders) if s else 0,
        "day_start_balance": str(st.day_start_balance) if st and st.day_start_balance is not None else None,
        "hwm_equity": str(st.hwm_equity) if st and st.hwm_equity is not None else None,
        "trading_date": st.trading_date.isoformat() if st and st.trading_date else None,
        "kill_switches": [k.value for k in ctx.active_kill_switches],
        "reconciliation_ok": ctx.reconciliation_ok,
        "ruleset_id": ctx.ruleset.ruleset_id,
    }
