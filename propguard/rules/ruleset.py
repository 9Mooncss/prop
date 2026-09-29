"""Compiled rule set consumed by the deterministic Risk Engine.

A ``RuleSet`` is an immutable snapshot of the *active* rules of one challenge phase, including
each rule's interpretation status and verification time. The Risk Engine never reads the
database directly -- it receives a ``RuleSet`` and decides based only on it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from propguard.rules.types import (
    Criticality,
    InterpretationStatus,
    RuleParams,
    criticality_of,
    parse_params,
)

T = TypeVar("T", bound=RuleParams)


@dataclass(frozen=True)
class RuleRecord:
    rule_id: str
    kind: str
    params: RuleParams
    raw_text: str = ""
    status: InterpretationStatus = InterpretationStatus.CONFIRMED
    confidence: Decimal = Decimal("1")
    verified_at: datetime | None = None
    evidence_ids: tuple[str, ...] = ()
    version: int = 1

    @property
    def criticality(self) -> Criticality:
        return criticality_of(self.kind)

    @property
    def is_confirmed(self) -> bool:
        return self.status == InterpretationStatus.CONFIRMED


@dataclass(frozen=True)
class RuleSet:
    ruleset_id: str
    firm_slug: str
    program_slug: str
    phase: str
    initial_balance: Decimal
    rules: tuple[RuleRecord, ...] = field(default_factory=tuple)
    # firm-level signal that something critical changed and is pending review
    pending_critical_change: bool = False

    def get(self, cls: type[T]) -> tuple[T | None, RuleRecord | None]:
        for r in self.rules:
            if r.kind == cls.kind:
                return r.params, r  # type: ignore[return-value]
        return None, None

    def records_of_kind(self, kind: str) -> list[RuleRecord]:
        return [r for r in self.rules if r.kind == kind]

    def non_confirmed_critical(self) -> list[RuleRecord]:
        return [r for r in self.rules if r.criticality == Criticality.CRITICAL and not r.is_confirmed]

    def oldest_critical_verification(self) -> datetime | None:
        times = [r.verified_at for r in self.rules if r.criticality == Criticality.CRITICAL]
        if not times or any(t is None for t in times):
            return None
        return min(times)  # type: ignore[type-var]

    def fingerprint(self) -> str:
        payload = {
            "firm": self.firm_slug,
            "program": self.program_slug,
            "phase": self.phase,
            "initial": str(self.initial_balance),
            "rules": sorted(
                [
                    {
                        "id": r.rule_id,
                        "kind": r.kind,
                        "v": r.version,
                        "status": r.status.value,
                        "params": r.params.model_dump(mode="json"),
                    }
                    for r in self.rules
                ],
                key=lambda x: x["id"],
            ),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def build_ruleset(
    *,
    firm_slug: str,
    program_slug: str,
    phase: str,
    initial_balance: Decimal | int | str,
    rules: list[dict[str, Any]],
    verified_at: datetime | None = None,
    pending_critical_change: bool = False,
) -> RuleSet:
    """Convenience builder from plain dicts: {kind, params, status?, rule_id?, text?}."""
    recs = []
    for i, r in enumerate(rules):
        kind = r["type"] if "type" in r else r["kind"]
        recs.append(
            RuleRecord(
                rule_id=r.get("rule_id") or f"{firm_slug}:{program_slug}:{phase}:{kind}:{i}",
                kind=kind,
                params=parse_params(kind, r.get("params")),
                raw_text=r.get("text", ""),
                status=InterpretationStatus(r.get("status", "CONFIRMED")),
                confidence=Decimal(str(r.get("confidence", "1"))),
                verified_at=r.get("verified_at", verified_at),
                evidence_ids=tuple(r.get("evidence", ())),
                version=int(r.get("version", 1)),
            )
        )
    rs = RuleSet(
        ruleset_id="",
        firm_slug=firm_slug,
        program_slug=program_slug,
        phase=phase,
        initial_balance=Decimal(str(initial_balance)),
        rules=tuple(recs),
        pending_critical_change=pending_critical_change,
    )
    return RuleSet(**{**rs.__dict__, "ruleset_id": rs.fingerprint()})
