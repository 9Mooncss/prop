"""Rule-source monitoring pipeline.

Cheapest-first:
  conditional GET (ETag / Last-Modified) -> 304: done, no parsing, no LLM
  -> deterministic extraction + canonicalization -> content hash equal: done, no LLM
  -> structural block diff -> keyword classification
  -> only for HIGH/CRITICAL hunks: optional LLM on the changed fragment (+1 block of context)
Every detected critical change marks the affected rules UNCERTAIN (fail closed for new risk) and
creates a PENDING RuleChange + alert. Nothing is auto-confirmed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from propguard.db.models import DocumentSnapshot, Evidence, Firm, LLMCall, Rule, RuleChange, Source
from propguard.llm.client import ChangeAnalyzer
from propguard.monitor.diff import classify, diff_blocks
from propguard.monitor.extract import EXTRACTOR_VERSION, content_hash, extract_blocks, fragment_match_score
from propguard.monitor.fetch import Fetcher, FetchResult
from propguard.notify.base import Notifier
from propguard.registry.service import mark_rules_uncertain_for_source, raise_alert

log = logging.getLogger(__name__)
FAILURE_ALERT_THRESHOLD = 3


@dataclass
class CheckOutcome:
    source_id: int
    url: str
    result: str  # NOT_MODIFIED | UNCHANGED | BASELINE | CHANGED | FAILED | BLOCKED
    severity: str = "INFO"
    llm_calls: int = 0
    rules_marked_uncertain: list[str] = field(default_factory=list)
    evidence_matched: int = 0
    message: str = ""


class SqlLLMLedger:
    def __init__(self, s: Session) -> None:
        self.s = s

    def cached(self, input_hash):
        r = self.s.scalar(select(LLMCall).where(LLMCall.input_hash == input_hash).order_by(LLMCall.id.desc()))
        return r.result if r else None

    def spent_this_month(self):
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return float(self.s.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                   .where(LLMCall.ts >= start)) or 0.0)

    def record(self, tier, model, purpose, input_hash, in_tok, out_tok, cost, result):
        self.s.add(LLMCall(tier=tier, model=model, purpose=purpose, input_hash=input_hash, input_tokens=in_tok,
                           output_tokens=out_tok, cost_usd=cost, result=result))


def check_source(s: Session, src: Source, fetcher: Fetcher, analyzer: ChangeAnalyzer | None = None,
                 notifier: Notifier | None = None, now: datetime | None = None) -> CheckOutcome:
    now = now or datetime.now(timezone.utc)
    firm = s.get(Firm, src.firm_id)
    src.last_checked_at = now
    res: FetchResult = fetcher.fetch(src.url, src.etag, src.last_modified)
    if res.status == "NOT_MODIFIED":
        _ok(src, now)
        return CheckOutcome(src.id, src.url, "NOT_MODIFIED")
    if res.status != "OK":
        src.consecutive_failures += 1
        src.last_error = res.error
        out = CheckOutcome(src.id, src.url, "BLOCKED" if res.status == "BLOCKED" else "FAILED", "WARNING",
                           message=res.error or res.status)
        if src.consecutive_failures == FAILURE_ALERT_THRESHOLD:
            a = raise_alert(s, "HIGH", "source_unavailable",
                            f"{firm.name}: source unavailable {FAILURE_ALERT_THRESHOLD}x ({res.error}); rules citing "
                            f"it will go stale and block new risk", firm.id, payload={"url": src.url})
            _notify(notifier, a.severity, "Rule source unavailable", a.message)
        return out

    title, blocks = extract_blocks(res.body)
    h = content_hash(blocks)
    src.etag, src.last_modified = res.etag, res.last_modified
    prev = s.scalar(select(DocumentSnapshot).where(DocumentSnapshot.source_id == src.id)
                    .order_by(DocumentSnapshot.version.desc()))
    if prev is not None and prev.content_hash == h:
        _ok(src, now)
        return CheckOutcome(src.id, src.url, "UNCHANGED")

    snap = DocumentSnapshot(source_id=src.id, fetched_at=now, http_status=res.http_status, content_hash=h,
                            canonical_text="\n".join(blocks), blocks=blocks, title=title,
                            parser_version=EXTRACTOR_VERSION, version=(prev.version + 1) if prev else 1)
    s.add(snap)
    s.flush()
    src.last_hash = h
    if not src.title and title:
        src.title = title
    if prev is None:
        matched = _match_evidence(s, src, snap, blocks, now)
        _ok(src, now)
        return CheckOutcome(src.id, src.url, "BASELINE", evidence_matched=matched,
                            message=f"baseline captured; {matched} seed fragments matched raw text")

    # --- real change
    hunks = diff_blocks(prev.blocks or [], blocks)
    assessment = classify(hunks)
    src.last_changed_at = now
    _ok(src, now)
    out = CheckOutcome(src.id, src.url, "CHANGED", assessment.severity)
    diff_text = "\n---\n".join(hk.text() for hk in hunks)[:8000]
    if assessment.severity in ("HIGH", "CRITICAL"):
        out.rules_marked_uncertain = mark_rules_uncertain_for_source(
            s, src, f"source changed {now.isoformat()} ({assessment.severity})")
        # topics not tied to evidence (e.g. jurisdiction) -> mark firm-wide critical rules of those kinds
        for r in s.scalars(select(Rule).where(Rule.firm_id == src.firm_id, Rule.is_current.is_(True),
                                              Rule.kind.in_(assessment.topics))):
            if r.interpretation_status == "CONFIRMED":
                r.interpretation_status = "UNCERTAIN"
                r.interpretation_notes = f"topic {r.kind} changed in {src.url}"
                out.rules_marked_uncertain.append(r.rule_key)
    proposals = []
    if assessment.needs_semantic_review and analyzer is not None and analyzer.enabled:
        for hk in hunks[:5]:  # bounded: only changed fragments, never full page
            a = analyzer.analyze(hk.text(), firm.slug, sorted(assessment.topics), src.doc_type)
            out.llm_calls += 0 if a.cached else 1
            proposals.append({"model": a.model, "tier": a.tier, "cached": a.cached, "valid": a.valid_params,
                              **a.proposal.model_dump()})
    s.add(RuleChange(rule_key=f"{firm.slug}:source:{src.id}", firm_id=firm.id,
                     old_value={"doc_hash": prev.content_hash, "version": prev.version},
                     new_value={"doc_hash": h, "version": snap.version, "topics": sorted(assessment.topics),
                                "llm_proposals": proposals},
                     source_id=src.id, old_doc_hash=prev.content_hash, new_doc_hash=h, diff_fragment=diff_text,
                     parser_version=EXTRACTOR_VERSION,
                     model_version=",".join(sorted({p["model"] for p in proposals if p.get("model")})) or None,
                     confidence=max([p.get("confidence", 0) for p in proposals], default=0.0),
                     criticality="CRITICAL" if assessment.severity == "CRITICAL" else
                     "HIGH" if assessment.severity == "HIGH" else "NORMAL",
                     approval_state="PENDING" if assessment.severity in ("HIGH", "CRITICAL") else "AUTO"))
    a = raise_alert(s, assessment.severity, "rule_source_changed",
                    f"{firm.name}: {src.doc_type} changed ({', '.join(sorted(assessment.topics)) or 'no rule topics'})",
                    firm.id, payload={"url": src.url, "diff": diff_text[:3000],
                                      "rules_marked_uncertain": out.rules_marked_uncertain})
    _notify(notifier, a.severity, "Rule source changed", a.message + "\n" + diff_text[:1500])
    return out


def _ok(src: Source, now: datetime) -> None:
    src.last_ok_at = now
    src.consecutive_failures = 0
    src.last_error = None


def _match_evidence(s: Session, src: Source, snap: DocumentSnapshot, blocks: list[str], now: datetime) -> int:
    """Deterministic provenance upgrade: a seed fragment found in the raw page text becomes
    SOURCE_MATCHED (confidence 0.8). Interpretation still needs owner confirmation."""
    n = 0
    for ev in s.scalars(select(Evidence).where(Evidence.source_id == src.id)):
        score = fragment_match_score(ev.fragment, blocks)
        if score >= 0.85:
            ev.snapshot_id = snap.id
            ev.last_verified_at = now
            if ev.verification_status == "UNVERIFIED":
                ev.verification_status = "SOURCE_MATCHED"
                ev.confidence = max(ev.confidence, 0.8)
            n += 1
    return n


def _notify(notifier: Notifier | None, severity: str, title: str, body: str) -> None:
    if notifier is not None:
        try:
            notifier.send(severity, title, body)
        except Exception:  # noqa: BLE001
            log.warning("notifier failed")


def run_monitor(s: Session, fetcher: Fetcher, analyzer: ChangeAnalyzer | None = None,
                notifier: Notifier | None = None, due_after_s: int = 3600) -> list[CheckOutcome]:
    now = datetime.now(timezone.utc)
    outs = []
    for src in s.scalars(select(Source).where(Source.monitored.is_(True)).order_by(Source.priority)):
        from propguard.db.session import aware
        last = aware(src.last_checked_at)
        if last is not None and (now - last).total_seconds() < due_after_s:
            continue
        outs.append(check_source(s, src, fetcher, analyzer, notifier, now))
        s.commit()
    return outs
