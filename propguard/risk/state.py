"""Deterministic account-state tracking: trading-day rollover, high-water marks, breach detection."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from propguard.risk.models import AccountSnapshot, RiskState
from propguard.risk.tradingday import ensure_aware, previous_reset, trading_date
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import DailyLossLimit, TradingDay

RESET_CAPTURE_WINDOW = timedelta(seconds=120)


def reset_spec(ruleset: RuleSet) -> tuple[str, str]:
    """(reset_time, tz) used for the trading day. Daily-loss reset wins over generic trading_day."""
    dl, _ = ruleset.get(DailyLossLimit)
    if dl is not None:
        return dl.reset_time, dl.reset_tz
    td, _ = ruleset.get(TradingDay)
    if td is not None:
        return td.rollover_time, td.tz
    return "00:00", "UTC"


@dataclass
class UpdateResult:
    state: RiskState
    accepted: bool
    rolled_over: bool = False
    events: list[str] = field(default_factory=list)


def new_state(account_id: str, ruleset: RuleSet, snapshot: AccountSnapshot) -> RiskState:
    """Initial state for a fresh challenge. The first snapshot must be at the challenge start."""
    rt, tz = reset_spec(ruleset)
    return RiskState(
        account_id=account_id,
        initial_balance=ruleset.initial_balance,
        trading_date=trading_date(snapshot.ts, rt, tz),
        day_start_balance=snapshot.balance,
        day_start_equity=snapshot.equity,
        day_start_known=True,
        hwm_balance=max(snapshot.balance, ruleset.initial_balance),
        hwm_equity=max(snapshot.equity, ruleset.initial_balance),
        eod_hwm_balance=max(snapshot.balance, ruleset.initial_balance),
        eod_hwm_equity=max(snapshot.equity, ruleset.initial_balance),
        min_equity_today=snapshot.equity,
        last_snapshot_ts=snapshot.ts,
        last_sequence=snapshot.sequence,
        started_at=snapshot.ts,
    )


def update_state(
    state: RiskState,
    snapshot: AccountSnapshot,
    ruleset: RuleSet,
    *,
    day_start_override: tuple[Decimal, Decimal] | None = None,
) -> UpdateResult:
    """Apply a snapshot. Pure: returns a new state; the input is not mutated.

    * Rejects out-of-order / duplicate snapshots (sequence or timestamp going backwards).
    * On trading-day rollover captures the day-start reference. If the first post-reset snapshot
      arrives later than ``RESET_CAPTURE_WINDOW`` after the reset (e.g. the app was down), the
      reference is marked *unknown* unless ``day_start_override`` (reconstructed by the platform
      adapter from deal history) is supplied.
    """
    ensure_aware(snapshot.ts)
    s = copy.deepcopy(state)
    if snapshot.sequence <= s.last_sequence and snapshot.sequence != 0:
        return UpdateResult(state, False, events=["out_of_order_sequence"])
    if s.last_snapshot_ts is not None and snapshot.ts < s.last_snapshot_ts:
        return UpdateResult(state, False, events=["out_of_order_timestamp"])

    rt, tz = reset_spec(ruleset)
    td = trading_date(snapshot.ts, rt, tz)
    rolled = False
    events: list[str] = []
    if s.trading_date is None or td != s.trading_date:
        rolled = s.trading_date is not None
        # end-of-day HWM uses the last known state *before* the reset (conservative: the current
        # balance is only used if captured within the capture window)
        reset_at = previous_reset(snapshot.ts, rt, tz)
        captured_promptly = (snapshot.ts - reset_at) <= RESET_CAPTURE_WINDOW
        gap_days = (td - s.trading_date).days if s.trading_date else 0
        if day_start_override is not None:
            s.day_start_balance, s.day_start_equity = day_start_override
            s.day_start_known = True
            events.append("day_start_reconstructed")
        elif captured_promptly and gap_days <= 1:
            s.day_start_balance, s.day_start_equity = snapshot.balance, snapshot.equity
            s.day_start_known = True
        else:
            # missed the reset: keep the best available estimate but flag it as unknown
            s.day_start_balance, s.day_start_equity = snapshot.balance, snapshot.equity
            s.day_start_known = False
            events.append("day_start_unknown")
        if s.day_start_balance is not None:
            s.eod_hwm_balance = max(s.eod_hwm_balance or s.day_start_balance, s.day_start_balance)
        if s.day_start_equity is not None:
            s.eod_hwm_equity = max(s.eod_hwm_equity or s.day_start_equity, s.day_start_equity)
        s.trading_date = td
        s.min_equity_today = snapshot.equity
        if rolled:
            events.append("rollover")

    s.hwm_balance = max(s.hwm_balance or snapshot.balance, snapshot.balance)
    s.hwm_equity = max(s.hwm_equity or snapshot.equity, snapshot.equity)
    s.min_equity_today = min(s.min_equity_today or snapshot.equity, snapshot.equity)
    for p in snapshot.positions:
        ptd = trading_date(p.opened_at, rt, tz)
        if ptd == td:
            s.trading_days.add(td.isoformat())
    s.last_snapshot_ts = snapshot.ts
    s.last_sequence = max(s.last_sequence, snapshot.sequence)
    return UpdateResult(s, True, rolled_over=rolled, events=events)


def record_activity(state: RiskState, ts: datetime, ruleset: RuleSet) -> RiskState:
    s = copy.deepcopy(state)
    rt, tz = reset_spec(ruleset)
    s.trading_days.add(trading_date(ts, rt, tz).isoformat())
    s.last_activity_ts = ts
    return s


def record_closed_pnl(state: RiskState, ts: datetime, pnl: Decimal, ruleset: RuleSet) -> RiskState:
    s = copy.deepcopy(state)
    rt, tz = reset_spec(ruleset)
    key = trading_date(ts, rt, tz).isoformat()
    s.daily_closed_pnl[key] = s.daily_closed_pnl.get(key, Decimal("0")) + pnl
    return s
