"""Property-based invariants of the Risk Engine."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from propguard.risk import limits as L
from propguard.risk.engine import RiskEngine
from propguard.risk.models import OrderIntent, Side
from propguard.risk.policy import Action, SafetyPolicy
from propguard.risk.state import new_state
from tests.helpers import EURUSD, NOW, ctx, order, position, quote, ruleset, snapshot

pytestmark = pytest.mark.risk
E = RiskEngine()

money = st.decimals(min_value=D("90000"), max_value=D("115000"), places=2)
lots = st.decimals(min_value=D("0.01"), max_value=D("20"), places=2)
pips = st.integers(min_value=5, max_value=500)
spreads = st.integers(min_value=0, max_value=30)
modes = st.sampled_from(["static", "trailing", "eod_trailing", "trailing_lock_at_initial"])
refs = st.sampled_from(["initial_balance", "day_start_balance", "day_start_equity", "day_start_max_balance_equity"])


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(balance=money, eq_delta=st.decimals(min_value=D("-3000"), max_value=D("3000"), places=2), req=lots,
       sl_pips=pips, spread=spreads, side=st.sampled_from([Side.BUY, Side.SELL]), mode=modes, ref=refs,
       dstart=money, has_pos=st.booleans())
def test_allowed_orders_never_breach_internal_floors_in_worst_case(balance, eq_delta, req, sl_pips, spread, side,
                                                                  mode, ref, dstart, has_pos):
    rs = ruleset(daily_loss_limit={"pct": 5, "reference": ref, "reset_tz": "Europe/Prague"},
                 max_loss={"pct": 10, "mode": mode})
    state = new_state("acc1", rs, snapshot(balance=str(dstart), ts=NOW - timedelta(hours=2), seq=0))
    equity = balance + eq_delta
    positions = [position(upnl=str(eq_delta))] if has_pos else []
    if not has_pos:
        equity = balance
    snap = snapshot(balance=str(balance), equity=str(equity), positions=positions)
    bid = D("1.1000")
    q = quote(bid=str(bid), ask=str(bid + D(spread) / D(100000)))
    entry = q.ask if side is Side.BUY else q.bid
    sl = entry - D(sl_pips) / D(10000) * side.sign
    c = ctx(rs=rs, snap=snap, state=state, quotes={"EURUSD": q})
    if has_pos:  # keep platform-reported unrealised consistent with our computation
        comp = L.computed_unrealized(positions[0], EURUSD, q)
        positions = [position(upnl=str(comp))]
        snap = snapshot(balance=str(balance), equity=str(balance + comp), positions=positions)
        c = ctx(rs=rs, snap=snap, state=state, quotes={"EURUSD": q})
    d = E.evaluate(order(str(req), side=side, sl=str(sl)), c)
    if d.allowed:
        assert d.approved_lots <= d.max_allowed_lots
        worst = snap.equity - d.open_risk - d.new_order_risk
        for lv in d.limits:
            assert worst >= lv.internal_floor, (lv, worst)
            assert lv.internal_floor > lv.external_floor or lv.allowance == 0
        # risk per trade cap respected
        assert d.new_order_risk <= state.initial_balance * SafetyPolicy().max_risk_per_trade_frac
    else:
        assert d.action == Action.DENY and d.approved_lots == 0


@settings(max_examples=200, deadline=None)
@given(pos_lots=lots, close_lots=lots, side=st.sampled_from([Side.BUY, Side.SELL]))
def test_reducing_orders_never_increase_exposure(pos_lots, close_lots, side):
    p = position(side=side, lots=str(pos_lots), sl=None)
    c = ctx(snap=snapshot(positions=[p]))
    o = order(str(close_lots), side=side.opposite(), sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    d = E.evaluate(o, c)
    assert d.allowed == (close_lots <= pos_lots)
    same = order(str(close_lots), side=side, sl=None, intent=OrderIntent.REDUCE, position_id="p1", coid="x")
    assert not E.evaluate(same, c).allowed


@settings(max_examples=200, deadline=None)
@given(base=st.decimals(min_value=D("0"), max_value=D("0.9"), places=2),
       conf=st.decimals(min_value=D("0"), max_value=D("1"), places=2),
       age=st.floats(min_value=0, max_value=100), vol=st.decimals(min_value=D("0.5"), max_value=D("5"), places=2))
def test_buffer_monotone_and_bounded(base, conf, age, vol):
    p = SafetyPolicy(base_buffer_frac=base)
    b = L.compute_buffer_frac(p, min_rule_confidence=conf, quote_age_s=age, volatility_ratio=vol)
    assert base <= b <= p.max_buffer_frac or b == p.max_buffer_frac
    b_worse = L.compute_buffer_frac(p, min_rule_confidence=min(conf, D("0.5")), quote_age_s=age + 10,
                                    volatility_ratio=vol + 1)
    assert b_worse >= b
