"""Recommendation run: hard filters -> scores -> persisted, fully traceable record."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from propguard.db.models import Firm, Recommendation, TradeHistory, UserProfile
from propguard.recommender.montecarlo import simulate
from propguard.recommender.replay import Trade, replay
from propguard.recommender.scoring import SCORING_VERSION, score_challenge
from propguard.registry.eligibility import Profile, evaluate, firm_to_dict
from propguard.registry.service import ruleset_for

ENGINE_VERSION = f"recommender/1.0.0+{SCORING_VERSION}"


def profile_from_row(p: UserProfile) -> Profile:
    return Profile(citizenship=p.citizenship, residence_country=p.residence_country,
                   residence_region=p.residence_region, tax_residency=p.tax_residency,
                   kyc_documents=tuple(p.kyc_documents or []), ip_location_country=p.ip_location_country,
                   payout_requirement=p.payout_requirement, platforms=tuple(p.platforms or []),
                   automation_channel=p.automation_channel, version=p.version)


def trades_from_row(h: TradeHistory) -> list[Trade]:
    out = []
    for t in h.trades:
        out.append(Trade(trade_id=t["trade_id"], symbol=t["symbol"], side=t["side"], lots=Decimal(t["lots"]),
                         open_time=datetime.fromisoformat(t["open_time"]),
                         close_time=datetime.fromisoformat(t["close_time"]), pnl=Decimal(t["pnl"]),
                         commission=Decimal(t.get("commission", "0")), swap=Decimal(t.get("swap", "0")),
                         mae=Decimal(t["mae"]) if t.get("mae") is not None else None))
    return out


def trades_to_json(trades: list[Trade]) -> list[dict[str, Any]]:
    return [{"trade_id": t.trade_id, "symbol": t.symbol, "side": t.side, "lots": str(t.lots),
             "open_time": t.open_time.isoformat(), "close_time": t.close_time.isoformat(), "pnl": str(t.pnl),
             "commission": str(t.commission), "swap": str(t.swap),
             "mae": None if t.mae is None else str(t.mae)} for t in trades]


def recommend(s: Session, profile_row: UserProfile, history: TradeHistory | None = None,
              mc_paths: int = 1000, stress: float = 1.25) -> Recommendation:
    profile = profile_from_row(profile_row)
    trades = trades_from_row(history) if history else None
    results = []
    for firm in s.scalars(select(Firm).order_by(Firm.slug)):
        fd = firm_to_dict(firm)
        elig = evaluate(profile, fd)
        for prog in firm.programs:
            phase = prog.phases[0] if prog.phases else "phase1"
            sizes = prog.challenges or []
            variants = [(c.account_size, c.price) for c in sizes] or [(100000.0, None)]
            for size, price in variants:
                rs = ruleset_for(s, firm.slug, prog.slug, phase, size)
                rep = mc = None
                if trades:
                    rep = replay(trades, rs, history.account_size)
                    mc = simulate(rep, rs, paths=mc_paths, stress_loss_multiplier=stress)
                sc = score_challenge(firm=fd, program_slug=prog.slug, program_platforms=prog.platforms,
                                     refund=prog.refund, account_size=size, price=price, rs=rs, profile=profile,
                                     elig=elig, rep=rep, mc=mc)
                d = sc.to_dict()
                d["eligibility"] = elig.to_dict()
                d["phase"] = phase
                d["rules"] = [{"rule_id": r.rule_id, "kind": r.kind, "status": r.status.value,
                               "confidence": str(r.confidence), "params": r.params.model_dump(mode="json"),
                               "evidence_ids": list(r.evidence_ids)} for r in rs.rules]
                d["firm_status"] = firm.status
                results.append(d)
    results.sort(key=lambda d: (not d["eligible"], -(d["total"] or -1)))
    inputs = {
        "profile": {"version": profile.version, **{k: v for k, v in profile.__dict__.items() if k != "version"}},
        "history": None if history is None else {"id": history.id, "content_hash": history.content_hash,
                                                 "account_size": history.account_size, "n_trades": len(history.trades)},
        "mc": {"paths": mc_paths, "stress_loss_multiplier": stress},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    inputs["inputs_hash"] = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    rec = Recommendation(profile_version=profile.version, history_id=history.id if history else None,
                         inputs=json.loads(json.dumps(inputs, default=str)),
                         results=json.loads(json.dumps(results, default=str)), engine_version=ENGINE_VERSION)
    s.add(rec)
    s.flush()
    return rec

