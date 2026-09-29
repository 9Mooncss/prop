import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from propguard.db.models import Conflict, Evidence, Firm, Rule, RuleChange, Source
from propguard.db.session import session_scope
from propguard.db.stores import SessionAudit, SqlAudit
from propguard.registry import service as reg
from propguard.registry.eligibility import Overall, Profile, evaluate, firm_to_dict
from propguard.registry.normalize import normalize_rule
from propguard.rules.types import InterpretationStatus

pytestmark = pytest.mark.integration
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "firm_demo.json"
SEED_DIR = Path(__file__).resolve().parents[2] / "seed" / "firms"


def load(sf, d=None):
    with session_scope(sf) as s:
        reg.load_seed(s, d or json.loads(FIX.read_text()), SessionAudit(s))


def test_normalize_never_guesses():
    n = normalize_rule("daily_loss_limit", {"pct": 5, "basis": "something_odd", "includes_floating": None,
                                            "reset_time": "00:00", "reset_tz": "Europe/Prague"},
                       InterpretationStatus.UNVERIFIED)
    assert n.status == InterpretationStatus.UNCERTAIN
    assert "basis" in n.notes and "includes_floating" in n.notes
    assert n.raw_params["basis"] == "something_odd"
    ok = normalize_rule("profit_target", {"pct": 8}, InterpretationStatus.UNVERIFIED)
    assert ok.status == InterpretationStatus.UNVERIFIED and ok.params["pct"] == "8"
    bad = normalize_rule("max_loss", {"pct": 10, "mode": "weird"}, InterpretationStatus.UNVERIFIED)
    assert bad.status == InterpretationStatus.UNCERTAIN and "mode" in bad.notes
    unk = normalize_rule("totally_new", {"a": 1}, InterpretationStatus.UNVERIFIED)
    assert unk.kind == "custom" and unk.status == InterpretationStatus.UNCERTAIN


def test_seed_load_idempotent_with_provenance(sf):
    load(sf)
    load(sf)
    with session_scope(sf) as s:
        f = s.scalar(select(Firm).where(Firm.slug == "demo-firm"))
        assert f.status == "WATCHLIST"  # nothing verified yet -> never VERIFIED from seed
        assert len(s.scalars(select(Source).where(Source.firm_id == f.id)).all()) == 6
        community = s.scalar(select(Source).where(Source.doc_type == "COMMUNITY"))
        assert not community.is_primary and not community.monitored
        rules = s.scalars(select(Rule).where(Rule.firm_id == f.id, Rule.is_current.is_(True))).all()
        assert all(r.version == 1 for r in rules)  # idempotent
        dl = next(r for r in rules if r.kind == "daily_loss_limit")
        assert dl.interpretation_status == "UNVERIFIED" and dl.evidence_ids
        ev = s.get(Evidence, dl.evidence_ids[0])
        assert "5%" in ev.fragment and ev.confidence == pytest.approx(0.6) and ev.fragment_hash
        assert any(r.kind == "custom" for r in rules)
        assert {r.kind for r in rules} >= {"ea_policy", "api_trading", "copy_trading"}


def test_ruleset_unverified_blocks_until_owner_verifies(sf):
    load(sf)
    with session_scope(sf) as s:
        rs = reg.ruleset_for(s, "demo-firm", "two-step", "phase1", 100000)
        assert rs.non_confirmed_critical()
        assert rs.oldest_critical_verification() is None
        for r in s.scalars(select(Rule).where(Rule.is_current.is_(True), Rule.kind != "custom")).all():
            reg.verify_rule(s, r.id, "owner", note="checked against page")
    with session_scope(sf) as s:
        rs = reg.ruleset_for(s, "demo-firm", "two-step", "phase1", 100000)
        assert not rs.non_confirmed_critical()
        assert rs.oldest_critical_verification() is not None
        changes = s.scalars(select(RuleChange).where(RuleChange.approval_state == "APPROVED")).all()
        assert changes and all(c.decided_by == "owner" for c in changes)
        f = s.scalar(select(Firm).where(Firm.slug == "demo-firm"))
        # PLATFORM_RULES and TERMS docs never verified -> still not VERIFIED
        assert f.status == "WATCHLIST" and "TERMS" in f.status_reason
    assert SqlAudit(sf).verify_chain() == (True, None)


