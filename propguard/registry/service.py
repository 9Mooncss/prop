"""Rule Registry service: seed ingestion, provenance, versioning, conflicts, RuleSet assembly."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from propguard.db.models import (
    Alert,
    Challenge,
    Conflict,
    Evidence,
    Firm,
    Program,
    Rule,
    RuleChange,
    Source,
)
from propguard.db.session import aware
from propguard.execution.stores import AuditSink
from propguard.registry.normalize import DOC_PRIORITY, PARSER_VERSION, PRIMARY_DOC_TYPES, normalize_rule
from propguard.rules.ruleset import RuleRecord, RuleSet
from propguard.rules.types import Criticality, InterpretationStatus, criticality_of, parse_params

FIRM_STATUSES = ("VERIFIED", "WATCHLIST", "INSUFFICIENT_DATA", "EXCLUDED")
REQUIRED_DOC_TYPES = ("TERMS", "TRADING_RULES", "FAQ", "RESTRICTED_COUNTRIES", "KYC_POLICY", "PAYOUT_POLICY",
                      "PLATFORM_RULES")
# seed research used a summarizing fetch tool -> fragments are near-verbatim, not raw captures
SEED_CONFIDENCE = 0.6

_AUTOMATION_MAP = {
    "ea_bots": ("ea_policy", "allowed"),
    "api_trading": ("api_trading", "allowed"),
    "copy_trading": ("copy_trading", "allowed"),
    "vps_vpn": ("ip_vps_restriction", "vps_allowed"),
    "hft": ("hft_restriction", "allowed"),
}


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- seed ingestion


def load_seed_dir(s: Session, path: Path, audit: AuditSink | None = None) -> list[str]:
    loaded = []
    for f in sorted(path.glob("*.json")):
        loaded.append(load_seed(s, json.loads(f.read_text()), audit, origin=f.name))
    return loaded


def load_seed(s: Session, d: dict[str, Any], audit: AuditSink | None = None, origin: str = "") -> str:
    """Idempotent upsert of one firm's research seed. Rules are versioned, never overwritten."""
    firm = s.scalar(select(Firm).where(Firm.slug == d["slug"]))
    if firm is None:
        firm = Firm(slug=d["slug"], name=d["name"])
        s.add(firm)
    firm.name = d["name"]
    firm.official_url = d.get("official_url")
    firm.jurisdiction = d.get("jurisdiction", {})
    firm.kyc = d.get("kyc", {})
    firm.payout = d.get("payout", {})
    firm.payout_classification = (d.get("payout") or {}).get("classification", "UNKNOWN")
    firm.automation = d.get("automation", {})
    firm.platforms = d.get("platforms", [])
    firm.risk_signals = d.get("risk_signals", [])
    firm.unknowns = d.get("unknowns", [])
    firm.researched_at = d.get("researched_at")
    s.flush()

    # sources + evidence (one evidence row per seed source fragment)
    ref_to_evidence: dict[str, int] = {}
    for src in d.get("sources", []):
        cu = canonical_url(src["url"])
        row = s.scalar(select(Source).where(Source.firm_id == firm.id, Source.canonical_url == cu))
        doc_type = src.get("doc_type", "MARKETING")
        if row is None:
            row = Source(firm_id=firm.id, url=src["url"], canonical_url=cu)
            s.add(row)
        row.seed_ref = src.get("id")
        row.title = src.get("title", "")
        row.doc_type = doc_type
        row.priority = DOC_PRIORITY.get(doc_type, 60)
        row.is_primary = doc_type in PRIMARY_DOC_TYPES
        row.monitored = row.is_primary
        s.flush()
        frag = src.get("fragment", "")
        fh = sha(frag)
        ev = s.scalar(select(Evidence).where(Evidence.source_id == row.id, Evidence.fragment_hash == fh))
        if ev is None:
            ev = Evidence(firm_id=firm.id, source_id=row.id, fragment=frag, fragment_hash=fh,
                          retrieved_at=_dt(src.get("retrieved_at")), parser_version=PARSER_VERSION,
                          confidence=SEED_CONFIDENCE, verification_status="UNVERIFIED", method="seed_research")
            s.add(ev)
            s.flush()
        ref_to_evidence[src.get("id", "")] = ev.id

    conflicts = d.get("conflicts", [])
    conflict_kinds = {c["field"].split(".")[0] for c in conflicts}
    for c in conflicts:
        exists = s.scalar(select(Conflict).where(Conflict.firm_id == firm.id, Conflict.field == c["field"],
                                                 Conflict.status == "OPEN"))
        if exists is None:
            s.add(Conflict(firm_id=firm.id, field=c["field"], rule_kind=c["field"].split(".")[0],
                           values=c.get("values", []), source_refs=c.get("sources", []), notes=c.get("notes", "")))

    def ev_ids(refs):
        return [ref_to_evidence[r] for r in refs or [] if r in ref_to_evidence]

    # firm-level automation rules (apply to all programs)
    auto = d.get("automation") or {}
    for key, (kind, field) in _AUTOMATION_MAP.items():
        if key in auto:
            params = {field: auto[key] if auto[key] in ("ALLOWED", "PROHIBITED", "CONDITIONAL") else "UNKNOWN"}
            if kind in ("ea_policy", "api_trading", "copy_trading") and auto.get("conditions"):
                params["conditions"] = auto["conditions"][:500]
            _upsert_rule(s, firm, None, "all", kind, params, auto.get("conditions", ""), ev_ids(auto.get("evidence")),
                         kind in conflict_kinds, audit)

    for p in d.get("programs", []):
        prog = s.scalar(select(Program).where(Program.firm_id == firm.id, Program.slug == p["slug"]))
        if prog is None:
            prog = Program(firm_id=firm.id, slug=p["slug"], name=p.get("name", p["slug"]))
            s.add(prog)
        prog.name = p.get("name", p["slug"])
        prog.platforms = p.get("platforms", [])
        prog.refund = p.get("refund") or ""
        prog.phases = [ph["name"] for ph in p.get("phases", [])]
        s.flush()
        existing_sizes = {(c.account_size, c.price) for c in prog.challenges}
        for sz in p.get("account_sizes", []):
            if (float(sz["size"]), sz.get("price_usd")) not in existing_sizes:
                s.add(Challenge(program_id=prog.id, account_size=float(sz["size"]), price=sz.get("price_usd"),
                                currency=sz.get("currency", "USD")))
        for ph in p.get("phases", []):
            for r in ph.get("rules", []):
                _upsert_rule(s, firm, prog, ph["name"], r["type"], r.get("params", {}), r.get("text", ""),
                             ev_ids(r.get("evidence")), r["type"] in conflict_kinds, audit)
    s.flush()
    firm.status, firm.status_reason = classify_firm(s, firm, proposed=d.get("proposed_status"))
    if audit:
        audit.record("registry.seed_loaded", None, {"firm": firm.slug, "origin": origin, "status": firm.status})
    return firm.slug


