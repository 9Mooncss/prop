"""Construction of shared services from Settings (used by API, worker and CLI)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from propguard.config import Settings, get_settings
from propguard.db.models import Setting, TradingAccount, UserProfile
from propguard.db.session import session_factory, session_scope
from propguard.logging_setup import register_secret
from propguard.notify.base import MultiNotifier, NullNotifier, TelegramNotifier, WebhookNotifier


@dataclass
class AppContext:
    settings: Settings

    @classmethod
    def create(cls, settings: Settings | None = None) -> "AppContext":
        st = settings or get_settings()
        for secret in (st.api_token, st.anthropic_api_key, st.webhook_url, st.telegram_bot_token):
            if secret is not None:
                register_secret(secret.get_secret_value())
        return cls(st)

    @cached_property
    def sf(self) -> sessionmaker[Session]:
        return session_factory(self.settings.database_url)

    @cached_property
    def notifier(self):
        ns = []
        if self.settings.webhook_url:
            ns.append(WebhookNotifier(self.settings.webhook_url.get_secret_value()))
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            ns.append(TelegramNotifier(self.settings.telegram_bot_token.get_secret_value(),
                                       self.settings.telegram_chat_id))
        return MultiNotifier(*ns) if ns else NullNotifier()

    def analyzer(self, s: Session):
        from propguard.llm.client import ChangeAnalyzer
        from propguard.monitor.pipeline import SqlLLMLedger
        key = self.settings.anthropic_api_key
        return ChangeAnalyzer(self.settings.llm_provider, self.settings.llm_model_cheap,
                              self.settings.llm_model_strong, self.settings.llm_monthly_budget_usd,
                              SqlLLMLedger(s), api_key=key.get_secret_value() if key else None)

    def fetcher(self):
        from propguard.monitor.fetch import Fetcher
        return Fetcher(self.settings.monitor_user_agent, self.settings.monitor_min_delay_per_host_s)

    def live_gate(self):
        from propguard.db.stores import SqlLiveApprovals
        from propguard.execution.live_gate import LiveGate
        return LiveGate(self.settings.live_allowed_env, SqlLiveApprovals(self.sf), self.settings.acceptance_marker)


def current_profile(s: Session) -> UserProfile:
    p = s.scalar(select(UserProfile).order_by(UserProfile.version.desc()))
    if p is None:  # default: citizenship Ukraine; everything else deliberately unset
        p = UserProfile(version=1, citizenship="UA", payout_requirement="DIRECT_CRYPTO", kyc_documents=[],
                        preferred_networks=[], platforms=[], automation_channel="api")
        s.add(p)
        s.flush()
    return p


PROFILE_FIELDS = ("citizenship", "residence_country", "residence_region", "tax_residency", "kyc_documents",
                  "ip_location_country", "payout_requirement", "preferred_networks", "platforms",
                  "automation_channel", "max_budget_usd")


def update_profile(s: Session, changes: dict[str, Any]) -> UserProfile:
    cur = current_profile(s)
    data = {f: getattr(cur, f) for f in PROFILE_FIELDS}
    for k, v in changes.items():
        if k not in PROFILE_FIELDS:
            raise ValueError(f"unknown profile field {k}")
        if k in ("citizenship", "residence_country", "tax_residency", "ip_location_country") and v:
            v = str(v).upper()
            if len(v) != 2 or not v.isalpha():
                raise ValueError(f"{k} must be an ISO-3166 alpha-2 code")
        if k == "payout_requirement" and v not in ("DIRECT_CRYPTO", "ANY_CRYPTO", "ANY"):
            raise ValueError("payout_requirement must be DIRECT_CRYPTO | ANY_CRYPTO | ANY")
        data[k] = v if v != "" else None
    new = UserProfile(version=cur.version + 1, **data)
    s.add(new)
    s.flush()
    return new


def save_account_status(sf: sessionmaker[Session], account_id: str, status: dict[str, Any]) -> None:
    with session_scope(sf) as s:
        key = f"account_status:{account_id}"
        row = s.get(Setting, key)
        payload = json.loads(json.dumps({**status, "ts": datetime.now(timezone.utc).isoformat()}, default=str))
        if row is None:
            s.add(Setting(key=key, value=payload))
        else:
            row.value = payload


def load_account_status(s: Session, account_id: str) -> dict[str, Any] | None:
    row = s.get(Setting, f"account_status:{account_id}")
    return row.value if row else None


def ensure_account(s: Session, account_id: str, firm: str, program: str, phase: str, size: float,
                   adapter: str = "simulated") -> TradingAccount:
    a = s.get(TradingAccount, account_id)
    if a is None:
        a = TradingAccount(id=account_id, name=account_id, firm_slug=firm, program_slug=program, phase=phase,
                           initial_balance=size, adapter=adapter, mode="PAPER")
        s.add(a)
        s.flush()
    return a


def status_from_session(session) -> dict[str, Any]:
    """Snapshot of an AccountSession for the dashboard (limits, positions, orders, kill switches)."""
    ctx = session.context()
    a = session.risk.assess(ctx)
    snap = session.snapshot
    return {
        "balance": str(snap.balance) if snap else None,
        "equity": str(snap.equity) if snap else None,
        "limits": [lv.to_dict() for lv in a.limits],
        "open_risk": str(a.open_risk),
        "positions": [{"id": p.position_id, "symbol": p.symbol, "side": p.side.value, "lots": str(p.lots),
                       "entry": str(p.entry_price), "sl": None if p.stop_loss is None else str(p.stop_loss),
                       "upnl": None if p.unrealized_pnl is None else f"{p.unrealized_pnl:.2f}",
                       "system": p.client_order_id is not None} for p in (snap.positions if snap else ())],
        "pending_orders": [{"id": o.broker_order_id, "symbol": o.symbol, "side": o.side.value,
                            "lots": str(o.lots), "type": o.order_type.value, "price": str(o.price)}
                           for o in (snap.pending_orders if snap else ())],
        "kill_switches": [k.value for k in ctx.active_kill_switches],
        "reconciliation_ok": ctx.reconciliation_ok,
        "ruleset_id": ctx.ruleset.ruleset_id,
        "rules_verified_at": (ctx.ruleset.oldest_critical_verification().isoformat()
                              if ctx.ruleset.oldest_critical_verification() else None),
        "pending_rule_change": ctx.ruleset.pending_critical_change,
        "day_start_known": ctx.state.day_start_known if ctx.state else None,
        "trading_date": ctx.state.trading_date.isoformat() if ctx.state and ctx.state.trading_date else None,
        "trading_days": len(ctx.state.trading_days) if ctx.state else 0,
        "initial_balance": str(ctx.ruleset.initial_balance),
    }


def dec(v: Any) -> Decimal:
    return Decimal(str(v))
