"""End-to-end simulated challenge: simulated market + broker + (demo) strategy + full risk envelope.

Rules that are not yet owner-verified would (correctly) block all trading. For *simulation only*,
``assume_rules_confirmed=True`` treats them as confirmed and fresh; the report states this
prominently. This flag does not exist on any live path.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from propguard.execution.session import AccountSession, NewsCalendar
from propguard.execution.simulator import SimClock, SimConfig, SimulatedBroker, SimulatedMarket
from propguard.execution.stores import Stores
from propguard.risk.models import InstrumentSpec
from propguard.risk.policy import SafetyPolicy
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import InterpretationStatus, ProfitTarget
from propguard.strategies.demo import DemoSMACrossover

DEFAULT_INSTRUMENTS = {
    "EURUSD": InstrumentSpec(symbol="EURUSD", asset_class="fx", contract_size=Decimal("100000"),
                             commission_per_lot_round_turn=Decimal("7"), base_ccy="EUR", quote_ccy="USD"),
}


@dataclass
class SimReport:
    ruleset_id: str
    assumed_rules_confirmed: bool
    days: int
    bars: int
    final_balance: str
    final_equity: str
    outcome: str
    decisions: dict[str, int] = field(default_factory=dict)
    kill_switches: list[str] = field(default_factory=list)
    external_breaches: list[str] = field(default_factory=list)
    min_internal_headroom: dict[str, str] = field(default_factory=dict)
    orders_sent: int = 0
    audit_entries: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _simulation_ruleset(rs: RuleSet, now: datetime, assume: bool) -> RuleSet:
    if not assume:
        return rs
    rules = tuple(replace(r, status=InterpretationStatus.CONFIRMED, verified_at=now,
                          confidence=max(r.confidence, Decimal("0.95"))) for r in rs.rules)
    return replace(rs, rules=rules, pending_critical_change=False)


def run_simulated_challenge(rs: RuleSet, *, days: int = 20, seed: int = 1, bar_minutes: int = 15,
                            start: datetime | None = None, assume_rules_confirmed: bool = False,
                            policy: SafetyPolicy | None = None, stores: Stores | None = None,
                            daily_vol: float = 0.006) -> SimReport:
    start = start or datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # a Monday
    clock = SimClock(start)
    market = SimulatedMarket(clock, DEFAULT_INSTRUMENTS)
    rng = random.Random(seed)
    price = 1.1000
    market.set_price("EURUSD", f"{price:.5f}", spread="0.00010")
    broker = SimulatedBroker("sim-acc", market, rs.initial_balance, SimConfig(seed=seed))
    stores = stores or Stores.memory()

    def rules() -> RuleSet:
        return _simulation_ruleset(rs, clock.now() - timedelta(minutes=1), assume_rules_confirmed)

    session = AccountSession("sim-acc", broker, market, rules, stores, clock.now, policy=policy,
                             news=lambda: NewsCalendar((), True))
    strat = DemoSMACrossover("EURUSD")
    session.start()
    report = SimReport(rs.ruleset_id, assume_rules_confirmed, days, 0, "0", "0", "NOT_COMPLETED")
    if assume_rules_confirmed:
        report.notes.append("SIMULATION ASSUMES UNVERIFIED RULES ARE CONFIRMED -- not valid for live decisions")
    report.notes.append("strategy: DemoSMACrossover (DEMONSTRATION ONLY, no claimed edge)")
    bars_per_day = int(24 * 60 / bar_minutes)
    sigma = daily_vol / math.sqrt(bars_per_day)
    min_head: dict[str, Decimal] = {}
    pt, _ = rs.get(ProfitTarget)
    end = start + timedelta(days=days)
    while clock.now() < end:
        clock.advance(minutes=bar_minutes)
        now = clock.now()
        closed = (now.weekday() == 5 or (now.weekday() == 4 and now.hour >= 21)
                  or (now.weekday() == 6 and now.hour < 22))
        market.set_closed(closed)
        if not closed:
            price *= math.exp(rng.gauss(0, sigma))
            market.set_price("EURUSD", f"{price:.5f}", spread="0.00010")
            broker.on_price_update()
        tick = session.tick()  # the service keeps running while markets are closed (captures resets)
        if closed:
            continue
        report.bars += 1
        for k in tick.new_kill_switches:
            report.kill_switches.append(f"{now.isoformat()} {k}")
        snap = session.snapshot
        if snap is None:
            continue
        a = session.risk.assess(session.context())
        for lv in a.limits:
            min_head[lv.name] = min(min_head.get(lv.name, lv.internal_headroom), lv.internal_headroom)
            if lv.external_headroom <= 0:
                report.external_breaches.append(f"{now.isoformat()} {lv.name}")
        for sig in strat.on_bar(now, market, snap):
            session.submit_signal(sig)
    decisions = Counter()
    audit = stores.audit
    entries = getattr(audit, "entries", [])
    for e in entries:
        if e["kind"] == "risk.decision":
            decisions[e["payload"]["decision"]["reasons"][0]] += 1
    snap = broker.get_snapshot()
    report.decisions = dict(decisions)
    report.final_balance = f"{snap.balance:.2f}"
    report.final_equity = f"{snap.equity:.2f}"
    report.orders_sent = broker.submit_count
    report.audit_entries = len(entries)
    report.min_internal_headroom = {k: f"{v:.2f}" for k, v in min_head.items()}
    if report.external_breaches:
        report.outcome = "FAILED"
    elif pt and snap.balance >= rs.initial_balance * (1 + pt.pct / 100):
        report.outcome = "TARGET_REACHED"
    return report
