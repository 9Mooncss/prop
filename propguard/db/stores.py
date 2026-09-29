"""SQL implementations of the execution-layer store protocols."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from propguard.db.models import (
    AuditLog,
    KillSwitchRow,
    LedgerPosition,
    OrderRow,
    ProcessedEvent,
    RiskStateRow,
    TradingAccount,
)
from propguard.db.session import session_scope
from propguard.execution.stores import Stores, chain_hash
from propguard.risk.models import RiskState
from propguard.risk.policy import KillSwitchKind


class SqlOrderStore:
    def __init__(self, f: sessionmaker[Session]) -> None:
        self.f = f

    def reserve(self, client_order_id, account_id, request):
        try:
            with session_scope(self.f) as s:
                s.add(OrderRow(client_order_id=client_order_id, account_id=account_id, status="RESERVED",
                               request=request, data={}))
            return True
        except IntegrityError:
            return False

    def get(self, client_order_id):
        with session_scope(self.f) as s:
            r = s.get(OrderRow, client_order_id)
            if r is None:
                return None
            return {"client_order_id": r.client_order_id, "account_id": r.account_id, "status": r.status,
                    "request": r.request, **(r.data or {})}

    def update(self, client_order_id, **fields):
        with session_scope(self.f) as s:
            r = s.get(OrderRow, client_order_id)
            if "status" in fields:
                r.status = fields.pop("status")
            r.data = {**(r.data or {}), **fields}

    def by_status(self, account_id, statuses):
        with session_scope(self.f) as s:
            rows = s.scalars(select(OrderRow).where(OrderRow.account_id == account_id,
                                                    OrderRow.status.in_(statuses))).all()
            return [{"client_order_id": r.client_order_id, "account_id": r.account_id, "status": r.status,
                     "request": r.request, **(r.data or {})} for r in rows]

    def seen_ids(self, account_id):
        with session_scope(self.f) as s:
            return frozenset(s.scalars(select(OrderRow.client_order_id).where(OrderRow.account_id == account_id)))


class SqlStateStore:
    def __init__(self, f):
        self.f = f

    def load(self, account_id):
        with session_scope(self.f) as s:
            r = s.get(RiskStateRow, account_id)
            return RiskState.from_dict(r.state) if r else None

    def save(self, state):
        with session_scope(self.f) as s:
            r = s.get(RiskStateRow, state.account_id)
            if r is None:
                s.add(RiskStateRow(account_id=state.account_id, state=state.to_dict()))
            else:
                r.state = state.to_dict()


class SqlKillSwitchStore:
    def __init__(self, f):
        self.f = f

    def active(self, account_id):
        with session_scope(self.f) as s:
            rows = s.scalars(select(KillSwitchRow.kind).where(KillSwitchRow.account_id == account_id,
                                                              KillSwitchRow.cleared_at.is_(None))).all()
            return [KillSwitchKind(k) for k in dict.fromkeys(rows)]

    def activate(self, account_id, kind, reason, details=None):
        with session_scope(self.f) as s:
            exists = s.scalar(select(func.count()).select_from(KillSwitchRow).where(
                KillSwitchRow.account_id == account_id, KillSwitchRow.kind == kind.value,
                KillSwitchRow.cleared_at.is_(None)))
            if exists:
                return False
            s.add(KillSwitchRow(account_id=account_id, kind=kind.value, reason=reason, details=details or {}))
            return True

    def clear(self, account_id, kind, actor, note):
        with session_scope(self.f) as s:
            r = s.scalars(select(KillSwitchRow).where(KillSwitchRow.account_id == account_id,
                                                      KillSwitchRow.kind == kind.value,
                                                      KillSwitchRow.cleared_at.is_(None))).first()
            if r is None:
                return False
            r.cleared_at, r.cleared_by, r.note = datetime.now(timezone.utc), actor, note
            return True


def _append_audit(s: Session, kind: str, account_id: str | None, payload: dict[str, Any]) -> str:
    if s.bind is not None and s.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        s.execute(text("SELECT pg_advisory_xact_lock(724001)"))  # serialize chain appends
    last = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
    pending = [o for o in s.new if isinstance(o, AuditLog)]
    prev = pending[-1].hash if pending else (last.hash if last else "0" * 64)
    body = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "account_id": account_id,
            "payload": _jsonable(payload)}
    h = chain_hash(prev, body)
    s.add(AuditLog(ts=body["ts"], kind=kind, account_id=account_id, payload=body["payload"],
                   prev_hash=prev, hash=h))
    s.flush()
    return h


class SessionAudit:
    """Audit writer bound to an existing session (same transaction as the change it records)."""

    def __init__(self, s: Session) -> None:
        self.s = s

    def record(self, kind, account_id, payload):
        return _append_audit(self.s, kind, account_id, payload)


class SqlAudit:
    """Append-only, hash-chained audit log in its own short transactions. ``verify_chain`` detects
    tampering or gaps."""

    def __init__(self, f):
        self.f = f

    def record(self, kind, account_id, payload):
        with session_scope(self.f) as s:
            return _append_audit(s, kind, account_id, payload)

    def verify_chain(self) -> tuple[bool, int | None]:
        with session_scope(self.f) as s:
            prev = "0" * 64
            for r in s.scalars(select(AuditLog).order_by(AuditLog.id)):
                body = {"ts": r.ts, "kind": r.kind, "account_id": r.account_id, "payload": r.payload}
                if r.prev_hash != prev or chain_hash(prev, body) != r.hash:
                    return False, r.id
                prev = r.hash
            return True, None


class SqlLedger:
    def __init__(self, f):
        self.f = f

    def positions(self, account_id):
        with session_scope(self.f) as s:
            return {r.position_id: dict(r.data) for r in
                    s.scalars(select(LedgerPosition).where(LedgerPosition.account_id == account_id))}

    def upsert_position(self, account_id, position_id, data):
        with session_scope(self.f) as s:
            r = s.get(LedgerPosition, (account_id, position_id))
            if r is None:
                s.add(LedgerPosition(account_id=account_id, position_id=position_id, data=dict(data)))
            else:
                r.data = dict(data)

    def remove_position(self, account_id, position_id):
        with session_scope(self.f) as s:
            r = s.get(LedgerPosition, (account_id, position_id))
            if r is not None:
                s.delete(r)

    def mark_event(self, account_id, event_id, sequence):
        try:
            with session_scope(self.f) as s:
                s.add(ProcessedEvent(account_id=account_id, event_id=event_id, sequence=sequence))
            return True
        except IntegrityError:
            return False

    def last_sequence(self, account_id):
        with session_scope(self.f) as s:
            return s.scalar(select(func.max(ProcessedEvent.sequence)).where(
                ProcessedEvent.account_id == account_id)) or 0


class SqlLiveApprovals:
    def __init__(self, f):
        self.f = f

    def is_live_enabled(self, account_id: str) -> bool:
        from propguard.execution.live_gate import code_fingerprint
        with session_scope(self.f) as s:
            a = s.get(TradingAccount, account_id)
            return bool(a and a.mode == "LIVE" and a.live_approved
                        and a.live_approval_fingerprint == code_fingerprint())


def sql_stores(f: sessionmaker[Session]) -> Stores:
    return Stores(SqlOrderStore(f), SqlStateStore(f), SqlKillSwitchStore(f), SqlAudit(f), SqlLedger(f))


def _jsonable(v: Any) -> Any:
    import json
    return json.loads(json.dumps(v, default=str))
