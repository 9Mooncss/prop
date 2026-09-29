"""LIVE-mode gate. New installations are PAPER_ONLY; LIVE requires every condition below.

1. Environment: ``PROPGUARD_ALLOW_LIVE=true`` (operator intent at deployment level).
2. Persistent per-account LIVE approval set by the owner via CLI with an explicit confirmation
   phrase (``propguard live enable``), stored with timestamp and code version.
3. A passing Risk Engine acceptance-suite marker for the *current* code fingerprint
   (``propguard acceptance`` writes it only if the suite passes).
4. The adapter declares ``is_live`` and the account is not in PAPER mode.

The gate is checked when the ExecutionEngine is constructed *and* before every live submission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CONFIRM_PHRASE = "I UNDERSTAND THIS SENDS REAL ORDERS"


class LiveNotAllowed(RuntimeError):
    pass


class LiveApprovalStore(Protocol):
    def is_live_enabled(self, account_id: str) -> bool: ...


def code_fingerprint() -> str:
    """Hash of the risk/execution/rules source files -- acceptance must be re-run after changes."""
    root = Path(__file__).resolve().parent.parent
    h = hashlib.sha256()
    for sub in ("risk", "execution", "rules"):
        for p in sorted((root / sub).glob("*.py")):
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:24]


@dataclass
class LiveGate:
    allow_live_env: bool
    approvals: LiveApprovalStore | None
    acceptance_marker: Path

    def acceptance_ok(self) -> bool:
        try:
            data = json.loads(self.acceptance_marker.read_text())
        except (OSError, ValueError):
            return False
        return data.get("passed") is True and data.get("fingerprint") == code_fingerprint()

    def check(self, account_id: str, adapter_is_live: bool) -> None:
        if not adapter_is_live:
            return
        problems = []
        if not self.allow_live_env:
            problems.append("PROPGUARD_ALLOW_LIVE is not true")
        if self.approvals is None or not self.approvals.is_live_enabled(account_id):
            problems.append(f"account {account_id} has no explicit LIVE approval")
        if not self.acceptance_ok():
            problems.append("risk acceptance suite has not passed for current code")
        if problems:
            raise LiveNotAllowed("LIVE execution refused: " + "; ".join(problems))


PAPER_ONLY_GATE = LiveGate(allow_live_env=False, approvals=None, acceptance_marker=Path("/nonexistent"))