def _upsert_rule(s: Session, firm: Firm, prog: Program | None, phase: str, kind: str, raw_params: dict,
                 text: str, evidence_ids: list[int], in_conflict: bool, audit: AuditSink | None) -> Rule:
    base = InterpretationStatus.CONFLICT if in_conflict else InterpretationStatus.UNVERIFIED
    n = normalize_rule(kind, raw_params, base)
    key = f"{firm.slug}:{prog.slug if prog else '*'}:{phase}:{n.kind if n.kind != 'custom' else kind}"
    cur = s.scalar(select(Rule).where(Rule.rule_key == key, Rule.is_current.is_(True)))
    if cur is not None and cur.params == n.params and cur.raw_text == text and cur.raw_params == n.raw_params:
        return cur  # unchanged
    new = Rule(rule_key=key, firm_id=firm.id, program_id=prog.id if prog else None, phase=phase, kind=n.kind,
               params=n.params, raw_params=n.raw_params, raw_text=text, criticality=criticality_of(n.kind).value,
               interpretation_status=n.status.value, confidence=SEED_CONFIDENCE, interpretation_notes=n.notes,
               evidence_ids=evidence_ids, version=(cur.version + 1) if cur else 1, parser_version=PARSER_VERSION)
    if cur is not None:
        cur.is_current = False
    s.add(new)
    s.flush()
    s.add(RuleChange(rule_key=key, firm_id=firm.id,
                     old_value=None if cur is None else {"params": cur.params, "status": cur.interpretation_status,
                                                         "version": cur.version},
                     new_value={"params": n.params, "status": n.status.value, "version": new.version},
                     diff_fragment=text[:1000], parser_version=PARSER_VERSION, confidence=SEED_CONFIDENCE,
                     criticality=new.criticality, approval_state="AUTO" if cur is None else "PENDING"))
    if audit:
        audit.record("registry.rule_version", None, {"rule_key": key, "version": new.version,
                                                     "status": n.status.value, "notes": n.notes})
    return new


