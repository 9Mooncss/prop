"""Deterministic normalization of extracted rule params into the typed Rule Registry schema.

Anything that cannot be mapped exactly is *not guessed*: unknown/invalid fields are dropped from the
normalized params and the rule is marked UNCERTAIN with an explanation, while the raw params and
text are kept verbatim for review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from propguard.rules.types import RULE_TYPES, InterpretationStatus, parse_params

PARSER_VERSION = "normalize/1.0.0"

DOC_PRIORITY = {
    "TERMS": 10,
    "TRADING_RULES": 20,
    "RESTRICTED_COUNTRIES": 25,
    "FAQ": 30,
    "KYC_POLICY": 35,
    "PAYOUT_POLICY": 35,
    "PLATFORM_RULES": 40,
    "MARKETING": 80,
    "COMMUNITY": 95,
}
PRIMARY_DOC_TYPES = {k for k, v in DOC_PRIORITY.items() if v < 90}

# field aliases seen in extracted data -> schema field
_ALIASES: dict[str, dict[str, str]] = {
    "profit_target": {"target_pct": "pct", "percent": "pct"},
    "daily_loss_limit": {"percent": "pct", "tz": "reset_tz", "timezone": "reset_tz"},
    "max_loss": {"percent": "pct", "type": "mode"},
    "min_trading_days": {"min_days": "days"},
    "max_duration": {"max_days": "days"},
    "profit_split": {"split_pct": "pct", "percent": "pct"},
    "consistency_rule": {"pct": "max_single_day_pct_of_total"},
}


@dataclass(frozen=True)
class NormalizedRule:
    kind: str
    params: dict[str, Any]
    raw_params: dict[str, Any]
    status: InterpretationStatus
    notes: str


def normalize_rule(kind: str, raw: dict[str, Any] | None, base_status: InterpretationStatus) -> NormalizedRule:
    raw = dict(raw or {})
    if kind not in RULE_TYPES:
        return NormalizedRule("custom", {"name": kind}, raw, InterpretationStatus.UNCERTAIN,
                              f"unknown rule kind '{kind}' stored as custom")
    aliases = _ALIASES.get(kind, {})
    params: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in raw.items():
        k2 = aliases.get(k, k)
        if v is None:
            dropped.append(f"{k}=null")
            continue
        params[k2] = v
    fields = RULE_TYPES[kind].model_fields
    for k in list(params):
        if k not in fields:
            dropped.append(f"{k}={params.pop(k)!r} (not in schema)")
    # drop invalid values one at a time, never substitute defaults silently for critical kinds
    for _ in range(len(params) + 1):
        try:
            model = parse_params(kind, params)
            break
        except ValueError as exc:
            bad = _first_bad_field(exc, params)
            if bad is None:
                return NormalizedRule(kind, {}, raw, InterpretationStatus.UNCERTAIN, f"unparseable: {exc}")
            dropped.append(f"{bad}={params.pop(bad)!r} (invalid)")
    else:  # pragma: no cover
        return NormalizedRule(kind, {}, raw, InterpretationStatus.UNCERTAIN, "unparseable")
    status = base_status
    notes = ""
    # fields relying on schema defaults for critical semantics are uncertain
    implicit = _implicit_critical_fields(kind, params)
    if dropped or implicit:
        status = InterpretationStatus.UNCERTAIN if base_status != InterpretationStatus.CONFLICT else base_status
        parts = []
        if dropped:
            parts.append("dropped: " + ", ".join(dropped))
        if implicit:
            parts.append("not stated by source (schema default used, needs confirmation): " + ", ".join(implicit))
        notes = "; ".join(parts)
    return NormalizedRule(kind, model.model_dump(mode="json"), raw, status, notes)


_CRITICAL_FIELDS = {
    "daily_loss_limit": ["pct", "pct_of", "reference", "includes_floating", "reset_time", "reset_tz"],
    "max_loss": ["pct", "mode", "basis"],
    "weekend_holding": ["allowed"],
    "overnight_holding": ["allowed"],
    "news_trading": ["allowed"],
    "ea_policy": ["allowed"],
    "api_trading": ["allowed"],
}


def _implicit_critical_fields(kind: str, params: dict[str, Any]) -> list[str]:
    need = _CRITICAL_FIELDS.get(kind, [])
    if kind == "daily_loss_limit" and params.get("enabled") is False:
        return []
    return [f for f in need if f not in params]


def _first_bad_field(exc: ValueError, params: dict[str, Any]) -> str | None:
    msg = str(exc)
    for k in params:
        if f"'{k}'" in msg or f"('{k}',)" in msg:
            return k
    return next(iter(params), None) if params else None
