"""Persistence protocols used by the execution layer, plus in-memory implementations.

SQL implementations live in ``propguard.db.stores`` and implement the same protocols.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from propguard.risk.models import RiskState
from propguard.risk.policy import KillSwitchKind


class OrderStore(Protocol):
    def reserve(self, client_order_id: str, account_id: str, request: dict[str, Any]) -> bool: ...
    def get(self, client_order_id: str) -> dict[str, Any] | None: ...
    def update(self, client_order_id: str, **fields: Any) -> None: ...
    def by_status(self, account_id: str, statuses: set[str]) -> list[dict[str, Any]]: ...
    def seen_ids(self, account_id: str) -> frozenset[str]: ...


class StateStore(Protocol):
    def load(self, account_id: str) -> RiskState | None: ...
    def save(self, state: RiskState) -> None: ...


class KillSwitchStore(Protocol):
    def active(self, account_id: str) -> list[KillSwitchKind]: ...
    def activate(self, account_id: str, kind: KillSwitchKind, reason: str, details: dict | None = None) -> bool: ...
    def clear(self, account_id: str, kind: KillSwitchKind, actor: str, note: str) -> bool: ...


class AuditSink(Protocol):
    def record(self, kind: str, account_id: str | None, payload: dict[str, Any]) -> str: ...


class LedgerStore(Protocol):
    """Positions this system believes are open, and processed broker event ids."""

    def positions(self, account_id: str) -> dict[str, dict[str, Any]]: ...
    def upsert_position(self, account_id: str, position_id: str, data: dict[str, Any]) -> None: ...
    def remove_position(self, account_id: str, position_id: str) -> None: ...
    def mark_event(self, account_id: str, event_id: str, sequence: int) -> bool: ...
    def last_sequence(self, account_id: str) -> int: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryOrderStore:
    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def reserve(self, client_order_id, account_id, request):
        with self._lock:
            if client_order_id in self._d:
                return False
            self._d[client_order_id] = {"client_order_id": client_order_id, "account_id": account_id,
                                        "status": "RESERVED", "request": request, "created_at": _now()}
            return True

    def get(self, client_order_id):
        v = self._d.get(client_order_id)
        return dict(v) if v else None

    def update(self, client_order_id, **fields):
        with self._lock:
            self._d[client_order_id].update(fields, updated_at=_now())

    def by_status(self, account_id, statuses):
        return [dict(v) for v in self._d.values() if v["account_id"] == account_id and v["status"] in statuses]

    def seen_ids(self, account_id):
        return frozenset(k for k, v in self._d.items() if v["account_id"] == account_id)


class MemoryStateStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def load(self, account_id):
        d = self._d.get(account_id)
        return RiskState.from_dict(d) if d else None

    def save(self, state):
        self._d[state.account_id] = state.to_dict()


@dataclass
class _KS:
    kind: KillSwitchKind
    reason: str
    details: dict
    activated_at: str
    cleared_at: str | None = None
    cleared_by: str | None = None
    note: str = ""


class MemoryKillSwitchStore:
    def __init__(self) -> None:
        self._d: dict[str, list[_KS]] = {}

    def active(self, account_id):
        return [k.kind for k in self._d.get(account_id, []) if k.cleared_at is None]

    def activate(self, account_id, kind, reason, details=None):
        if kind in self.active(account_id):
            return False
        self._d.setdefault(account_id, []).append(_KS(kind, reason, details or {}, _now()))
        return True

    def clear(self, account_id, kind, actor, note):
        for k in self._d.get(account_id, []):
            if k.kind == kind and k.cleared_at is None:
                k.cleared_at, k.cleared_by, k.note = _now(), actor, note
                return True
        return False

    def history(self, account_id):
        return list(self._d.get(account_id, []))


@dataclass
class MemoryAudit:
    """Hash-chained in-memory audit log (same chaining as the SQL implementation)."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind, account_id, payload):
        prev = self.entries[-1]["hash"] if self.entries else "0" * 64
        body = {"ts": _now(), "kind": kind, "account_id": account_id, "payload": payload, "prev_hash": prev}
        h = chain_hash(prev, body)
        self.entries.append({**body, "hash": h})
        return h

    def of_kind(self, kind):
        return [e for e in self.entries if e["kind"] == kind]


def chain_hash(prev: str, body: dict[str, Any]) -> str:
    data = json.dumps({k: body[k] for k in ("ts", "kind", "account_id", "payload")}, sort_keys=True, default=str)
    return hashlib.sha256((prev + data).encode()).hexdigest()


class MemoryLedger:
    def __init__(self) -> None:
        self._pos: dict[str, dict[str, dict]] = {}
        self._events: dict[str, set[str]] = {}
        self._seq: dict[str, int] = {}

    def positions(self, account_id):
        return dict(self._pos.get(account_id, {}))

    def upsert_position(self, account_id, position_id, data):
        self._pos.setdefault(account_id, {})[position_id] = dict(data)

    def remove_position(self, account_id, position_id):
        self._pos.get(account_id, {}).pop(position_id, None)

    def mark_event(self, account_id, event_id, sequence):
        s = self._events.setdefault(account_id, set())
        if event_id in s:
            return False
        s.add(event_id)
        self._seq[account_id] = max(self._seq.get(account_id, 0), sequence)
        return True

    def last_sequence(self, account_id):
        return self._seq.get(account_id, 0)


@dataclass
class Stores:
    orders: OrderStore
    state: StateStore
    kill_switches: KillSwitchStore
    audit: AuditSink
    ledger: LedgerStore

    @classmethod
    def memory(cls) -> "Stores":
        return cls(MemoryOrderStore(), MemoryStateStore(), MemoryKillSwitchStore(), MemoryAudit(), MemoryLedger())