# --------------------------------------------------------------------------- classification


def classify_firm(s: Session, firm: Firm, proposed: str | None = None) -> tuple[str, str]:
    """Deterministic firm status. Never higher than the evidence supports."""
    j = firm.jurisdiction or {}
    if (j.get("ukraine_citizens") == "PROHIBITED" and j.get("ukraine_residents") == "PROHIBITED") or proposed == "EXCLUDED":
        return "EXCLUDED", "Ukraine prohibited by firm (citizenship and/or residence) or excluded by research"
    sources = list(s.scalars(select(Source).where(Source.firm_id == firm.id)))
    by_type: dict[str, list[Source]] = {}
    for src in sources:
        by_type.setdefault(src.doc_type, []).append(src)
    verified_types = set()
    for t, srcs in by_type.items():
        for src in srcs:
            if s.scalar(select(Evidence).where(Evidence.source_id == src.id,
                                               Evidence.verification_status == "VERIFIED")):
                verified_types.add(t)
    missing = [t for t in REQUIRED_DOC_TYPES if t not in verified_types]
    rules = list(s.scalars(select(Rule).where(Rule.firm_id == firm.id, Rule.is_current.is_(True))))
    open_conflicts = s.scalars(select(Conflict).where(Conflict.firm_id == firm.id, Conflict.status == "OPEN")).all()
    crit_unconfirmed = [r.rule_key for r in rules if r.criticality == "CRITICAL"
                        and r.interpretation_status != "CONFIRMED"]
    has_programs = bool(firm.programs)
    if not missing and has_programs and not crit_unconfirmed and not open_conflicts:
        return "VERIFIED", "all required primary documents verified; critical rules confirmed; no open conflicts"
    reasons = []
    if missing:
        reasons.append("unverified document types: " + ", ".join(missing))
    if crit_unconfirmed:
        reasons.append(f"{len(crit_unconfirmed)} critical rules not confirmed")
    if open_conflicts:
        reasons.append(f"{len(open_conflicts)} open source conflicts")
    if not has_programs:
        reasons.append("no program rules captured")
    if firm.official_url and has_programs and j.get("ukraine_citizens") not in (None, "UNKNOWN"):
        return "WATCHLIST", "; ".join(reasons)
    return "INSUFFICIENT_DATA", "; ".join(reasons)


# --------------------------------------------------------------------------- verification workflow


