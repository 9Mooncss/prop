"""PreTradeGuard: the single gate between intent and platform.

It evaluates every ``OrderRequest`` with the deterministic ``RiskEngine`` and, only on ALLOW or
MODIFY, mints an ``ApprovedOrder``: an HMAC-signed, short-lived, single-use token binding the exact
order fields (with the approved size) to the decision. ``BrokerAdapter.submit`` refuses anything
else. The signing key is generated per process and never persisted or logged.

Python cannot make bypass physically impossible for code running in the same interpreter, so this
is layered with: adapters never exposed to strategies, ``__init_subclass__`` preventing overrides
of ``submit``, and a static test (``tests/integration/test_no_bypass.py``) that fails if any module
other than the adapter base calls the protected ``_place/_close/_cancel/_modify_sl`` hooks or
touches the signing key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, replace

from propguard.execution.interfaces import BypassAttempt
from propguard.risk.engine import RiskContext, RiskEngine
from propguard.risk.models import OrderRequest
from propguard.risk.policy import Action, RiskDecision

_KEY = secrets.token_bytes(32)
_CONSUMED: set[str] = set()
_LOCK = threading.Lock()
APPROVAL_TTL_S = 5.0


def _canonical(order: OrderRequest) -> bytes:
    d = {
        "coid": order.client_order_id,
        "acc": order.account_id,
        "sym": order.symbol,
        "side": order.side.value,
        "lots": str(order.lots),
        "intent": order.intent.value,
        "type": order.order_type.value,
        "price": None if order.price is None else str(order.price),
        "sl": None if order.stop_loss is None else str(order.stop_loss),
        "tp": None if order.take_profit is None else str(order.take_profit),
        "pos": order.position_id,
        "tgt": order.target_broker_order_id,
        "em": order.emergency,
    }
    return json.dumps(d, sort_keys=True).encode()


@dataclass(frozen=True)
class ApprovedOrder:
    order: OrderRequest
    decision_id: str
    adapter_name: str
    expires_at: float
    signature: str


def _sign(order: OrderRequest, decision_id: str, adapter_name: str, expires_at: float) -> str:
    msg = _canonical(order) + b"|" + decision_id.encode() + b"|" + adapter_name.encode() + b"|" + repr(
        expires_at).encode()
    return hmac.new(_KEY, msg, hashlib.sha256).hexdigest()


def verify_and_consume(approved: object, *, adapter_name: str) -> OrderRequest:
    if not isinstance(approved, ApprovedOrder):
        raise BypassAttempt("broker submit requires an ApprovedOrder from PreTradeGuard")
    expected = _sign(approved.order, approved.decision_id, approved.adapter_name, approved.expires_at)
    if not hmac.compare_digest(expected, approved.signature):
        raise BypassAttempt("invalid approval signature (order modified after approval or forged)")
    if approved.adapter_name != adapter_name:
        raise BypassAttempt("approval was issued for a different adapter")
    if time.time() > approved.expires_at:
        raise BypassAttempt("approval expired; re-evaluate with fresh state")
    with _LOCK:
        if approved.signature in _CONSUMED:
            raise BypassAttempt("approval already used (duplicate submission)")
        _CONSUMED.add(approved.signature)
    return approved.order


@dataclass(frozen=True)
class GuardResult:
    decision: RiskDecision
    decision_id: str
    approved: ApprovedOrder | None


class PreTradeGuard:
    def __init__(self, engine: RiskEngine | None = None, ttl_s: float = APPROVAL_TTL_S) -> None:
        self.engine = engine or RiskEngine()
        self.ttl_s = ttl_s

    def authorize(self, order: OrderRequest, ctx: RiskContext, *, adapter_name: str) -> GuardResult:
        decision = self.engine.evaluate(order, ctx)
        decision_id = str(uuid.uuid4())
        if decision.action == Action.DENY:
            return GuardResult(decision, decision_id, None)
        final = order
        if decision.action == Action.MODIFY:
            assert decision.approved_lots is not None
            final = replace(order, lots=decision.approved_lots)
        expires = time.time() + self.ttl_s
        sig = _sign(final, decision_id, adapter_name, expires)
        return GuardResult(decision, decision_id, ApprovedOrder(final, decision_id, adapter_name, expires, sig))
