"""Structural diff of block lists and deterministic change classification."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# keyword -> rule kinds potentially affected. Deliberately broad: false positives only cost a review.
KEYWORDS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"(?i)daily (loss|drawdown)|max(imum)? daily"), ("daily_loss_limit",)),
    (re.compile(r"(?i)(max(imum)?|overall|total) (loss|drawdown)|trailing|high[- ]water"), ("max_loss",)),
    (re.compile(r"(?i)drawdown|equity|balance|floating"), ("daily_loss_limit", "max_loss")),
    (re.compile(r"(?i)profit target"), ("profit_target",)),
    (re.compile(r"(?i)news"), ("news_trading",)),
    (re.compile(r"(?i)weekend"), ("weekend_holding",)),
    (re.compile(r"(?i)overnight"), ("overnight_holding",)),
    (re.compile(r"(?i)expert advisor|\bea\b|\beas\b|bot|algorithm|automated"), ("ea_policy",)),
    (re.compile(r"(?i)\bapi\b"), ("api_trading",)),
    (re.compile(r"(?i)copy[- ]?trad"), ("copy_trading",)),
    (re.compile(r"(?i)\bvps\b|\bvpn\b|ip address"), ("ip_vps_restriction",)),
    (re.compile(r"(?i)high[- ]frequency|\bhft\b|latency|arbitrage|tick scalp"), ("hft_restriction",
                                                                                  "latency_arbitrage")),
    (re.compile(r"(?i)consisten"), ("consistency_rule",)),
    (re.compile(r"(?i)prohibited|forbidden|not allowed|banned|violation"), ("prohibited_strategies",)),
    (re.compile(r"(?i)lot size|position size|leverage"), ("max_lot_size", "leverage")),
    (re.compile(r"(?i)trading days?|minimum days"), ("min_trading_days",)),
    (re.compile(r"(?i)countr|residen|citizen|jurisdiction|sanction|restricted"), ("jurisdiction",)),
    (re.compile(r"(?i)\bkyc\b|identity|verification|proof of address|passport"), ("kyc",)),
    (re.compile(r"(?i)payout|withdraw|reward|crypto|usdt|usdc|bitcoin|wallet|rise|deel"), ("payout",)),
    (re.compile(r"(?i)profit split"), ("profit_split",)),
    (re.compile(r"(?i)refund"), ("refund_policy",)),
    (re.compile(r"(?i)\bmt4\b|\bmt5\b|ctrader|dxtrade|match[- ]?trader|tradelocker|platform"),
     ("platform_requirement",)),
]
CRITICAL_TOPICS = {"daily_loss_limit", "max_loss", "news_trading", "weekend_holding", "overnight_holding",
                   "ea_policy", "api_trading", "jurisdiction", "kyc", "payout", "platform_requirement",
                   "hft_restriction", "prohibited_strategies", "copy_trading", "ip_vps_restriction"}
_NUM = re.compile(r"\d+(?:[.,]\d+)?\s*%?")


@dataclass
class ChangedHunk:
    op: str  # replace | insert | delete
    old: list[str]
    new: list[str]
    context_before: list[str]
    context_after: list[str]

    def text(self, limit: int = 2000) -> str:
        parts = [f"CONTEXT: {' | '.join(self.context_before)}"] if self.context_before else []
        if self.old:
            parts.append("OLD: " + " | ".join(self.old))
        if self.new:
            parts.append("NEW: " + " | ".join(self.new))
        if self.context_after:
            parts.append(f"CONTEXT: {' | '.join(self.context_after)}")
        return "\n".join(parts)[:limit]


@dataclass
class ChangeAssessment:
    severity: str  # INFO | WARNING | HIGH | CRITICAL
    topics: set[str] = field(default_factory=set)
    numbers_changed: bool = False
    hunks: list[ChangedHunk] = field(default_factory=list)

    @property
    def needs_semantic_review(self) -> bool:
        return self.severity in ("HIGH", "CRITICAL")


def diff_blocks(old: list[str], new: list[str], context: int = 1) -> list[ChangedHunk]:
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(ChangedHunk(tag, old[i1:i2], new[j1:j2], new[max(0, j1 - context):j1],
                                 new[j2:j2 + context]))
    return hunks


def classify(hunks: list[ChangedHunk]) -> ChangeAssessment:
    topics: set[str] = set()
    numbers_changed = False
    for h in hunks:
        text = " ".join(h.old + h.new + h.context_before + h.context_after)
        for pat, kinds in KEYWORDS:
            if pat.search(text):
                topics.update(kinds)
        if sorted(_NUM.findall(" ".join(h.old))) != sorted(_NUM.findall(" ".join(h.new))):
            numbers_changed = True
    if not hunks:
        sev = "INFO"
    elif topics & CRITICAL_TOPICS:
        sev = "CRITICAL" if numbers_changed or topics & {"ea_policy", "api_trading", "jurisdiction", "news_trading",
                                                         "weekend_holding"} else "HIGH"
    elif topics:
        sev = "HIGH" if numbers_changed else "WARNING"
    else:
        sev = "INFO"
    return ChangeAssessment(sev, topics, numbers_changed, hunks)
