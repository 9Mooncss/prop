"""FastAPI application: JSON API under /api, server-rendered dashboard pages, /health, /metrics.

Mutating endpoints require the owner token (``PROPGUARD_API_TOKEN``) when configured; without a token
they are only accepted from loopback clients. There is deliberately NO endpoint that:
enables LIVE trading, pays for a challenge, signs blockchain transactions, or lets an LLM change rules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from propguard.app_context import AppContext, current_profile, load_account_status, update_profile
from propguard.db.models import (
    Alert,
    AuditLog,
    Conflict,
    Evidence,
    Firm,
    KillSwitchRow,
    OrderRow,
    Program,
    Recommendation,
    Rule,
    RuleChange,
    Source,
    TradeHistory,
    TradingAccount,
    WalletAddress,
)
from propguard.db.session import session_scope
from propguard.db.stores import SessionAudit
from propguard.recommender.replay import parse_trades_csv
from propguard.recommender.service import recommend, trades_to_json
from propguard.registry import service as reg
from propguard.registry.eligibility import evaluate, firm_to_dict
from propguard.registry.service import rules_freshness_report
from propguard.risk.policy import KillSwitchKind

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def mask(addr: str) -> str:
    return addr if len(addr) <= 12 else f"{addr[:6]}…{addr[-4:]}"


TEMPLATES.env.filters["mask"] = mask
TEMPLATES.env.filters["pct"] = lambda v: f"{float(v) * 100:.0f}%" if v not in (None, "") else "–"


def create_app(ctx: AppContext | None = None) -> FastAPI:
    ctx = ctx or AppContext.create()
    app = FastAPI(title="PropGuard", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ctx = ctx

    def db():
        with session_scope(ctx.sf) as s:
            yield s

    def owner(request: Request) -> str:
        tok = ctx.settings.api_token
        if tok is None:
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
                raise HTTPException(403, "set PROPGUARD_API_TOKEN to allow non-loopback mutations")
            return "owner@loopback"
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given:
            given = request.cookies.get("pg_token", "")
        if not hmac.compare_digest(given.encode(), tok.get_secret_value().encode()):
            raise HTTPException(401, "owner token required")
        return "owner"

    # ------------------------------------------------------------------ health / metrics
    @app.get("/health")
    def health(s: Session = Depends(db)):
        s.execute(text("SELECT 1"))
        from propguard.db.migrate import current
        return {"status": "ok", "db": "ok", "schema": current(ctx.settings.database_url),
                "execution_mode": ctx.settings.execution_mode, "live_allowed_env": ctx.settings.live_allowed_env}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(s: Session = Depends(db)):
        def c(q):
            return s.scalar(q) or 0
        lines = [
            f"propguard_alerts_open {c(select(func.count()).select_from(Alert).where(Alert.acknowledged.is_(False)))}",
            f"propguard_kill_switches_active {c(select(func.count()).select_from(KillSwitchRow).where(KillSwitchRow.cleared_at.is_(None)))}",
            f"propguard_orders_rejected_by_risk_total {c(select(func.count()).select_from(OrderRow).where(OrderRow.status == 'REJECTED_BY_RISK'))}",
            f"propguard_sources_failing {c(select(func.count()).select_from(Source).where(Source.consecutive_failures > 0))}",
            f"propguard_rule_changes_pending {c(select(func.count()).select_from(RuleChange).where(RuleChange.approval_state == 'PENDING'))}",
            f"propguard_conflicts_open {c(select(func.count()).select_from(Conflict).where(Conflict.status == 'OPEN'))}",
        ]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ JSON API
    @app.get("/api/firms")
    def api_firms(s: Session = Depends(db)):
        prof = _profile_obj(s)
        return [_firm_summary(s, f, prof) for f in s.scalars(select(Firm).order_by(Firm.slug))]

    @app.get("/api/firms/{slug}")
    def api_firm(slug: str, s: Session = Depends(db)):
        f = _firm_or_404(s, slug)
        return _firm_detail(s, f, _profile_obj(s))

    @app.post("/api/rules/{rule_id}/verify")
    def api_verify_rule(rule_id: int, body: dict[str, Any] | None = None, who: str = Depends(owner),
                        s: Session = Depends(db)):
        body = body or {}
        try:
            r = reg.verify_rule(s, rule_id, who, body.get("params"), body.get("note", ""), SessionAudit(s))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"rule_id": r.id, "version": r.version, "status": r.interpretation_status}

    @app.post("/api/conflicts/{cid}/resolve")
    def api_resolve(cid: int, body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        try:
            c = reg.resolve_conflict(s, cid, body["resolution"], who, SessionAudit(s))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": c.id, "status": c.status}

    @app.get("/api/rule-changes")
    def api_changes(s: Session = Depends(db), state: str | None = None):
        q = select(RuleChange).order_by(RuleChange.id.desc()).limit(200)
        if state:
            q = q.where(RuleChange.approval_state == state)
        return [_row(r) for r in s.scalars(q)]

    @app.post("/api/rule-changes/{cid}/decide")
    def api_decide(cid: int, body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        """Acknowledge a detected change. APPROVE only closes the review item; affected rules stay
        UNCERTAIN until each is re-verified via /api/rules/{id}/verify."""
        rc = s.get(RuleChange, cid)
        if rc is None or rc.approval_state != "PENDING":
            raise HTTPException(400, "not pending")
        decision = body.get("decision")
        if decision not in ("APPROVED", "REJECTED"):
            raise HTTPException(400, "decision must be APPROVED or REJECTED")
        rc.approval_state, rc.decided_by, rc.decided_at = decision, who, datetime.now(timezone.utc)
        SessionAudit(s).record("registry.change_decided", None, {"change_id": cid, "decision": decision,
                                                                 "actor": who, "note": body.get("note", "")})
        return {"id": cid, "approval_state": decision}

    @app.get("/api/alerts")
    def api_alerts(s: Session = Depends(db), include_ack: bool = False):
        q = select(Alert).order_by(Alert.id.desc()).limit(200)
        if not include_ack:
            q = q.where(Alert.acknowledged.is_(False))
        return [_row(a) for a in s.scalars(q)]

    @app.post("/api/alerts/{aid}/ack")
    def api_ack(aid: int, who: str = Depends(owner), s: Session = Depends(db)):
        a = s.get(Alert, aid)
        if a is None:
            raise HTTPException(404)
        a.acknowledged = True
        SessionAudit(s).record("alert.ack", a.account_id, {"alert_id": aid, "actor": who})
        return {"id": aid, "acknowledged": True}

    @app.get("/api/profile")
    def api_profile(s: Session = Depends(db)):
        return _row(current_profile(s))

    @app.post("/api/profile")
    def api_set_profile(body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        try:
            p = update_profile(s, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        SessionAudit(s).record("profile.update", None, {"version": p.version, "fields": sorted(body), "actor": who})
        return _row(p)

    @app.post("/api/histories")
    async def api_history(file: UploadFile, account_size: float = Form(...), name: str = Form("history"),
                          who: str = Depends(owner), s: Session = Depends(db)):
        raw = (await file.read()).decode("utf-8-sig")
        try:
            trades = parse_trades_csv(raw)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"invalid CSV: {exc}") from exc
        h = TradeHistory(name=name, content_hash=hashlib.sha256(raw.encode()).hexdigest(), account_size=account_size,
                         trades=trades_to_json(trades))
        s.add(h)
        s.flush()
        return {"id": h.id, "trades": len(trades), "content_hash": h.content_hash}

    @app.post("/api/recommendations")
    def api_recommend(body: dict[str, Any] | None = None, who: str = Depends(owner), s: Session = Depends(db)):
        body = body or {}
        hist = s.get(TradeHistory, body["history_id"]) if body.get("history_id") else None
        rec = recommend(s, current_profile(s), hist, mc_paths=int(body.get("mc_paths", 1000)),
                        stress=float(body.get("stress", 1.25)))
        SessionAudit(s).record("recommendation.created", None, {"id": rec.id, "inputs_hash": rec.inputs["inputs_hash"]})
        return {"id": rec.id, "results": rec.results[:50]}

    @app.get("/api/recommendations/{rid}")
    def api_get_rec(rid: int, s: Session = Depends(db)):
        r = s.get(Recommendation, rid)
        if r is None:
            raise HTTPException(404)
        return _row(r)

    @app.get("/api/accounts")
    def api_accounts(s: Session = Depends(db)):
        return [{**_row(a), "status": load_account_status(s, a.id)} for a in s.scalars(select(TradingAccount))]

    @app.get("/api/accounts/{aid}")
    def api_account(aid: str, s: Session = Depends(db)):
        return _account_detail(s, aid)

    @app.post("/api/accounts/{aid}/kill-switch")
    def api_kill(aid: str, body: dict[str, Any] | None = None, who: str = Depends(owner), s: Session = Depends(db)):
        reason = (body or {}).get("reason", "manual stop from dashboard")
        exists = s.scalar(select(KillSwitchRow).where(KillSwitchRow.account_id == aid, KillSwitchRow.kind == "MANUAL",
                                                      KillSwitchRow.cleared_at.is_(None)))
        if exists is None:
            s.add(KillSwitchRow(account_id=aid, kind="MANUAL", reason=reason, details={"actor": who}))
        SessionAudit(s).record("killswitch.activate", aid, {"kind": "MANUAL", "reason": reason, "actor": who})
        return {"account_id": aid, "kill_switch": "MANUAL", "active": True}

    @app.post("/api/accounts/{aid}/kill-switch/{kind}/clear")
    def api_clear(aid: str, kind: str, body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        """Manual clear by the owner with a note. If the underlying condition persists, the next
        supervisor tick re-raises it. Not reachable from any LLM component."""
        try:
            KillSwitchKind(kind)
        except ValueError as exc:
            raise HTTPException(400, "unknown kind") from exc
        note = (body or {}).get("note", "").strip()
        if len(note) < 5:
            raise HTTPException(400, "a review note (>=5 chars) is required to clear a kill switch")
        r = s.scalar(select(KillSwitchRow).where(KillSwitchRow.account_id == aid, KillSwitchRow.kind == kind,
                                                 KillSwitchRow.cleared_at.is_(None)))
        if r is None:
            raise HTTPException(404, "not active")
        r.cleared_at, r.cleared_by, r.note = datetime.now(timezone.utc), who, note
        SessionAudit(s).record("killswitch.clear", aid, {"kind": kind, "actor": who, "note": note})
        return {"account_id": aid, "kind": kind, "cleared": True}

    @app.get("/api/audit")
    def api_audit(s: Session = Depends(db), limit: int = 100, kind: str | None = None, account_id: str | None = None):
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
        if kind:
            q = q.where(AuditLog.kind == kind)
        if account_id:
            q = q.where(AuditLog.account_id == account_id)
        return [_row(a) for a in s.scalars(q)]

    @app.get("/api/wallets")
    def api_wallets(s: Session = Depends(db)):
        return [{**_row(w), "address": mask(w.address)} for w in s.scalars(select(WalletAddress))]

    @app.post("/api/wallets")
    def api_add_wallet(body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        addr = str(body.get("address", "")).strip()
        if not (20 <= len(addr) <= 120) or any(c.isspace() for c in addr):
            raise HTTPException(400, "invalid address format")
        w = WalletAddress(label=body.get("label", "wallet"), network=body.get("network", ""),
                          currency=body.get("currency", ""), address=addr, confirmed_by_owner=False)
        s.add(w)
        s.flush()
        SessionAudit(s).record("wallet.added", None, {"id": w.id, "network": w.network, "address": mask(addr)})
        return {"id": w.id, "confirm_by_retyping_last_6": True}

    @app.post("/api/wallets/{wid}/confirm")
    def api_confirm_wallet(wid: int, body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        w = s.get(WalletAddress, wid)
        if w is None:
            raise HTTPException(404)
        if body.get("last6") != w.address[-6:]:
            raise HTTPException(400, "confirmation mismatch: re-type the last 6 characters of the full address")
        w.confirmed_by_owner, w.confirmed_at = True, datetime.now(timezone.utc)
        SessionAudit(s).record("wallet.confirmed", None, {"id": wid, "actor": who})
        return {"id": wid, "confirmed": True}

    @app.post("/api/simulations")
    def api_simulate(body: dict[str, Any], who: str = Depends(owner), s: Session = Depends(db)):
        from propguard.simulation import run_simulated_challenge
        rs = reg.ruleset_for(s, body["firm"], body["program"], body.get("phase", "phase1"),
                             float(body.get("account_size", 100000)))
        rep = run_simulated_challenge(rs, days=min(int(body.get("days", 20)), 120), seed=int(body.get("seed", 1)),
                                      assume_rules_confirmed=bool(body.get("assume_rules_confirmed", False)))
        SessionAudit(s).record("simulation.run", None, {"request": body, "report": rep.to_dict()})
        return rep.to_dict()

    # ------------------------------------------------------------------ HTML
    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request, s: Session = Depends(db)):
        prof = _profile_obj(s)
        firms = [_firm_summary(s, f, prof) for f in s.scalars(select(Firm).order_by(Firm.slug))]
        accounts = [{**_row(a), "status": load_account_status(s, a.id),
                     "kill": [k.kind for k in s.scalars(select(KillSwitchRow).where(
                         KillSwitchRow.account_id == a.id, KillSwitchRow.cleared_at.is_(None)))]}
                    for a in s.scalars(select(TradingAccount))]
        alerts = list(s.scalars(select(Alert).where(Alert.acknowledged.is_(False)).order_by(Alert.id.desc()).limit(20)))
        changes = list(s.scalars(select(RuleChange).where(RuleChange.approval_state == "PENDING")
                                 .order_by(RuleChange.id.desc()).limit(20)))
        conflicts = list(s.scalars(select(Conflict).where(Conflict.status == "OPEN")))
        recs = list(s.scalars(select(Recommendation).order_by(Recommendation.id.desc()).limit(5)))
        fresh = rules_freshness_report(s, ctx.settings.rules_max_age_h)
        return TEMPLATES.TemplateResponse(request, "dashboard.html", {
            "firms": firms, "accounts": accounts, "alerts": alerts, "changes": changes, "conflicts": conflicts,
            "recs": recs, "profile": current_profile(s), "settings": ctx.settings,
            "fresh_count": sum(1 for f in fresh if f["fresh"]), "fresh_total": len(fresh)})

    @app.get("/firms/{slug}", response_class=HTMLResponse)
    def page_firm(slug: str, request: Request, s: Session = Depends(db)):
        f = _firm_or_404(s, slug)
        return TEMPLATES.TemplateResponse(request, "firm.html", {"d": _firm_detail(s, f, _profile_obj(s))})

    @app.get("/accounts/{aid}", response_class=HTMLResponse)
    def page_account(aid: str, request: Request, s: Session = Depends(db)):
        return TEMPLATES.TemplateResponse(request, "account.html", {"d": _account_detail(s, aid)})

    @app.get("/recommendations/{rid}", response_class=HTMLResponse)
    def page_rec(rid: int, request: Request, s: Session = Depends(db)):
        r = s.get(Recommendation, rid)
        if r is None:
            raise HTTPException(404)
        return TEMPLATES.TemplateResponse(request, "recommendation.html", {"r": r})

    @app.get("/changes", response_class=HTMLResponse)
    def page_changes(request: Request, s: Session = Depends(db)):
        rows = list(s.scalars(select(RuleChange).order_by(RuleChange.id.desc()).limit(100)))
        return TEMPLATES.TemplateResponse(request, "changes.html", {"rows": rows})

    @app.post("/ui/login")
    def ui_login(token: str = Form(...)):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("pg_token", token, httponly=True, samesite="strict")
        return resp

    @app.exception_handler(HTTPException)
    def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


# ---------------------------------------------------------------------- helpers


def _row(obj) -> dict[str, Any]:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return json.loads(json.dumps(d, default=str))


def _firm_or_404(s: Session, slug: str) -> Firm:
    f = s.scalar(select(Firm).where(Firm.slug == slug))
    if f is None:
        raise HTTPException(404, "firm not found")
    return f


def _profile_obj(s: Session):
    from propguard.recommender.service import profile_from_row
    return profile_from_row(current_profile(s))


def _firm_summary(s: Session, f: Firm, prof) -> dict[str, Any]:
    elig = evaluate(prof, firm_to_dict(f))
    srcs = list(s.scalars(select(Source).where(Source.firm_id == f.id)))
    checked = [x.last_ok_at for x in srcs if x.last_ok_at]
    return {
        "slug": f.slug, "name": f.name, "status": f.status, "status_reason": f.status_reason,
        "payout": f.payout_classification, "networks": sorted({n for m in (f.payout or {}).get("methods", [])
                                                               for n in (m.get("networks") or [])}),
        "eligibility": elig.overall.value,
        "rejections": [f"{c.name}: {c.explanation}" for c in elig.checks if c.verdict.value in ("FAIL", "UNKNOWN")],
        "last_checked": max(checked).isoformat() if checked else None,
        "pending_changes": s.scalar(select(func.count()).select_from(RuleChange).where(
            RuleChange.firm_id == f.id, RuleChange.approval_state == "PENDING")) or 0,
        "open_conflicts": s.scalar(select(func.count()).select_from(Conflict).where(
            Conflict.firm_id == f.id, Conflict.status == "OPEN")) or 0,
        "programs": len(f.programs),
        "automation": f.automation or {},
    }


def _firm_detail(s: Session, f: Firm, prof) -> dict[str, Any]:
    rules = list(s.scalars(select(Rule).where(Rule.firm_id == f.id, Rule.is_current.is_(True)).order_by(Rule.rule_key)))
    ev_ids = {e for r in rules for e in (r.evidence_ids or [])}
    evidence = {e.id: e for e in s.scalars(select(Evidence).where(Evidence.id.in_(ev_ids)))} if ev_ids else {}
    sources = {x.id: x for x in s.scalars(select(Source).where(Source.firm_id == f.id))}
    return {
        "firm": _row(f), "summary": _firm_summary(s, f, prof),
        "eligibility": evaluate(prof, firm_to_dict(f)).to_dict(),
        "programs": [{**_row(p), "challenges": [_row(c) for c in p.challenges]}
                     for p in s.scalars(select(Program).where(Program.firm_id == f.id))],
        "rules": [{**_row(r), "effective_verified_at": (lambda v: v.isoformat() if v else None)(
            reg.effective_verified_at(s, r)),
            "evidence": [{"id": e, "fragment": evidence[e].fragment, "status": evidence[e].verification_status,
                          "confidence": evidence[e].confidence,
                          "url": sources[evidence[e].source_id].url if evidence[e].source_id in sources else None,
                          "doc_type": sources[evidence[e].source_id].doc_type if evidence[e].source_id in sources
                          else None} for e in (r.evidence_ids or []) if e in evidence]} for r in rules],
        "sources": [_row(x) for x in sources.values()],
        "conflicts": [_row(c) for c in s.scalars(select(Conflict).where(Conflict.firm_id == f.id))],
        "changes": [_row(c) for c in s.scalars(select(RuleChange).where(RuleChange.firm_id == f.id)
                                               .order_by(RuleChange.id.desc()).limit(30))],
    }


def _account_detail(s: Session, aid: str) -> dict[str, Any]:
    a = s.get(TradingAccount, aid)
    if a is None:
        raise HTTPException(404, "account not found")
    return {
        "account": _row(a), "status": load_account_status(s, aid),
        "kill_switches": [_row(k) for k in s.scalars(select(KillSwitchRow).where(KillSwitchRow.account_id == aid)
                                                     .order_by(KillSwitchRow.id.desc()).limit(50))],
        "blocked": [_row(o) for o in s.scalars(select(OrderRow).where(OrderRow.account_id == aid,
                                                                      OrderRow.status == "REJECTED_BY_RISK")
                                               .order_by(OrderRow.created_at.desc()).limit(50))],
        "orders": [_row(o) for o in s.scalars(select(OrderRow).where(OrderRow.account_id == aid)
                                              .order_by(OrderRow.created_at.desc()).limit(50))],
    }

