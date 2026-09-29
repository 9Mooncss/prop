"""Replay a closed-trade history through a challenge RuleSet (deterministic).

Model (documented, conservative):
* PnL scaled by target_account / history_account.
* Intra-trade floating loss: if the history provides MAE (max adverse excursion, money), all
  simultaneously open trades are assumed to hit their MAE at the same time (worst case). Without
  MAE the replay can only check realised PnL and says so in ``assumptions``.
* Day-start reference = balance at the first event of the trading day (equity at reset unknown).
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from propguard.risk.tradingday import is_weekend_window, trading_date
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import (
    ConsistencyRule,
    DailyLossLimit,
    HFTRestriction,
    MaxDuration,
    MaxLoss,
    MaxLotSize,
    MinTradingDays,
    OvernightHolding,
    ProfitTarget,
    Tri,
    WeekendHolding,
)

REPLAY_VERSION = "replay/1.0.0"
Z = Decimal("0")


@dataclass(frozen=True)
class Trade:
    trade_id: str
    symbol: str
    side: str
    lots: Decimal
    open_time: datetime
    close_time: datetime
    pnl: Decimal  # gross, account ccy
    commission: Decimal = Z
    swap: Decimal = Z
    mae: Decimal | None = None  # magnitude (positive number) of worst floating loss

    @property
    def net(self) -> Decimal:
        return self.pnl + self.commission + self.swap


def _dt(v: str) -> datetime:
    d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_trades_csv(text: str) -> list[Trade]:
    """Columns: trade_id,symbol,side,lots,open_time,close_time,pnl[,commission,swap,mae]. Times ISO-8601
    (UTC if no offset). Commission/swap as signed amounts (costs negative)."""
    rows = csv.DictReader(io.StringIO(text))
    out = []
    for i, r in enumerate(rows):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        mae = r.get("mae")
        out.append(Trade(
            trade_id=r.get("trade_id") or str(i + 1), symbol=r["symbol"], side=r["side"].upper(),
            lots=Decimal(r.get("lots") or "0"), open_time=_dt(r["open_time"]), close_time=_dt(r["close_time"]),
            pnl=Decimal(r["pnl"]), commission=Decimal(r.get("commission") or "0"),
            swap=Decimal(r.get("swap") or "0"), mae=abs(Decimal(mae)) if mae else None))
    for t in out:
        if t.close_time < t.open_time:
            raise ValueError(f"trade {t.trade_id}: close before open")
    return sorted(out, key=lambda t: (t.open_time, t.trade_id))


@dataclass
class Violation:
    rule: str
    rule_id: str | None
    ts: str
    trading_date: str
    trade_id: str | None
    magnitude: str  # how far beyond the limit (money or units)
    detail: str


@dataclass
class Approach:
    limit: str
    min_headroom: str  # money
    min_headroom_frac: str  # of allowance
    at: str | None
    trading_date: str | None


@dataclass
class ReplayResult:
    ruleset_id: str
    outcome: str  # PASSED | FAILED | NOT_COMPLETED
    failed_at: str | None
    violations: list[Violation] = field(default_factory=list)
    approaches: list[Approach] = field(default_factory=list)
    target_reached_at: str | None = None
    trading_days: int = 0
    days_elapsed: int = 0
    final_balance: str = "0"
    max_drawdown_frac: str = "0"
    daily_pnl: dict[str, str] = field(default_factory=dict)
    daily_worst: dict[str, str] = field(default_factory=dict)  # worst intraday loss vs day start
    assumptions: list[str] = field(default_factory=list)
    version: str = REPLAY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def replay(trades: list[Trade], rs: RuleSet, history_account_size: Decimal | float | str) -> ReplayResult:
    initial = rs.initial_balance
    scale = initial / Decimal(str(history_account_size))
    dl, dl_rec = rs.get(DailyLossLimit)
    ml, ml_rec = rs.get(MaxLoss)
    pt, pt_rec = rs.get(ProfitTarget)
    mtd, _ = rs.get(MinTradingDays)
    md, md_rec = rs.get(MaxDuration)
    wk, wk_rec = rs.get(WeekendHolding)
    on, on_rec = rs.get(OvernightHolding)
    cons, cons_rec = rs.get(ConsistencyRule)
    mls, mls_rec = rs.get(MaxLotSize)
    hft, hft_rec = rs.get(HFTRestriction)
    rt, tz = (dl.reset_time, dl.reset_tz) if dl else ("00:00", "UTC")
    res = ReplayResult(rs.ruleset_id, "NOT_COMPLETED", None)
    if not trades:
        res.assumptions.append("empty history")
        return res
    has_mae = all(t.mae is not None for t in trades)
    if not has_mae:
        res.assumptions.append("no MAE column: intraday floating drawdown not modelled (optimistic)")
    res.assumptions.append("simultaneous open trades assumed to reach MAE together (conservative)")
    res.assumptions.append("day-start reference approximated by balance at first event of the day")
    res.assumptions.append(f"PnL scaled x{scale:.4f} to account size {initial}")

    # events: (ts, order, kind, trade)
    events = []
    for t in trades:
        events.append((t.open_time, 0, "open", t))
        events.append((t.close_time, 1, "close", t))
    events.sort(key=lambda e: (e[0], e[1]))
    balance = initial
    hwm = initial
    eod_hwm = initial
    open_trades: dict[str, Trade] = {}
    cur_date = None
    day_start = initial
    days_with_trades: set[str] = set()
    daily_pnl: dict[str, Decimal] = {}
    daily_worst: dict[str, Decimal] = {}
    min_head = {"daily_loss": (None, None, None), "max_loss": (None, None, None)}
    max_dd = Z
    start = trades[0].open_time

    def record_violation(rule, rid, ts, d, tid, mag, detail):
        res.violations.append(Violation(rule, rid, ts.isoformat(), d, tid, str(mag), detail))
        if res.failed_at is None:
            res.failed_at = ts.isoformat()

    for ts, _, kind, t in events:
        d = trading_date(ts, rt, tz)
        dkey = d.isoformat()
        if cur_date != d:
            if cur_date is not None:
                eod_hwm = max(eod_hwm, balance)
            cur_date, day_start = d, balance
        if kind == "open":
            open_trades[t.trade_id] = t
            days_with_trades.add(dkey)
            lots = t.lots * scale
            if mls is not None and lots > mls.max_lots:
                record_violation("max_lot_size", mls_rec.rule_id, ts, dkey, t.trade_id, lots - mls.max_lots,
                                 f"scaled lots {lots:.2f} > {mls.max_lots}")
        else:
            open_trades.pop(t.trade_id, None)
            net = t.net * scale
            balance += net
            daily_pnl[dkey] = daily_pnl.get(dkey, Z) + net
            hold = t.close_time - t.open_time
            if wk is not None and wk.allowed == Tri.PROHIBITED and _spans_weekend(t, wk.close_by, wk.tz):
                record_violation("weekend_holding", wk_rec.rule_id, ts, dkey, t.trade_id, "1",
                                 "position held over weekend")
            if on is not None and on.allowed == Tri.PROHIBITED and trading_date(t.open_time, rt, tz) != d:
                record_violation("overnight_holding", on_rec.rule_id, ts, dkey, t.trade_id, "1",
                                 "position held across daily rollover")
            if hft is not None and hft.min_hold_seconds and hold.total_seconds() < hft.min_hold_seconds:
                record_violation("hft_restriction", hft_rec.rule_id, ts, dkey, t.trade_id,
                                 str(hft.min_hold_seconds - int(hold.total_seconds())),
                                 f"held {hold.total_seconds():.0f}s < {hft.min_hold_seconds}s")
        floating_worst = sum(((ot.mae or Z) * scale for ot in open_trades.values()), Z)
        worst_equity = balance - floating_worst
        hwm = max(hwm, balance)
        max_dd = max(max_dd, (hwm - worst_equity) / initial)
        daily_worst[dkey] = min(daily_worst.get(dkey, Z), worst_equity - day_start)
        # daily loss
        if dl is not None and dl.enabled:
            base = initial if dl.pct_of == "initial_balance" else day_start
            ref = initial if dl.reference == "initial_balance" else day_start
            allowance = base * dl.pct / 100
            floor = ref - allowance
            measured = worst_equity if dl.includes_floating else balance
            head = measured - floor
            _track(min_head, "daily_loss", head, allowance, ts, dkey)
            if head <= 0:
                record_violation("daily_loss_limit", dl_rec.rule_id, ts, dkey, t.trade_id, -head,
                                 f"value {measured:.2f} <= daily floor {floor:.2f}")
        if ml is not None:
            allowance = initial * ml.pct / 100
            if ml.mode == "static":
                floor = initial - allowance
            elif ml.mode == "eod_trailing":
                floor = eod_hwm - allowance
            elif ml.mode == "trailing_lock_at_initial":
                floor = min(hwm - allowance, initial)
            else:
                floor = hwm - allowance
            measured = worst_equity if ml.basis == "equity" else balance
            head = measured - floor
            _track(min_head, "max_loss", head, allowance, ts, dkey)
            if head <= 0:
                record_violation("max_loss", ml_rec.rule_id, ts, dkey, t.trade_id, -head,
                                 f"value {measured:.2f} <= max-loss floor {floor:.2f} ({ml.mode})")
        if pt is not None and res.target_reached_at is None and balance >= initial * (1 + pt.pct / 100):
            res.target_reached_at = ts.isoformat()

    end = trades[-1].close_time
    res.trading_days = len(days_with_trades)
    res.days_elapsed = (end.date() - start.date()).days + 1
    if md is not None and md.days is not None and res.target_reached_at:
        if _dt(res.target_reached_at) - start > timedelta(days=md.days):
            record_violation("max_duration", md_rec.rule_id, _dt(res.target_reached_at), "", None,
                             str((_dt(res.target_reached_at) - start).days - md.days), "target reached too late")
    if cons is not None and cons.max_single_day_pct_of_total is not None:
        total = sum((v for v in daily_pnl.values() if v > 0), Z)
        if total > 0:
            best_day, best = max(daily_pnl.items(), key=lambda kv: kv[1])
            share = best / total * 100
            if share > cons.max_single_day_pct_of_total:
                record_violation("consistency_rule", cons_rec.rule_id, end, best_day, None,
                                 f"{share - cons.max_single_day_pct_of_total:.2f}%",
                                 f"best day {share:.1f}% of profit > {cons.max_single_day_pct_of_total}%")
    for name, (h, frac, at) in min_head.items():
        if h is not None:
            res.approaches.append(Approach(name, f"{h[0]:.2f}", f"{frac:.4f}", at[0], at[1]))
    res.final_balance = f"{balance:.2f}"
    res.max_drawdown_frac = f"{max_dd:.4f}"
    res.daily_pnl = {k: f"{v:.2f}" for k, v in sorted(daily_pnl.items())}
    res.daily_worst = {k: f"{v:.2f}" for k, v in sorted(daily_worst.items())}
    min_days_ok = mtd is None or res.trading_days >= mtd.days
    if res.violations:
        res.outcome = "FAILED"
    elif res.target_reached_at and min_days_ok:
        res.outcome = "PASSED"
    else:
        res.outcome = "NOT_COMPLETED"
        if res.target_reached_at and not min_days_ok:
            res.assumptions.append(f"target reached but only {res.trading_days} trading days (< {mtd.days})")
    return res


def _track(store, name, head, allowance, ts, dkey):
    frac = head / allowance if allowance > 0 else Z
    cur = store[name]
    if cur[0] is None or head < cur[0][0]:
        store[name] = ((head,), frac, (ts.isoformat(), dkey))


def _spans_weekend(t: Trade, close_by: str, tz: str) -> bool:
    cur = t.open_time
    while cur < t.close_time:
        if is_weekend_window(cur, close_by, tz):
            return True
        cur += timedelta(hours=1)
    return is_weekend_window(t.close_time, close_by, tz) and t.close_time - t.open_time > timedelta(hours=1)
