from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from propguard.risk import limits as L
from propguard.risk.models import RiskState
from propguard.risk.state import new_state, update_state
from propguard.risk.tradingday import (
    friday_cutoff_utc, is_weekend_window, next_reset, previous_reset, trading_date,
)
from propguard.rules.types import MaxLoss
from tests.helpers import ruleset, snapshot

pytestmark = pytest.mark.risk
UTC = timezone.utc


def test_trading_date_midnight_prague_summer_and_winter():
    # 23:30 UTC in summer = 01:30 CEST next day
    assert trading_date(datetime(2026, 7, 1, 22, 30, tzinfo=UTC), "00:00", "Europe/Prague") == date(2026, 7, 2)
    assert trading_date(datetime(2026, 7, 1, 21, 59, tzinfo=UTC), "00:00", "Europe/Prague") == date(2026, 7, 1)
    # winter: CET = UTC+1
    assert trading_date(datetime(2026, 12, 1, 22, 59, tzinfo=UTC), "00:00", "Europe/Prague") == date(2026, 12, 1)
    assert trading_date(datetime(2026, 12, 1, 23, 0, tzinfo=UTC), "00:00", "Europe/Prague") == date(2026, 12, 2)


def test_next_reset_across_dst_transitions():
    # EU DST ends 2026-10-25 03:00 CEST -> 02:00 CET. Midnight resets: 24th 22:00Z, 25th 23:00Z
    r1 = next_reset(datetime(2026, 10, 24, 12, 0, tzinfo=UTC), "00:00", "Europe/Prague")
    assert r1 == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    r2 = next_reset(r1, "00:00", "Europe/Prague")
    assert r2 == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)  # 25-hour day
    # DST starts 2026-03-29: midnight 28th = 23:00Z, midnight 29th = 22:00Z (23-hour day)
    r3 = next_reset(datetime(2026, 3, 28, 12, 0, tzinfo=UTC), "00:00", "Europe/Prague")
    assert r3 == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert next_reset(r3, "00:00", "Europe/Prague") == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)


def test_new_york_17h_rollover_and_dst():
    # 17:00 NY: in summer (EDT, UTC-4) = 21:00Z; in winter (EST) = 22:00Z
    assert next_reset(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), "17:00", "America/New_York") == \
        datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
    assert next_reset(datetime(2026, 12, 1, 12, 0, tzinfo=UTC), "17:00", "America/New_York") == \
        datetime(2026, 12, 1, 22, 0, tzinfo=UTC)
    # session after 17:00 NY belongs to next day
    assert trading_date(datetime(2026, 7, 1, 21, 30, tzinfo=UTC), "17:00", "America/New_York") == date(2026, 7, 2)
    assert trading_date(datetime(2026, 7, 1, 20, 30, tzinfo=UTC), "17:00", "America/New_York") == date(2026, 7, 1)
    # US DST ends 2026-11-01
    r = previous_reset(datetime(2026, 11, 2, 12, 0, tzinfo=UTC), "17:00", "America/New_York")
    assert r == datetime(2026, 11, 1, 22, 0, tzinfo=UTC)


def test_reset_inside_dst_gap_is_resolved():
    # 02:30 local does not exist on 2026-03-29 in Prague -> resolved to post-transition instant
    r = next_reset(datetime(2026, 3, 28, 23, 30, tzinfo=UTC), "02:30", "Europe/Prague")
    assert r.tzinfo is not None and r > datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    assert r <= datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        trading_date(datetime(2026, 1, 1), "00:00", "UTC")


def test_weekend_window():
    fri_2130 = datetime(2026, 10, 2, 21, 30, tzinfo=UTC)
    assert is_weekend_window(fri_2130, "21:00", "UTC")
    assert not is_weekend_window(datetime(2026, 10, 2, 20, 30, tzinfo=UTC), "21:00", "UTC")
    assert is_weekend_window(datetime(2026, 10, 4, 12, 0, tzinfo=UTC), "21:00", "UTC")
    assert not is_weekend_window(datetime(2026, 10, 4, 22, 30, tzinfo=UTC), "21:00", "UTC")
    assert friday_cutoff_utc(datetime(2026, 10, 3, 9, 0, tzinfo=UTC), "21:00", "UTC") == fri_2130 - timedelta(minutes=30)


T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)  # 10:00 Prague


def test_rollover_captures_day_start_and_eod_hwm():
    rs = ruleset()
    st = new_state("acc1", rs, snapshot(ts=T0, seq=0))
    r = update_state(st, snapshot(balance="101000", equity="100500", ts=T0 + timedelta(hours=5), seq=1), rs)
    assert r.accepted and not r.rolled_over
    assert r.state.hwm_balance == D("101000") and r.state.hwm_equity == D("100500")
    assert r.state.min_equity_today == D("100000")
    reset = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
    r2 = update_state(r.state, snapshot(balance="101000", equity="100800", ts=reset + timedelta(seconds=30), seq=2), rs)
    assert r2.rolled_over and r2.state.day_start_known
    assert r2.state.day_start_balance == D("101000") and r2.state.day_start_equity == D("100800")
    assert r2.state.trading_date == date(2026, 9, 30)
    assert r2.state.eod_hwm_balance == D("101000")


