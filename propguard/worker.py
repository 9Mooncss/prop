"""Background worker: rule-source monitoring + freshness alerts + heartbeat for health checks."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from propguard.app_context import AppContext
from propguard.db.models import Alert, TradingAccount
from propguard.db.session import session_scope
from propguard.monitor.pipeline import run_monitor
from propguard.registry.service import raise_alert, rules_freshness_report, ruleset_for

log = logging.getLogger("propguard.worker")
_stop = False


def _handle(_sig, _frm):  # pragma: no cover
    global _stop
    _stop = True


def heartbeat(ctx: AppContext) -> None:
    p = ctx.settings.data_dir / "worker.heartbeat"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(datetime.now(timezone.utc).isoformat())


def freshness_check(ctx: AppContext) -> int:
    """Alert when an active account's critical rules are stale / unconfirmed (new risk is already
    blocked by the Risk Engine; this makes it visible before a session)."""
    n = 0
    with session_scope(ctx.sf) as s:
        for a in s.scalars(select(TradingAccount).where(TradingAccount.status == "ACTIVE")):
            rs = ruleset_for(s, a.firm_slug, a.program_slug, a.phase, a.initial_balance)
            oldest = rs.oldest_critical_verification()
            stale = oldest is None or datetime.now(timezone.utc) - oldest > timedelta(hours=ctx.settings.rules_max_age_h)
            if stale or rs.non_confirmed_critical() or rs.pending_critical_change:
                recent = s.scalar(select(Alert).where(Alert.kind == "rules_not_fresh", Alert.account_id == a.id,
                                                      Alert.created_at > datetime.now(timezone.utc) - timedelta(hours=6)))
                if recent is None:
                    raise_alert(s, "HIGH", "rules_not_fresh",
                                f"{a.id}: critical rules not fresh/confirmed -> new risk blocked (fail closed)",
                                account_id=a.id, payload={"unconfirmed": [r.kind for r in rs.non_confirmed_critical()],
                                                          "pending_change": rs.pending_critical_change})
                    ctx.notifier.send("HIGH", "Rules not fresh", f"{a.id}: new risk blocked until rules verified")
                    n += 1
        _ = rules_freshness_report(s, ctx.settings.rules_max_age_h)
    return n


def run_forever(ctx: AppContext, once: bool = False) -> None:
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    fetcher = ctx.fetcher()
    last_monitor = 0.0
    while not _stop:
        heartbeat(ctx)
        now = time.monotonic()
        if once or now - last_monitor >= ctx.settings.monitor_interval_s:
            try:
                with session_scope(ctx.sf) as s:
                    outs = run_monitor(s, fetcher, ctx.analyzer(s), ctx.notifier,
                                       due_after_s=ctx.settings.monitor_interval_s)
                log.info("monitor pass", extra={"ctx_checked": len(outs),
                                                "ctx_changed": sum(o.result == "CHANGED" for o in outs)})
            except Exception:  # noqa: BLE001 -- keep worker alive, error is logged (redacted)
                log.exception("monitor pass failed")
            last_monitor = now
            try:
                freshness_check(ctx)
            except Exception:  # noqa: BLE001
                log.exception("freshness check failed")
        if once:
            return
        time.sleep(15)