def test_conflict_blocks_confirmation_and_is_kept(sf):
    d = json.loads(FIX.read_text())
    d["conflicts"] = [{"field": "daily_loss_limit.pct", "values": ["5", "4"], "sources": ["s4", "s5"]}]
    load(sf, d)
    with session_scope(sf) as s:
        dl = s.scalar(select(Rule).where(Rule.kind == "daily_loss_limit", Rule.is_current.is_(True)))
        assert dl.interpretation_status == "CONFLICT"
        with pytest.raises(ValueError, match="conflict"):
            reg.verify_rule(s, dl.id, "owner")
        c = s.scalar(select(Conflict))
        assert c.values == ["5", "4"] and c.status == "OPEN"
        reg.resolve_conflict(s, c.id, "Terms (s5) says 5%, FAQ outdated", "owner")
        reg.verify_rule(s, dl.id, "owner")


def test_seed_change_creates_new_version_and_pending_change(sf):
    load(sf)
    d = json.loads(FIX.read_text())
    d["programs"][0]["phases"][0]["rules"][0]["params"]["pct"] = 10
    load(sf, d)
    with session_scope(sf) as s:
        rows = s.scalars(select(Rule).where(Rule.kind == "profit_target")).all()
        assert sorted(r.version for r in rows) == [1, 2]
        assert s.scalar(select(RuleChange).where(RuleChange.approval_state == "PENDING")) is not None


def test_eligibility_separates_citizenship_residence_tax_kyc_ip(sf):
    load(sf)
    with session_scope(sf) as s:
        f = firm_to_dict(s.scalar(select(Firm).where(Firm.slug == "demo-firm")))
    r = evaluate(Profile(), f)
    assert r.overall == Overall.UNKNOWN  # residence and IP not set -> never assumed
    r = evaluate(Profile(residence_country="PL", ip_location_country="PL",
                         kyc_documents=({"type": "passport", "country": "UA"},)), f)
    assert r.overall == Overall.ELIGIBLE, r.to_dict()
    r = evaluate(Profile(residence_country="UA", ip_location_country="UA",
                         kyc_documents=({"type": "passport", "country": "UA"},)), f)
    assert r.overall == Overall.CONDITIONAL  # region restriction -> must confirm region
    r = evaluate(Profile(residence_country="UA", residence_region="Donetsk", ip_location_country="UA",
                         kyc_documents=({"type": "passport", "country": "UA"},)), f)
    assert r.overall == Overall.INELIGIBLE
    r = evaluate(Profile(residence_country="IR", ip_location_country="PL"), f)
    assert r.overall == Overall.INELIGIBLE
    r = evaluate(Profile(residence_country="PL", ip_location_country="IR"), f)
    assert r.overall == Overall.INELIGIBLE  # actual IP location restricted: reported, never circumvented
    f2 = {**f, "payout_classification": "CRYPTO_VIA_PROVIDER"}
    r = evaluate(Profile(residence_country="PL", ip_location_country="PL",
                         kyc_documents=({"type": "passport", "country": "UA"},)), f2)
    assert r.overall == Overall.INELIGIBLE and any(c.name == "crypto_payout" and c.verdict.value == "FAIL"
                                                   for c in r.checks)
    r = evaluate(Profile(residence_country="PL", ip_location_country="PL", payout_requirement="ANY_CRYPTO",
                         kyc_documents=({"type": "passport", "country": "UA"},)), f2)
    assert r.overall == Overall.ELIGIBLE


def test_research_seed_files_all_load(sf):
    with session_scope(sf) as s:
        slugs = reg.load_seed_dir(s, SEED_DIR)
    assert len(slugs) >= 5
    with session_scope(sf) as s:
        statuses = {f.slug: f.status for f in s.scalars(select(Firm))}
    assert "VERIFIED" not in statuses.values()  # seeds alone can never produce VERIFIED
    assert statuses.get("e8-markets") == "EXCLUDED"


def test_effective_verification_goes_stale_and_source_change_invalidates(sf):
    load(sf)
    with session_scope(sf) as s:
        dl = s.scalar(select(Rule).where(Rule.kind == "daily_loss_limit", Rule.is_current.is_(True)))
        new = reg.verify_rule(s, dl.id, "owner")
        src = s.get(Source, s.get(Evidence, new.evidence_ids[0]).source_id)
        src.last_ok_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        assert reg.effective_verified_at(s, new) is not None
        src.last_changed_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        assert reg.effective_verified_at(s, new) is None
        marked = reg.mark_rules_uncertain_for_source(s, src, "changed")
        assert new.rule_key in marked and new.interpretation_status == "UNCERTAIN"