def test_missed_reset_marks_day_start_unknown_unless_reconstructed():
    rs = ruleset()
    st = new_state("acc1", rs, snapshot(ts=T0, seq=0))
    late = datetime(2026, 9, 29, 22, 0, tzinfo=UTC) + timedelta(hours=3)  # app restarted 3h after reset
    r = update_state(st, snapshot(ts=late, seq=5), rs)
    assert r.accepted and not r.state.day_start_known and "day_start_unknown" in r.events
    r2 = update_state(st, snapshot(ts=late, seq=5), rs, day_start_override=(D("100000"), D("100000")))
    assert r2.state.day_start_known and "day_start_reconstructed" in r2.events


def test_out_of_order_and_duplicate_snapshots_rejected():
    rs = ruleset()
    st = new_state("acc1", rs, snapshot(ts=T0, seq=0))
    r = update_state(st, snapshot(balance="99000", ts=T0 + timedelta(minutes=2), seq=3), rs)
    assert r.accepted
    dup = update_state(r.state, snapshot(balance="99000", ts=T0 + timedelta(minutes=2), seq=3), rs)
    assert not dup.accepted
    older = update_state(r.state, snapshot(balance="100500", ts=T0 + timedelta(minutes=1), seq=4), rs)
    assert not older.accepted and older.state.hwm_balance == r.state.hwm_balance


def test_state_roundtrip_for_restart():
    rs = ruleset()
    st = new_state("acc1", rs, snapshot(ts=T0, seq=0))
    st = update_state(st, snapshot(balance="100700", ts=T0 + timedelta(minutes=5), seq=1), rs).state
    st.trading_days.add("2026-09-29")
    st.daily_closed_pnl["2026-09-29"] = D("700")
    restored = RiskState.from_dict(st.to_dict())
    assert restored == st


def _state(initial="100000", hwm_eq="100000", hwm_bal="100000", eod_eq="100000", eod_bal="100000"):
    return RiskState(account_id="a", initial_balance=D(initial), hwm_equity=D(hwm_eq), hwm_balance=D(hwm_bal),
                     eod_hwm_equity=D(eod_eq), eod_hwm_balance=D(eod_bal))


def test_trailing_modes():
    st = _state(hwm_eq="104000", eod_eq="103000")
    snap = snapshot(balance="102000", equity="102500")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="static"), st, snap)[0] == D("95000")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="trailing"), st, snap)[0] == D("99000")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="eod_trailing"), st, snap)[0] == D("98000")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="trailing_lock_at_initial"), st, snap)[0] == D("99000")
    st2 = _state(hwm_eq="110000")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="trailing_lock_at_initial"), st2, snap)[0] == D("100000")
    # current equity above stored HWM is included immediately (intraday trailing)
    snap_hi = snapshot(balance="105000", equity="106000")
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="trailing"), st, snap_hi)[0] == D("101000")
    # balance basis ignores floating
    assert L.max_loss_floor(MaxLoss(pct=D(5), mode="trailing", basis="balance"),
                            _state(hwm_bal="101000"), snap_hi)[0] == D("100000")


def test_daily_reference_variants():
    from propguard.rules.types import DailyLossLimit
    st = RiskState(account_id="a", initial_balance=D("100000"), day_start_balance=D("102000"),
                   day_start_equity=D("101000"))
    f = lambda **kw: L.daily_floor(DailyLossLimit(pct=D(5), **kw), st)[0]  # noqa: E731
    assert f(reference="initial_balance") == D("95000")
    assert f(reference="day_start_balance") == D("97000")
    assert f(reference="day_start_equity") == D("96000")
    assert f(reference="day_start_max_balance_equity") == D("97000")
    assert f(reference="day_start_balance", pct_of="reference") == D("96900")


def test_equity_vs_balance_measurement_uses_conservative_value():
    rs = ruleset(daily_loss_limit={"pct": 5, "includes_floating": False, "reset_tz": "Europe/Prague"})
    st = new_state("acc1", rs, snapshot(ts=T0, seq=0))
    from propguard.risk.policy import SafetyPolicy
    views = L.limit_views(rs, st, snapshot(balance="100000", equity="96000"), SafetyPolicy(), D("0.3"))
    daily = next(v for v in views if v.name == "daily_loss")
    # balance-based rule, but floating loss counts for headroom (it realises on close)
    assert daily.current_value == D("100000")
    assert daily.external_headroom == D("1000")
