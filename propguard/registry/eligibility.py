"""User eligibility checks. Citizenship, residence, tax residency, KYC documents and IP location are
separate inputs. Nothing is inferred: an unset field yields UNKNOWN, never a pass.

This module only *reports* restrictions. It never suggests ways around KYC, geoblocks, VPN bans or
country restrictions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

UA_OCCUPIED_REGION_TERMS = ("crimea", "sevastopol", "donetsk", "luhansk", "lugansk", "kherson", "zaporizhzhia",
                            "zaporozhye")


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    CONDITIONAL = "CONDITIONAL"


class Overall(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    CONDITIONAL = "CONDITIONAL"  # eligible if an owner-confirmable condition holds
    UNKNOWN = "UNKNOWN"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True)
class Profile:
    citizenship: str = "UA"
    residence_country: str | None = None
    residence_region: str | None = None
    tax_residency: str | None = None
    kyc_documents: tuple[dict[str, str], ...] = ()
    ip_location_country: str | None = None
    payout_requirement: str = "DIRECT_CRYPTO"  # DIRECT_CRYPTO | ANY_CRYPTO | ANY
    platforms: tuple[str, ...] = ()
    automation_channel: str = "api"
    version: int = 0


@dataclass
class Check:
    name: str
    verdict: Verdict
    explanation: str
    evidence: list[int] = field(default_factory=list)


@dataclass
class EligibilityResult:
    firm_slug: str
    overall: Overall
    checks: list[Check]

    def to_dict(self) -> dict[str, Any]:
        return {"firm": self.firm_slug, "overall": self.overall.value,
                "checks": [{**asdict(c), "verdict": c.verdict.value} for c in self.checks]}


def _prohibited(j: dict) -> set[str]:
    return {c.upper() for c in j.get("prohibited_countries") or []}


def evaluate(profile: Profile, firm: dict[str, Any]) -> EligibilityResult:
    """``firm`` is a plain dict with keys: slug, status, jurisdiction, kyc, payout_classification,
    automation, platforms (as stored in the registry)."""
    checks: list[Check] = []
    j = firm.get("jurisdiction") or {}
    prohibited = _prohibited(j)
    basis = j.get("restriction_basis", "unknown")
    ev = list(j.get("evidence_ids") or [])

    status = firm.get("status")
    if status == "EXCLUDED":
        checks.append(Check("firm_status", Verdict.FAIL, "firm is EXCLUDED in the registry"))
    elif status == "INSUFFICIENT_DATA":
        checks.append(Check("firm_status", Verdict.UNKNOWN, "insufficient primary-source data for this firm"))
    else:
        checks.append(Check("firm_status", Verdict.PASS, f"firm status {status}"))

    # citizenship
    cit = profile.citizenship.upper()
    if cit == "UA" and j.get("ukraine_citizens") == "PROHIBITED":
        checks.append(Check("citizenship", Verdict.FAIL, "firm does not accept Ukrainian citizens", ev))
    elif cit in prohibited and basis in ("citizenship", "both", "unknown"):
        checks.append(Check("citizenship", Verdict.FAIL,
                            f"{cit} on firm's prohibited list (basis: {basis})", ev))
    elif cit == "UA" and j.get("ukraine_citizens") in (None, "UNKNOWN"):
        checks.append(Check("citizenship", Verdict.UNKNOWN, "firm policy for Ukrainian citizens not confirmed", ev))
    else:
        checks.append(Check("citizenship", Verdict.PASS, f"{cit} citizenship not restricted per sources", ev))

    # residence (country + region)
    res = (profile.residence_country or "").upper() or None
    if res is None:
        checks.append(Check("residence", Verdict.UNKNOWN, "set country of actual residence in profile"))
    elif res == "UA" and j.get("ukraine_residents") == "PROHIBITED":
        checks.append(Check("residence", Verdict.FAIL, "firm does not accept residents of Ukraine", ev))
    elif res in prohibited and basis in ("residence", "both", "unknown"):
        checks.append(Check("residence", Verdict.FAIL, f"residence {res} on prohibited list", ev))
    elif res == "UA" and j.get("ukraine_residents") in (None, "UNKNOWN"):
        checks.append(Check("residence", Verdict.UNKNOWN, "firm policy for residents of Ukraine not confirmed", ev))
    else:
        region_text = " ".join([j.get("notes") or "", " ".join(j.get("restricted_regions") or [])]).lower()
        region_restricted = res == "UA" and any(t in region_text for t in UA_OCCUPIED_REGION_TERMS)
        if region_restricted:
            reg = (profile.residence_region or "").lower()
            if not reg:
                checks.append(Check("residence_region", Verdict.CONDITIONAL,
                                    "firm restricts specific Ukrainian regions; set residence_region to confirm", ev))
            elif any(t in reg for t in UA_OCCUPIED_REGION_TERMS):
                checks.append(Check("residence_region", Verdict.FAIL, "residence region is restricted by firm", ev))
            else:
                checks.append(Check("residence_region", Verdict.PASS, "residence region not in firm's restricted list"))
        checks.append(Check("residence", Verdict.PASS, f"residence {res} not restricted per sources", ev))

    # tax residency (separate entity; firms rarely restrict on it -- only fail on explicit list match)
    tax = (profile.tax_residency or "").upper() or None
    if tax is None:
        checks.append(Check("tax_residency", Verdict.UNKNOWN, "set tax residency in profile (never assumed)"))
    elif tax in prohibited and basis in ("both", "unknown"):
        checks.append(Check("tax_residency", Verdict.CONDITIONAL,
                            f"tax residency {tax} appears on prohibited list; confirm with firm", ev))
    else:
        checks.append(Check("tax_residency", Verdict.PASS, f"tax residency {tax}"))

    # KYC documents
    kyc = firm.get("kyc") or {}
    req_docs = {d.lower() for d in kyc.get("documents") or []}
    have = {(d.get("type") or "").lower() for d in profile.kyc_documents}
    doc_countries = {(d.get("country") or "").upper() for d in profile.kyc_documents}
    if doc_countries & prohibited:
        checks.append(Check("kyc_documents", Verdict.FAIL,
                            f"KYC document issuing country {sorted(doc_countries & prohibited)} is prohibited"))
    elif not req_docs:
        checks.append(Check("kyc_documents", Verdict.UNKNOWN, "firm KYC document requirements not confirmed"))
    elif not profile.kyc_documents:
        checks.append(Check("kyc_documents", Verdict.CONDITIONAL,
                            f"firm requires {sorted(req_docs)}; add your available KYC documents to profile"))
    elif req_docs - have:
        checks.append(Check("kyc_documents", Verdict.CONDITIONAL, f"missing documents: {sorted(req_docs - have)}"))
    else:
        checks.append(Check("kyc_documents", Verdict.PASS, "required documents available"))

    # IP location (reported only; never suggests changing it)
    ip = (profile.ip_location_country or "").upper() or None
    if ip and ip in prohibited:
        checks.append(Check("ip_location", Verdict.FAIL,
                            f"your actual IP location {ip} is on the firm's restricted list"))
    else:
        checks.append(Check("ip_location", Verdict.PASS if ip else Verdict.UNKNOWN,
                            f"IP location {ip}" if ip else "set your actual IP location country"))

    # payout rail
    pc = firm.get("payout_classification", "UNKNOWN")
    req = profile.payout_requirement
    if req == "DIRECT_CRYPTO":
        v = Verdict.PASS if pc == "DIRECT_CRYPTO" else (Verdict.UNKNOWN if pc == "UNKNOWN" else Verdict.FAIL)
        checks.append(Check("crypto_payout", v, f"payout classification {pc}; required DIRECT_CRYPTO"))
    elif req == "ANY_CRYPTO":
        ok = pc in ("DIRECT_CRYPTO", "CRYPTO_VIA_PROVIDER")
        checks.append(Check("crypto_payout", Verdict.PASS if ok else (Verdict.UNKNOWN if pc == "UNKNOWN"
                                                                      else Verdict.FAIL), f"payout {pc}"))
    else:
        checks.append(Check("crypto_payout", Verdict.PASS, f"payout {pc} (no crypto requirement)"))

    # automation permission for the channel the owner will use
    auto = firm.get("automation") or {}
    key = "ea_bots" if profile.automation_channel == "ea" else "api_trading"
    val = auto.get(key, "UNKNOWN")
    checks.append(Check("automation", {"ALLOWED": Verdict.PASS, "CONDITIONAL": Verdict.CONDITIONAL,
                                       "PROHIBITED": Verdict.FAIL}.get(val, Verdict.UNKNOWN),
                        f"{key} = {val}" + (f" ({auto.get('conditions')})" if auto.get("conditions") else "")))

    # platform
    fplat = {p.lower() for p in firm.get("platforms") or []}
    if profile.platforms:
        want = {p.lower() for p in profile.platforms}
        if not fplat:
            checks.append(Check("platform", Verdict.UNKNOWN, "firm platforms not confirmed"))
        else:
            checks.append(Check("platform", Verdict.PASS if want & fplat else Verdict.FAIL,
                                f"firm platforms {sorted(fplat)}; you use {sorted(want)}"))

    verdicts = {c.verdict for c in checks}
    overall = (Overall.INELIGIBLE if Verdict.FAIL in verdicts else Overall.UNKNOWN if Verdict.UNKNOWN in verdicts
               else Overall.CONDITIONAL if Verdict.CONDITIONAL in verdicts else Overall.ELIGIBLE)
    return EligibilityResult(firm.get("slug", ""), overall, checks)


def firm_to_dict(firm) -> dict[str, Any]:
    return {"slug": firm.slug, "status": firm.status, "jurisdiction": firm.jurisdiction or {}, "kyc": firm.kyc or {},
            "payout_classification": firm.payout_classification, "automation": firm.automation or {},
            "platforms": firm.platforms or []}