def verify_rule(s: Session, rule_id: int, actor: str, params: dict[str, Any] | None = None,
                note: str = "", audit: AuditSink | None = None) -> Rule:
    """Owner (human) confirms a rule interpretation against the primary source.

    Creates a new CONFIRMED version. ``params`` may correct the normalized params; they are
    validated against the schema. LLM output can never call this path.
    """
    cur = s.get(Rule, rule_id)
    if cur is None or not cur.is_current:
        raise ValueError("rule not found or not current")
    if cur.kind == "custom":
        raise ValueError("custom rules cannot be confirmed; model the rule kind first")
    new_params = parse_params(cur.kind, params if params is not None else cur.params).model_dump(mode="json")
    conflict = s.scalar(select(Conflict).where(Conflict.firm_id == cur.firm_id, Conflict.rule_kind == cur.kind,
                                               Conflict.status == "OPEN"))
    if conflict is not None:
        raise ValueError(f"open conflict #{conflict.id} for {cur.kind}; resolve it first")
    now = datetime.now(timezone.utc)
    new = Rule(rule_key=cur.rule_key, firm_id=cur.firm_id, program_id=cur.program_id, phase=cur.phase,
               kind=cur.kind, params=new_params, raw_params=cur.raw_params, raw_text=cur.raw_text,
               criticality=cur.criticality, interpretation_status="CONFIRMED", confidence=1.0,
               interpretation_notes=(note or "confirmed by owner"), evidence_ids=cur.evidence_ids,
               version=cur.version + 1, verified_at=now, verified_by=actor, parser_version=cur.parser_version)
    cur.is_current = False
    s.add(new)
    s.add(RuleChange(rule_key=cur.rule_key, firm_id=cur.firm_id,
                     old_value={"params": cur.params, "status": cur.interpretation_status, "version": cur.version},
                     new_value={"params": new_params, "status": "CONFIRMED", "version": new.version},
                     confidence=1.0, criticality=cur.criticality, approval_state="APPROVED", decided_by=actor,
                     decided_at=now, diff_fragment=note))
    for ev_id in cur.evidence_ids or []:
        ev = s.get(Evidence, ev_id)
        if ev is not None:
            ev.verification_status = "VERIFIED"
            ev.last_verified_at = now
            ev.confidence = 1.0
    s.flush()
    firm = s.get(Firm, cur.firm_id)
    firm.status, firm.status_reason = classify_firm(s, firm)
    if audit:
        audit.record("registry.rule_verified", None, {"rule_key": cur.rule_key, "version": new.version,
                                                      "actor": actor, "params": new_params})
    return new


def resolve_conflict(s: Session, conflict_id: int, resolution: str, actor: str,
                     audit: AuditSink | None = None) -> Conflict:
    c = s.get(Conflict, conflict_id)
    if c is None or c.status != "OPEN":
        raise ValueError("conflict not open")
    c.status, c.resolution = "RESOLVED", f"{resolution} (by {actor})"
    if audit:
        audit.record("registry.conflict_resolved", None, {"conflict_id": conflict_id, "resolution": resolution,
                                                          "actor": actor})
    # rules of that kind remain non-CONFIRMED until explicitly verified
    for r in s.scalars(select(Rule).where(Rule.firm_id == c.firm_id, Rule.kind == c.rule_kind,
                                          Rule.is_current.is_(True))):
        if r.interpretation_status == "CONFLICT":
            r.interpretation_status = "UNCERTAIN"
    return c


def mark_rules_uncertain_for_source(s: Session, source: Source, reason: str) -> list[str]:
    """After a detected change in a source, every current rule citing it becomes UNCERTAIN."""
    ev_ids = set(s.scalars(select(Evidence.id).where(Evidence.source_id == source.id)))
    changed = []
    for r in s.scalars(select(Rule).where(Rule.firm_id == source.firm_id, Rule.is_current.is_(True))):
        if ev_ids & set(r.evidence_ids or []) and r.interpretation_status == "CONFIRMED":
            r.interpretation_status = "UNCERTAIN"
            r.interpretation_notes = f"{reason} (was CONFIRMED v{r.version})"
            changed.append(r.rule_key)
    return changed


# --------------------------------------------------------------------------- RuleSet assembly


