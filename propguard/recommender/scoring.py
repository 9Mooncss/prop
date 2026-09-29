"""Transparent challenge scoring: hard filters first, then a weighted, explained breakdown."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from propguard.recommender.montecarlo import MCResult
from propguard.recommender.replay import ReplayResult
from propguard.registry.eligibility import EligibilityResult, Overall, Profile
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import (
    ConsistencyRule,
    DailyLossLimit,
    InterpretationStatus,
    MaxDuration,
    MaxLoss,
    MinTradingDays,
    NewsTrading,
    PayoutFrequency,
    ProfitSplit,
    ProfitTarget,
    Tri,
    WeekendHolding,
)

SCORING_VERSION = "scoring/1.0.0"
# platform -> adapter status. Live adapters are added only after platform + firm rules research.
ADAPTER_STATUS: dict[str, str] = {}  # e.g. {"ctrader": "live"} once implemented and accepted
REQUIRED_KINDS = ("daily_loss_limit", "max_loss", "profit_target")


@dataclass
class HardFilter:
    name: str
    result: str  # PASS | FAIL | CONDITIONAL
    explanation: str


@dataclass
class Component:
    name: str
    score: float  # 0..100
    weight: float
    explanation: str


@dataclass
class ChallengeScore:
    firm: str
    program: str
    account_size: float
    price: float | None
    ruleset_id: str
    hard_filters: list[HardFilter]
    eligible: bool
    conditional: bool
    components: list[Component] = field(default_factory=list)
    total: float | None = None
    rule_compatibility: dict[str, Any] | None = None
    version: str = SCORING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def hard_filters(profile: Profile, elig: EligibilityResult, rs: RuleSet, firm: dict[str, Any],
                 program_platforms: list[str]) -> list[HardFilter]:
    out: list[HardFilter] = []
    ov = elig.overall
    out.append(HardFilter("user_eligibility", {Overall.ELIGIBLE: "PASS", Overall.CONDITIONAL: "CONDITIONAL"}.get(
        ov, "FAIL"), "; ".join(f"{c.name}:{c.verdict.value}" for c in elig.checks)))
    pc = firm.get("payout_classification", "UNKNOWN")
    if profile.payout_requirement == "DIRECT_CRYPTO":
        out.append(HardFilter("direct_crypto", "PASS" if pc == "DIRECT_CRYPTO" else "FAIL",
                              f"payout classification {pc}"))
    auto_kind = "ea_policy" if profile.automation_channel == "ea" else "api_trading"
    auto = next((r for r in rs.rules if r.kind == auto_kind), None)
    allowed = getattr(auto.params, "allowed", Tri.UNKNOWN) if auto else Tri.UNKNOWN
    out.append(HardFilter("automation_allowed", {Tri.ALLOWED: "PASS", Tri.CONDITIONAL: "CONDITIONAL"}.get(
        allowed, "FAIL"), f"{auto_kind} = {allowed.value if hasattr(allowed, 'value') else allowed}"))
    plats = [p.lower() for p in (program_platforms or firm.get("platforms") or [])]
    if profile.platforms:
        ok = bool(set(plats) & {p.lower() for p in profile.platforms})
        out.append(HardFilter("platform_compatible", "PASS" if ok else "FAIL", f"program platforms {plats}"))
    live = [p for p in plats if ADAPTER_STATUS.get(p) == "live"]
    out.append(HardFilter("integration_path", "PASS" if live else "CONDITIONAL",
                          f"live adapter for {live}" if live else
                          f"no accepted live adapter for {plats or 'unknown platforms'}: PAPER/simulation only"))
    kinds = {r.kind: r for r in rs.rules}
    missing = [k for k in REQUIRED_KINDS if k not in kinds]
    bad = [r.kind for r in rs.rules if r.criticality.value == "CRITICAL"
           and r.status in (InterpretationStatus.CONFLICT, InterpretationStatus.UNCERTAIN)]
    unverified = [r.kind for r in rs.rules if r.criticality.value == "CRITICAL"
                  and r.status == InterpretationStatus.UNVERIFIED]
    if missing or bad:
        out.append(HardFilter("critical_rules_defined", "FAIL",
                              f"missing: {missing}; uncertain/conflicting: {bad}"))
    elif unverified:
        out.append(HardFilter("critical_rules_defined", "CONDITIONAL",
                              f"not yet owner-verified against primary source: {unverified}"))
    else:
        out.append(HardFilter("critical_rules_defined", "PASS", "all critical rules confirmed"))
    return out


def rule_compatibility(rep: ReplayResult | None, mc: MCResult | None) -> dict[str, Any] | None:
    if rep is None:
        return None
    parts = []
    conduct = [v for v in rep.violations if v.rule not in ("daily_loss_limit", "max_loss", "max_duration")]
    dd = [v for v in rep.violations if v.rule in ("daily_loss_limit", "max_loss")]
    parts.append(("replay_drawdown", 0.0 if dd else 100.0, 0.35,
                  f"{len(dd)} drawdown violations in replay" if dd else "no drawdown violation in replay"))
    parts.append(("replay_conduct", 0.0 if conduct else 100.0, 0.20,
                  ", ".join(sorted({v.rule for v in conduct})) or "no conduct/holding/lot violations"))
    heads = [float(a.min_headroom_frac) for a in rep.approaches]
    closest = min(heads) if heads else 1.0
    parts.append(("closest_approach", max(0.0, min(100.0, closest * 100)), 0.20,
                  f"closest approach left {closest:.0%} of an allowance"))
    if mc is not None:
        parts.append(("monte_carlo_breach", (1 - mc.breach_rate) * 100, 0.25,
                      f"breach in {mc.breach_rate:.0%} of {mc.paths} resampled paths "
                      f"(stress x{mc.stress_loss_multiplier}); p05 min headroom {mc.p05_min_headroom_frac:.0%}"))
    wsum = sum(p[2] for p in parts)
    score = sum(p[1] * p[2] for p in parts) / wsum
    return {"score": round(score, 1), "components": [dict(zip(("name", "score", "weight", "explanation"), p))
                                                     for p in parts],
            "replay_outcome": rep.outcome, "violations": [asdict(v) for v in rep.violations[:50]],
            "approaches": [asdict(a) for a in rep.approaches], "assumptions": rep.assumptions,
            "monte_carlo": mc.to_dict() if mc else None}


def score_challenge(*, firm: dict[str, Any], program_slug: str, program_platforms: list[str], refund: str,
                    account_size: float, price: float | None, rs: RuleSet, profile: Profile,
                    elig: EligibilityResult, rep: ReplayResult | None = None,
                    mc: MCResult | None = None) -> ChallengeScore:
    hf = hard_filters(profile, elig, rs, firm, program_platforms)
    eligible = all(h.result != "FAIL" for h in hf)
    cs = ChallengeScore(firm["slug"], program_slug, account_size, price, rs.ruleset_id, hf, eligible,
                        any(h.result == "CONDITIONAL" for h in hf))
    comps: list[Component] = []

    def add(name, score, weight, expl):
        comps.append(Component(name, round(max(0.0, min(100.0, score)), 1), weight, expl))

    if price:
        per_k = price / (account_size / 1000)
        add("cost", 100 - per_k * 10, 1.0, f"${per_k:.2f} per $1k of account")
    else:
        add("cost", 50, 0.5, "price unknown")
    rl = (refund or "").lower()
    add("refund", 100 if "refund" in rl and "no" not in rl else 50 if not rl or "unknown" in rl else 20, 0.5,
        refund or "unknown")
    p, _ = rs.get(ProfitTarget)
    add("profit_target", 100 - float(p.pct) * 6 if p else 0, 1.0, f"{p.pct}%" if p else "unknown")
    dl, _ = rs.get(DailyLossLimit)
    add("daily_loss", (float(dl.pct) * 15 if dl.enabled else 100) if dl else 0, 1.0,
        (f"{dl.pct}% ({dl.reference}, floating={'yes' if dl.includes_floating else 'no'})" if dl.enabled
         else "none") if dl else "unknown")
    ml, _ = rs.get(MaxLoss)
    arch = {"static": 100, "trailing_lock_at_initial": 70, "eod_trailing": 60, "trailing": 40}
    add("drawdown_architecture", arch.get(ml.mode, 0) if ml else 0, 1.5, ml.mode if ml else "unknown")
    add("max_loss", float(ml.pct) * 8 if ml else 0, 1.0, f"{ml.pct}%" if ml else "unknown")
    d, _ = rs.get(MaxDuration)
    add("duration", 100 if (d and d.days is None) else (min(100, d.days * 2) if d else 50), 0.7,
        "unlimited" if d and d.days is None else f"{d.days} days" if d else "unknown")
    m, _ = rs.get(MinTradingDays)
    add("min_days", 100 - (m.days * 8 if m else 30), 0.4, f"{m.days}" if m else "unknown")
    c, _ = rs.get(ConsistencyRule)
    add("consistency", 100 if c is None else 50, 0.5, "none captured" if c is None else
        f"best day <= {c.max_single_day_pct_of_total}%")
    n, _ = rs.get(NewsTrading)
    add("news", {Tri.ALLOWED: 100, Tri.CONDITIONAL: 60}.get(n.allowed, 30) if n else 30, 0.5,
        n.allowed.value if n else "unknown")
    w, _ = rs.get(WeekendHolding)
    add("weekend", {Tri.ALLOWED: 100}.get(w.allowed, 50) if w else 40, 0.3, w.allowed.value if w else "unknown")
    sp, _ = rs.get(ProfitSplit)
    add("profit_split", float(sp.pct) if sp else 40, 0.8, f"{sp.pct}%" if sp else "unknown")
    pf, _ = rs.get(PayoutFrequency)
    add("payout_frequency", 100 if (pf and pf.on_demand) else (100 - (pf.every_days or 30)) if pf else 40, 0.5,
        ("on demand" if pf.on_demand else f"every {pf.every_days} days") if pf else "unknown")
    methods = (firm.get("payout") or {}).get("methods") or []
    nets = sorted({n for mth in methods for n in (mth.get("networks") or [])})
    pc = firm.get("payout_classification", "UNKNOWN")
    add("crypto_rails", (100 if pc == "DIRECT_CRYPTO" else 40 if pc == "CRYPTO_VIA_PROVIDER" else 0)
        - (0 if nets else 20), 1.0, f"{pc}; networks {nets or 'unconfirmed'}")
    rc = rule_compatibility(rep, mc)
    cs.rule_compatibility = rc
    if rc is not None:
        add("rule_compatibility", rc["score"], 3.0, f"replay {rc['replay_outcome']}")
    cs.components = comps
    if eligible:
        cs.total = round(sum(x.score * x.weight for x in comps) / sum(x.weight for x in comps), 1)
    return cs

