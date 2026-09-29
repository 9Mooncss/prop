"""Optional LLM assistance for *interpreting changed text fragments*.

Hard boundaries (enforced by construction, see docs/ARCHITECTURE.md):
* Output is only ever a *proposal* stored as a PENDING RuleChange. It cannot confirm rules, clear kill
  switches, change limits, or touch the Risk Engine / execution path (no imports from there).
* Only the changed fragment plus minimal context is sent -- never full pages, never secrets
  (fragments are passed through the log redactor first).
* Tiered: cheapest configured model first; escalate to the strong tier when the cheap result is
  ambiguous or low-confidence. Model ids come from settings, not code.
* Results are cached by input hash; a monthly budget cap stops calls (fail closed = rule stays
  UNCERTAIN, which blocks new risk).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from propguard.logging_setup import redact
from propguard.rules.types import RULE_TYPES, parse_params

log = logging.getLogger(__name__)
PROMPT_VERSION = "change-analysis/1"

# USD per million tokens (input, output). Override via PROPGUARD_LLM_PRICES='{"model": [in, out]}'.
DEFAULT_PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
}

SYSTEM_PROMPT = (
    "You analyse a small changed fragment of an official prop-trading-firm document. "
    "Report only what the fragment literally states. If the fragment does not state a value "
    "unambiguously, set ambiguous=true and leave new_params empty. Never infer values from general "
    "industry practice. Never suggest ways to circumvent any rule, KYC, country or platform "
    "restriction. Allowed rule kinds: " + ", ".join(sorted(RULE_TYPES)) + ". "
    "new_params must use the parameter names of that rule kind's schema; allowed-values fields use "
    "ALLOWED | PROHIBITED | CONDITIONAL | UNKNOWN."
)


class ChangeProposal(BaseModel):
    affected_rule_kind: str | None = Field(default=None, description="one of the allowed rule kinds or null")
    new_params: dict[str, Any] = Field(default_factory=dict)
    ambiguous: bool = True
    confidence: float = Field(default=0.0, ge=0, le=1)
    summary: str = ""
    quote: str = Field(default="", description="verbatim sentence from the fragment supporting the values")


@dataclass
class Analysis:
    proposal: ChangeProposal
    model: str | None
    tier: str | None
    cached: bool = False
    cost_usd: float = 0.0
    valid_params: bool = False


class LLMLedger(Protocol):
    def cached(self, input_hash: str) -> dict | None: ...
    def spent_this_month(self) -> float: ...
    def record(self, tier: str, model: str, purpose: str, input_hash: str, in_tok: int, out_tok: int,
               cost: float, result: dict) -> None: ...


class ChangeAnalyzer:
    def __init__(self, provider: str, cheap_model: str, strong_model: str, budget_usd: float,
                 ledger: LLMLedger | None, api_key: str | None = None,
                 client_factory: Callable[[], Any] | None = None, prices: dict | None = None) -> None:
        self.provider = provider
        self.models = {"cheap": cheap_model, "strong": strong_model}
        self.budget = budget_usd
        self.ledger = ledger
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        self._api_key = api_key
        self._client_factory = client_factory
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.provider == "anthropic" and (self._api_key is not None or self._client_factory is not None)

    def analyze(self, fragment: str, firm: str, topics: list[str], doc_type: str) -> Analysis:
        if not self.enabled:
            return Analysis(ChangeProposal(summary="LLM disabled; manual review required"), None, None)
        safe = redact(fragment)[:4000]
        user = json.dumps({"firm": firm, "document_type": doc_type, "candidate_topics": sorted(topics),
                           "changed_fragment": safe}, sort_keys=True)
        ih = hashlib.sha256((PROMPT_VERSION + "|" + user).encode()).hexdigest()
        if self.ledger and (hit := self.ledger.cached(ih)):
            p = ChangeProposal.model_validate(hit.get("proposal", {}))
            return Analysis(p, hit.get("model"), hit.get("tier"), cached=True, valid_params=self._valid(p))
        a = self._call("cheap", user, ih)
        if a.proposal.ambiguous or a.proposal.confidence < 0.8 or not a.valid_params:
            strong = self._call("strong", user, ih + ":strong")
            strong.cost_usd += a.cost_usd
            a = strong
        return a

    def _valid(self, p: ChangeProposal) -> bool:
        if p.ambiguous or not p.affected_rule_kind or p.affected_rule_kind not in RULE_TYPES:
            return False
        try:
            parse_params(p.affected_rule_kind, p.new_params)
            return True
        except ValueError:
            return False

    def _call(self, tier: str, user: str, ih: str) -> Analysis:
        model = self.models[tier]
        if self.ledger and self.ledger.spent_this_month() >= self.budget:
            log.warning("llm budget exhausted; change stays UNCERTAIN", extra={"ctx_tier": tier})
            return Analysis(ChangeProposal(summary="LLM budget exhausted; manual review required"), model, tier)
        client = self._get_client()
        try:
            resp = client.messages.parse(
                model=model, max_tokens=1024, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}], output_format=ChangeProposal)
        except Exception as exc:  # noqa: BLE001 -- any API failure is fail-closed
            log.warning("llm call failed: %s", type(exc).__name__)
            return Analysis(ChangeProposal(summary=f"LLM error {type(exc).__name__}; manual review"), model, tier)
        if getattr(resp, "stop_reason", None) == "refusal" or resp.parsed_output is None:
            p = ChangeProposal(summary="model declined / no structured output; manual review")
        else:
            p = resp.parsed_output
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        pin, pout = self.prices.get(model, (10.0, 50.0))
        cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout
        if self.ledger:
            self.ledger.record(tier, model, "change_analysis", ih, in_tok, out_tok, cost,
                               {"proposal": p.model_dump(), "model": model, "tier": tier})
        return Analysis(p, model, tier, cost_usd=cost, valid_params=self._valid(p))

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import anthropic  # optional dependency
                self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client