def ruleset_for(s: Session, firm_slug: str, program_slug: str, phase: str, initial_balance: Decimal | float,
                now: datetime | None = None) -> RuleSet:
    now = now or datetime.now(timezone.utc)
    firm = s.scalar(select(Firm).where(Firm.slug == firm_slug))
    if firm is None:
        raise ValueError(f"unknown firm {firm_slug}")
    prog = s.scalar(select(Program).where(Program.firm_id == firm.id, Program.slug == program_slug))
    q = select(Rule).where(Rule.firm_id == firm.id, Rule.is_current.is_(True))
    rows = [r for r in s.scalars(q)
            if (r.program_id is None and r.phase == "all")
            or (prog is not None and r.program_id == prog.id and r.phase in (phase, "all"))]
    # phase-specific rule wins over program/firm-wide rule of same kind
    chosen: dict[str, Rule] = {}
    for r in sorted(rows, key=lambda r: (r.phase != "all", r.program_id is not None)):
        chosen[r.kind] = r
    records = []
    for r in chosen.values():
        if r.kind == "custom":
            continue
        try:
            params = parse_params(r.kind, r.params)
        except ValueError:
            continue  # incomplete normalized params: absence makes the ruleset incomplete -> fail closed
        records.append(RuleRecord(
            rule_id=f"{r.rule_key}@v{r.version}", kind=r.kind, params=params, raw_text=r.raw_text,
            status=InterpretationStatus(r.interpretation_status), confidence=Decimal(str(r.confidence)),
            verified_at=effective_verified_at(s, r), evidence_ids=tuple(str(e) for e in r.evidence_ids or []),
            version=r.version))
    pending = s.scalar(select(RuleChange).where(RuleChange.firm_id == firm.id, RuleChange.approval_state == "PENDING",
                                                RuleChange.criticality.in_(["CRITICAL", "HIGH"])))
    rs = RuleSet(ruleset_id="", firm_slug=firm_slug, program_slug=program_slug, phase=phase,
                 initial_balance=Decimal(str(initial_balance)), rules=tuple(records),
                 pending_critical_change=pending is not None)
    return RuleSet(**{**rs.__dict__, "ruleset_id": rs.fingerprint()})


def effective_verified_at(s: Session, r: Rule) -> datetime | None:
    """A confirmed rule is as fresh as the oldest successful re-check of its sources (if unchanged)."""
    if r.interpretation_status != "CONFIRMED" or r.verified_at is None:
        return None
    base = aware(r.verified_at)
    times = [base]
    for ev_id in r.evidence_ids or []:
        ev = s.get(Evidence, ev_id)
        if ev is None or ev.source_id is None:
            continue
        src = s.get(Source, ev.source_id)
        if src is None or src.last_ok_at is None:
            continue
        if src.last_changed_at is not None and aware(src.last_changed_at) > base:
            return None  # source changed after confirmation
        times.append(max(aware(src.last_ok_at), base))
    # freshness = latest confirmation or successful unchanged re-check, bounded by the stalest source
    return min(times[1:]) if len(times) > 1 else base


def rules_freshness_report(s: Session, max_age_h: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out = []
    for r in s.scalars(select(Rule).where(Rule.is_current.is_(True), Rule.criticality == Criticality.CRITICAL.value)):
        v = effective_verified_at(s, r)
        out.append({"rule_key": r.rule_key, "status": r.interpretation_status,
                    "verified_at": v.isoformat() if v else None,
                    "fresh": v is not None and now - v <= timedelta(hours=max_age_h)})
    return out


def raise_alert(s: Session, severity: str, kind: str, message: str, firm_id: int | None = None,
                account_id: str | None = None, payload: dict | None = None) -> Alert:
    a = Alert(severity=severity, kind=kind, message=message, firm_id=firm_id, account_id=account_id,
              payload=payload or {})
    s.add(a)
    s.flush()
    return a
