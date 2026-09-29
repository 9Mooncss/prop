"""Monte Carlo / stress simulation of a strategy against challenge limits.

Bootstraps *days* from the replayed history (daily net PnL + that day's worst intraday excursion),
so intraday risk and day-level clustering are preserved. This is an estimate of proximity to hard
limits -- NOT a guarantee of passing a challenge.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from statistics import median

from propguard.recommender.replay import ReplayResult
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import DailyLossLimit, MaxDuration, MaxLoss, MinTradingDays, ProfitTarget

MC_VERSION = "montecarlo/1.0.0"
DISCLAIMER = ("Simulation based on resampled historical days. Past behaviour does not guarantee future "
              "results; this is not a probability of passing and not financial advice.")


@dataclass
class MCResult:
    paths: int
    pass_rate: float
    breach_rate: float
    timeout_rate: float
    median_days_to_pass: float | None
    p05_min_headroom_frac: float  # 5th percentile of the minimum distance to any floor (fraction of allowance)
    stress_loss_multiplier: float
    seed: int
    disclaimer: str = DISCLAIMER
    version: str = MC_VERSION

    def to_dict(self):
        return asdict(self)


def simulate(replay_res: ReplayResult, rs: RuleSet, paths: int = 2000, seed: int = 42,
             stress_loss_multiplier: float = 1.0, max_days_if_unlimited: int = 60) -> MCResult | None:
    days = [(float(replay_res.daily_pnl.get(k, "0")), float(v)) for k, v in replay_res.daily_worst.items()]
    if len(days) < 5:
        return None
    initial = float(rs.initial_balance)
    dl, _ = rs.get(DailyLossLimit)
    ml, _ = rs.get(MaxLoss)
    pt, _ = rs.get(ProfitTarget)
    mtd, _ = rs.get(MinTradingDays)
    md, _ = rs.get(MaxDuration)
    daily_allow = initial * float(dl.pct) / 100 if dl and dl.enabled else None
    max_allow = initial * float(ml.pct) / 100 if ml else initial
    target = initial * (1 + float(pt.pct) / 100) if pt else None
    min_days = mtd.days if mtd else 0
    horizon = md.days if md and md.days else max_days_if_unlimited
    trailing = ml is not None and ml.mode != "static"
    rng = random.Random(seed)
    passed = breached = timeout = 0
    days_to_pass, min_heads = [], []
    for _ in range(paths):
        bal, hwm, min_head = initial, initial, 1.0
        result = "timeout"
        for day in range(1, horizon + 1):
            pnl, worst = rng.choice(days)
            if pnl < 0:
                pnl *= stress_loss_multiplier
            worst = min(worst, 0.0) * stress_loss_multiplier
            if daily_allow:
                h = (daily_allow + worst) / daily_allow
                min_head = min(min_head, h)
                if h <= 0:
                    result = "breach"
                    break
            floor = (hwm - max_allow) if trailing else (initial - max_allow)
            h = ((bal + worst) - floor) / max_allow
            min_head = min(min_head, h)
            if h <= 0:
                result = "breach"
                break
            bal += pnl
            hwm = max(hwm, bal)
            if (bal - floor) / max_allow <= 0:
                result = "breach"
                break
            if target and bal >= target and day >= min_days:
                result = "pass"
                days_to_pass.append(day)
                break
        passed += result == "pass"
        breached += result == "breach"
        timeout += result == "timeout"
        min_heads.append(min_head)
    min_heads.sort()
    return MCResult(paths, passed / paths, breached / paths, timeout / paths,
                    median(days_to_pass) if days_to_pass else None,
                    round(min_heads[int(0.05 * len(min_heads))], 4), stress_loss_multiplier, seed)
