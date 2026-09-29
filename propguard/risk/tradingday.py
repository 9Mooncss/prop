"""Trading-day arithmetic in the prop firm's timezone (DST-aware via zoneinfo)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse_hhmm(v: str) -> time:
    hh, mm = v.split(":")
    return time(int(hh), int(mm))


def ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError("naive datetime not allowed in risk calculations")
    return ts


def trading_date(ts: datetime, reset_time: str, tz: str) -> date:
    """Trading date that ``ts`` belongs to, where the day starts at ``reset_time`` local time.

    Example: reset 17:00 America/New_York -> Tuesday 18:00 NY belongs to Wednesday's session?
    No: we define the trading date as the local calendar date of the *start* of the session, shifted
    so that a reset at 00:00 gives the plain local date. For resets after midnight-local (e.g. 17:00),
    times at/after the reset belong to the *next* calendar day's session, matching the common
    "day rolls at 5pm New York" convention.
    """
    ensure_aware(ts)
    local = ts.astimezone(ZoneInfo(tz))
    r = _parse_hhmm(reset_time)
    if r == time(0, 0):
        return local.date()
    # sessions start at reset time; label a session by the calendar date on which it ENDS
    if local.time() >= r:
        return local.date() + timedelta(days=1)
    return local.date()


def next_reset(ts: datetime, reset_time: str, tz: str) -> datetime:
    """UTC datetime of the next daily reset strictly after ``ts``."""
    ensure_aware(ts)
    zi = ZoneInfo(tz)
    local = ts.astimezone(zi)
    r = _parse_hhmm(reset_time)
    candidate_date = local.date()
    for _ in range(3):
        cand = _local_to_utc(candidate_date, r, zi)
        if cand > ts:
            return cand
        candidate_date += timedelta(days=1)
    raise RuntimeError("unreachable")  # pragma: no cover


def previous_reset(ts: datetime, reset_time: str, tz: str) -> datetime:
    """UTC datetime of the most recent reset at or before ``ts``."""
    ensure_aware(ts)
    zi = ZoneInfo(tz)
    local = ts.astimezone(zi)
    r = _parse_hhmm(reset_time)
    d = local.date()
    for _ in range(3):
        cand = _local_to_utc(d, r, zi)
        if cand <= ts:
            return cand
        d -= timedelta(days=1)
    raise RuntimeError("unreachable")  # pragma: no cover


def _local_to_utc(d: date, t: time, zi: ZoneInfo) -> datetime:
    """Resolve local wall time to UTC, handling DST gaps (shift forward) and folds (earliest)."""
    naive = datetime.combine(d, t)
    aware = naive.replace(tzinfo=zi, fold=0)
    # DST gap: wall time doesn't exist; round-trip changes it -> use the post-transition instant
    roundtrip = aware.astimezone(timezone.utc).astimezone(zi)
    if roundtrip.replace(tzinfo=None) != naive:
        aware = naive.replace(tzinfo=zi, fold=1)
    return aware.astimezone(timezone.utc)


def minutes_to_next_reset(ts: datetime, reset_time: str, tz: str) -> float:
    return (next_reset(ts, reset_time, tz) - ts).total_seconds() / 60.0


def minutes_since_reset(ts: datetime, reset_time: str, tz: str) -> float:
    return (ts - previous_reset(ts, reset_time, tz)).total_seconds() / 60.0


def local_weekday_time(ts: datetime, tz: str) -> tuple[int, time]:
    local = ensure_aware(ts).astimezone(ZoneInfo(tz))
    return local.weekday(), local.time()


def friday_cutoff_utc(ts: datetime, close_by: str, tz: str) -> datetime:
    """UTC time of this week's Friday cutoff (or the one just passed if it's the weekend)."""
    zi = ZoneInfo(tz)
    local = ensure_aware(ts).astimezone(zi)
    days_to_friday = (4 - local.weekday()) % 7
    if local.weekday() in (5, 6):  # weekend -> last Friday
        days_to_friday = -((local.weekday() - 4) % 7)
    friday = local.date() + timedelta(days=days_to_friday)
    return _local_to_utc(friday, _parse_hhmm(close_by), zi)


def is_weekend_window(ts: datetime, close_by: str, tz: str, reopen_weekday: int = 6,
                      reopen_time: str = "22:00") -> bool:
    """True between Friday cutoff and the weekly reopen (default Sunday 22:00 in tz)."""
    zi = ZoneInfo(tz)
    local = ensure_aware(ts).astimezone(zi)
    cutoff = friday_cutoff_utc(ts, close_by, tz)
    # reopen = next occurrence of reopen weekday/time after cutoff
    cutoff_local = cutoff.astimezone(zi)
    days = (reopen_weekday - cutoff_local.weekday()) % 7
    reopen = _local_to_utc(cutoff_local.date() + timedelta(days=days), _parse_hhmm(reopen_time), zi)
    return cutoff <= local.astimezone(timezone.utc) < reopen
