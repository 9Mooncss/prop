from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from propguard.recommender.montecarlo import simulate
from propguard.recommender.replay import Trade, parse_trades_csv, replay
from tests.helpers import ruleset

pytestmark = pytest.mark.risk
UTC = timezone.utc


def T(i, day, hour, pnl, mae=None, hours=2, lots="1", start=datetime(2026, 9, 1, tzinfo=UTC)):
    o = start + timedelta(days=day, hours=hour)
    return Trade(str(i), "EURUSD", "BUY", D(lots), o, o + timedelta(hours=hours), D(pnl),
                 D("-7"), D("0"), None if mae is None else D(mae))


def test_csv_parsing_and_validation():
    csv = ("trade_id,symbol,side,lots,open_time,close_time,pnl,commission,swap,mae\n"
           "1,EURUSD,buy,1.0,2026-09-01T08:00:00Z,2026-09-01T10:00:00Z,250,-7,0,120\n")
    t = parse_trades_csv(csv)[0]
    assert t.side == "BUY" and t.mae == D("120") and t.net == D("243")
    with pytest.raises(ValueError):
        parse_trades_csv(csv.replace("2026-09-01T10:00:00Z", "2026-08-01T10:00:00Z"))


def test_replay_pass():
    trades = [T(i, i, 8, "1500", mae="300") for i in range(8)]  # Tue.. 8 winners
    r = replay(trades, ruleset(), 100000)
    assert r.outcome == "PASSED" and r.target_reached_at and not r.violations
    assert r.trading_days >= 4


def test_replay_daily_loss_violation_located_with_magnitude():
    trades = [T(1, 1, 8, "-2000"), T(2, 1, 11, "-2000"), T(3, 1, 14, "-1500")]
    r = replay(trades, ruleset(), 100000)
    assert r.outcome == "FAILED"
    v = r.violations[0]
    assert v.rule == "daily_loss_limit" and v.trade_id == "3" and D(v.magnitude) > 0
    assert v.trading_date == "2026-09-02"


def test_mae_counts_for_equity_based_daily_limit():
    trades = [T(1, 1, 8, "100", mae="5200")]
    assert replay(trades, ruleset(), 100000).violations[0].rule == "daily_loss_limit"
    rs_bal = ruleset(daily_loss_limit={"pct": 5, "includes_floating": False, "reset_tz": "Europe/Prague"})
    assert not [v for v in replay(trades, rs_bal, 100000).violations if v.rule == "daily_loss_limit"]


def test_scaling_to_account_size():
    trades = [T(1, 1, 8, "-3000")]  # -3% on a 100k history
    r = replay(trades, ruleset(initial=50000), 100000)  # scaled to -1500 on 50k
    assert r.final_balance == "48496.50"


def test_trailing_drawdown_replay():
    rs = ruleset(max_loss={"pct": 5, "mode": "trailing", "basis": "equity"},
                 daily_loss_limit={"pct": 5, "reset_tz": "Europe/Prague"})
    trades = [T(1, 1, 8, "4000"), T(2, 2, 8, "-2500"), T(3, 3, 8, "-2600")]
    r = replay(trades, rs, 100000)
    assert any(v.rule == "max_loss" for v in r.violations)  # HWM 103993 -> floor 98993
    static = ruleset(max_loss={"pct": 5, "mode": "static"})
    assert not any(v.rule == "max_loss" for v in replay(trades, static, 100000).violations)


def test_weekend_overnight_consistency_and_closest_approach():
    rs = ruleset(weekend_holding={"allowed": "PROHIBITED", "tz": "UTC"},
                 overnight_holding={"allowed": "PROHIBITED"},
                 consistency_rule={"max_single_day_pct_of_total": 30})
    fri = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
    trades = [Trade("w", "EURUSD", "BUY", D("1"), fri, fri + timedelta(days=3), D("500")),
              T("big", 10, 8, "5000"), T("s", 11, 8, "200")]
    r = replay(trades, rs, 100000)
    rules = {v.rule for v in r.violations}
    assert {"weekend_holding", "overnight_holding", "consistency_rule"} <= rules
    assert {a.limit for a in r.approaches} == {"daily_loss", "max_loss"}


def test_monte_carlo_is_deterministic_and_stress_increases_breaches():
    import random
    rnd = random.Random(5)
    trades = [T(i, i, 8, str(rnd.choice([900, -700, 400, -300, 1200, -1100])), mae="600") for i in range(40)]
    r = replay(trades, ruleset(), 100000)
    a = simulate(r, ruleset(), paths=500, seed=1)
    b = simulate(r, ruleset(), paths=500, seed=1)
    assert a == b and 0 <= a.pass_rate <= 1 and "not a probability of passing" in a.disclaimer
    s = simulate(r, ruleset(), paths=500, seed=1, stress_loss_multiplier=3.0)
    assert s.breach_rate >= a.breach_rate
    assert simulate(replay(trades[:3], ruleset(), 100000), ruleset()) is None  # too little data
