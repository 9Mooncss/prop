from datetime import timedelta
from decimal import Decimal as D

import pytest

from propguard.risk.engine import NewsEvent, RiskEngine
from propguard.risk.models import OrderIntent, OrderType, PendingOrder, Side
from propguard.risk.policy import Action, KillSwitchKind, Reason, SafetyPolicy
from propguard.risk.state import new_state
from tests.helpers import NOW, ctx, order, position, quote, ruleset, snapshot

pytestmark = pytest.mark.risk
E = RiskEngine()


def test_allow_basic_with_full_decision_payload():
    d = E.evaluate(order("1.00"), ctx())
    assert d.action == Action.ALLOW, d.explanation
    assert d.reasons == (Reason.OK,)
    # per-lot risk: entry 1.1002 (ask+slip), exit 1.0950 (sl - slip - spread) => 0.0052*100000 + 7 = 527
    assert d.new_order_risk == D("527.0000")
    # binding: risk per trade 1% of 100k = 1000 / 527 = 1.897 -> 1.89
    assert d.max_allowed_lots == D("1.89")
    names = {lv.name for lv in d.limits}
    assert names == {"daily_loss", "max_loss"}
    daily = next(lv for lv in d.limits if lv.name == "daily_loss")
    assert daily.external_floor == D("95000")
    assert daily.internal_floor == D("96500")  # 30% of 5000 buffer
    assert d.freshness["reconciliation_ok"] is True
    assert d.rule_ids and d.engine_version and d.ruleset_id
    payload = d.to_dict()
    assert payload["allowed"] is True and payload["reasons"] == ["OK"]


def test_deny_above_risk_per_trade_reports_max_size():
    d = E.evaluate(order("2.00"), ctx())
    assert d.action == Action.DENY
    assert d.primary_reason == Reason.RISK_PER_TRADE
    assert d.max_allowed_lots == D("1.89")


def test_downsize_when_policy_allows():
    d = E.evaluate(order("2.00"), ctx(policy=SafetyPolicy(allow_downsize=True)))
    assert d.action == Action.MODIFY and d.approved_lots == D("1.89")


def test_daily_internal_limit_binds_after_losses():
    # lost 3000 today; internal floor 96500 -> capacity 500 -> 0.94 lots
    c = ctx(snap=snapshot(balance="97000"))
    d = E.evaluate(order("1.00"), c)
    assert d.action == Action.DENY and d.primary_reason == Reason.INTERNAL_DAILY_LIMIT
    assert d.max_allowed_lots == D("0.94")
    assert E.evaluate(order("0.94", coid="o2"), c).action == Action.ALLOW


def test_internal_floor_reached_denies_everything_new():
    d = E.evaluate(order("0.01"), ctx(snap=snapshot(balance="96400")))
    assert d.action == Action.DENY and d.primary_reason == Reason.INTERNAL_DAILY_LIMIT
    assert d.max_allowed_lots == 0


def test_external_breach_denies():
    d = E.evaluate(order("0.01"), ctx(snap=snapshot(balance="94900")))
    assert Reason.EXTERNAL_DAILY_LIMIT_BREACHED in d.reasons


def test_stop_loss_required_and_invalid_stop():
    assert E.evaluate(order(sl=None), ctx()).primary_reason == Reason.STOP_LOSS_REQUIRED
    assert E.evaluate(order(sl="1.1100"), ctx()).primary_reason == Reason.INVALID_STOP_LOSS
    assert E.evaluate(order(side=Side.SELL, sl="1.0900"), ctx()).primary_reason == Reason.INVALID_STOP_LOSS


def test_no_stop_uses_gap_risk_when_policy_permits():
    d = E.evaluate(order("0.10", sl=None), ctx(policy=SafetyPolicy(require_stop_loss=False)))
    # gap 2% of ~1.1002 * 100000 = ~2200 per lot -> 1000/2207 -> 0.45 lots max
    assert d.max_allowed_lots == D("0.45")


@pytest.mark.parametrize("kw,reason", [
    (dict(active_kill_switches=(KillSwitchKind.MANUAL,)), Reason.KILL_SWITCH_ACTIVE),
    (dict(reconciliation_ok=False), Reason.RECONCILIATION_NOT_OK),
    (dict(challenge_active=False), Reason.CHALLENGE_NOT_ACTIVE),
    (dict(seen_client_order_ids=frozenset({"o1"})), Reason.DUPLICATE_ORDER),
    (dict(news_calendar_available=False, rs=ruleset(news_trading={"allowed": "UNKNOWN"})),
     Reason.NEWS_CALENDAR_UNAVAILABLE),
])
def test_system_gates(kw, reason):
    d = E.evaluate(order(), ctx(**kw))
    assert d.action == Action.DENY and reason in d.reasons


def test_stale_market_data_and_snapshot():
    stale_q = {"EURUSD": quote(ts=NOW - timedelta(seconds=30))}
    assert Reason.STALE_MARKET_DATA in E.evaluate(order(), ctx(quotes=stale_q)).reasons
    assert Reason.NO_QUOTE in E.evaluate(order(), ctx(quotes={})).reasons
    assert Reason.STATE_STALE in E.evaluate(order(), ctx(snap=snapshot(ts=NOW - timedelta(minutes=5)))).reasons
    assert Reason.INVALID_QUOTE in E.evaluate(order(), ctx(quotes={"EURUSD": quote(bid="1.2", ask="1.1")})).reasons


def test_rule_freshness_and_certainty_fail_closed():
    stale = ruleset(verified_at=NOW - timedelta(hours=30))
    assert Reason.RULES_STALE in E.evaluate(order(), ctx(rs=stale)).reasons
    unc = ruleset(statuses={"daily_loss_limit": "UNCERTAIN"})
    assert Reason.RULE_UNCERTAIN in E.evaluate(order(), ctx(rs=unc)).reasons
    conf = ruleset(statuses={"max_loss": "CONFLICT"})
    assert Reason.RULE_CONFLICT in E.evaluate(order(), ctx(rs=conf)).reasons
    pend = ruleset(pending=True)
    assert Reason.RULE_CHANGE_PENDING in E.evaluate(order(), ctx(rs=pend)).reasons
    incomplete = ruleset(max_loss=None)
    assert Reason.RULES_INCOMPLETE in E.evaluate(order(), ctx(rs=incomplete)).reasons


def test_uncertain_rule_still_allows_risk_reduction():
    unc = ruleset(statuses={"daily_loss_limit": "UNCERTAIN"}, pending=True)
    snap = snapshot(positions=[position()])
    close = order("1.00", side=Side.SELL, sl=None, coid="c", intent=OrderIntent.CLOSE, position_id="p1")
    d = E.evaluate(close, ctx(rs=unc, snap=snap, active_kill_switches=(KillSwitchKind.STALE_RULES,)))
    assert d.action == Action.ALLOW


def test_automation_policy():
    for allowed in ("PROHIBITED", "UNKNOWN"):
        rs = ruleset(api_trading={"allowed": allowed})
        assert Reason.AUTOMATION_NOT_PERMITTED in E.evaluate(order(), ctx(rs=rs)).reasons
    rs = ruleset(api_trading={"allowed": "CONDITIONAL", "conditions": "own strategy only"})
    assert Reason.AUTOMATION_NOT_PERMITTED in E.evaluate(order(), ctx(rs=rs)).reasons
    pol = SafetyPolicy(treat_conditional_automation_as_allowed=True)
    assert E.evaluate(order(), ctx(rs=rs, policy=pol, automation_conditions_acknowledged=True)).allowed
    # ea channel uses ea_policy
    rs = ruleset(ea_policy={"allowed": "PROHIBITED"})
    assert E.evaluate(order(), ctx(rs=rs)).allowed  # api channel unaffected
    assert not E.evaluate(order(), ctx(rs=rs, automation_channel="ea")).allowed
    rs = ruleset(api_trading=None)
    assert Reason.AUTOMATION_NOT_PERMITTED in E.evaluate(order(), ctx(rs=rs)).reasons


def test_news_blackout():
    rs = ruleset(news_trading={"allowed": "PROHIBITED", "blackout_before_min": 2, "blackout_after_min": 2})
    ev = NewsEvent(ts=NOW + timedelta(minutes=6), currency="USD", impact="high", title="NFP")
    d = E.evaluate(order(), ctx(rs=rs, news_events=(ev,)))
    assert Reason.NEWS_BLACKOUT in d.reasons  # 2 + 5 internal extra minutes
    ev_far = NewsEvent(ts=NOW + timedelta(minutes=30), currency="USD", impact="high")
    assert E.evaluate(order(), ctx(rs=rs, news_events=(ev_far,))).allowed
    ev_other = NewsEvent(ts=NOW + timedelta(minutes=1), currency="JPY", impact="high")
    assert E.evaluate(order(), ctx(rs=rs, news_events=(ev_other,))).allowed
    # closing inside blackout when firm also bans closes: denied unless emergency
    snap = snapshot(positions=[position()])
    close = order("1.00", side=Side.SELL, sl=None, coid="c", intent=OrderIntent.CLOSE, position_id="p1")
    assert E.evaluate(close, ctx(rs=rs, snap=snap, news_events=(ev,))).primary_reason == Reason.NEWS_BLACKOUT
    em = order("1.00", side=Side.SELL, sl=None, coid="c2", intent=OrderIntent.CLOSE, position_id="p1", emergency=True)
    assert E.evaluate(em, ctx(rs=rs, snap=snap, news_events=(ev,))).allowed


def test_weekend_guard():
    friday = NOW + timedelta(days=3, hours=9, minutes=30)  # Friday 19:30 UTC
    rs = ruleset(verified_at=friday, weekend_holding={"allowed": "PROHIBITED", "close_by": "21:00", "tz": "UTC"})
    assert friday.weekday() == 4
    c = ctx(rs=rs, now=friday, snap=snapshot(ts=friday), quotes={"EURUSD": quote(ts=friday)})
    assert Reason.WEEKEND_HOLDING in E.evaluate(order(), c).reasons
    rs_ok = ruleset(verified_at=friday, weekend_holding={"allowed": "ALLOWED", "tz": "UTC"})
    c = ctx(rs=rs_ok, now=friday, snap=snapshot(ts=friday), quotes={"EURUSD": quote(ts=friday)})
    assert E.evaluate(order(), c).allowed


def test_rollover_guard():
    near = NOW.replace(hour=21, minute=55)  # 23:55 Prague (CEST) -> 5 min before reset
    c = ctx(now=near, snap=snapshot(ts=near), quotes={"EURUSD": quote(ts=near)})
    assert Reason.ROLLOVER_GUARD in E.evaluate(order(), c).reasons


def test_open_positions_and_pending_orders_consume_capacity():
    # open position 1 lot, SL 50 pips away from entry at 1.1000; mark 1.1000 -> remaining ~ 520+3.5
    snap = snapshot(balance="97000", positions=[position(upnl="0")])
    d = E.evaluate(order("0.10"), ctx(snap=snap))
    assert d.open_risk > D("520")
    assert d.action == Action.DENY  # 97000 - ~523 < 96500 internal floor, no capacity left
    po = PendingOrder("b1", "cx", "EURUSD", Side.BUY, D("1"), OrderType.LIMIT, D("1.0990"), D("1.0940"))
    d2 = E.evaluate(order("0.10"), ctx(snap=snapshot(balance="97500", pending=[po])))
    assert d2.open_risk > D("500")


def test_consistency_checks():
    bad = snapshot(balance="100000", equity="99000", positions=[position(upnl="0")])
    assert Reason.BALANCE_INCONSISTENT in E.evaluate(order(), ctx(snap=bad)).reasons
    mism = snapshot(balance="100000", equity="100500", positions=[position(upnl="500")])
    assert Reason.CALC_MISMATCH in E.evaluate(order(), ctx(snap=mism)).reasons
    unknown = snapshot(positions=[position(coid=None, upnl="0")])
    assert Reason.UNKNOWN_POSITION in E.evaluate(order(), ctx(snap=unknown)).reasons


def test_state_from_previous_trading_day_denies_new_risk():
    rs = ruleset()
    old = new_state("acc1", rs, snapshot(ts=NOW - timedelta(days=1), seq=0))
    assert Reason.DAY_STATE_UNCERTAIN in E.evaluate(order(), ctx(state=old)).reasons


def test_day_state_unknown_denies_new_risk():
    rs = ruleset()
    st = new_state("acc1", rs, snapshot(ts=NOW - timedelta(hours=2), seq=0))
    st.day_start_known = False
    assert Reason.DAY_STATE_UNCERTAIN in E.evaluate(order(), ctx(state=st)).reasons


def test_reduce_validation():
    snap = snapshot(positions=[position(lots="1.00")])
    c = ctx(snap=snap, active_kill_switches=(KillSwitchKind.MANUAL,))
    same_side = order("0.5", side=Side.BUY, sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    assert E.evaluate(same_side, c).primary_reason == Reason.NOT_RISK_REDUCING
    too_big = order("1.5", side=Side.SELL, sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    assert E.evaluate(too_big, c).primary_reason == Reason.NOT_RISK_REDUCING
    ok = order("0.5", side=Side.SELL, sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    assert E.evaluate(ok, c).allowed
    missing = order("0.5", side=Side.SELL, sl=None, intent=OrderIntent.REDUCE, position_id="nope")
    assert E.evaluate(missing, c).primary_reason == Reason.POSITION_NOT_FOUND
    partial_close = order("0.5", side=Side.SELL, sl=None, intent=OrderIntent.CLOSE, position_id="p1")
    assert not E.evaluate(partial_close, c).allowed


def test_cancel_and_modify_sl():
    po = PendingOrder("b1", "cx", "EURUSD", Side.BUY, D("1"), OrderType.LIMIT, D("1.0990"), D("1.0940"))
    snap = snapshot(positions=[position(sl="1.0950")], pending=[po])
    c = ctx(snap=snap, active_kill_switches=(KillSwitchKind.MANUAL,))
    assert E.evaluate(order(intent=OrderIntent.CANCEL, target_broker_order_id="b1", sl=None), c).allowed
    assert not E.evaluate(order(intent=OrderIntent.CANCEL, target_broker_order_id="zz", sl=None), c).allowed
    tighter = order(intent=OrderIntent.MODIFY_SL, position_id="p1", sl="1.0970")
    looser = order(intent=OrderIntent.MODIFY_SL, position_id="p1", sl="1.0900")
    assert E.evaluate(tighter, c).allowed
    assert E.evaluate(looser, c).primary_reason == Reason.NOT_RISK_REDUCING


def test_profit_target_caps_risk():
    d = E.evaluate(order("1.00"), ctx(snap=snapshot(balance="110500")))
    assert d.action == Action.DENY and d.primary_reason == Reason.RISK_PER_TRADE
    # post-target cap 0.1% of initial = 100 / 527 -> 0.18
    assert d.max_allowed_lots == D("0.18")


def test_max_lot_and_leverage_rules():
    rs = ruleset(max_lot_size={"max_lots": "0.5"})
    d = E.evaluate(order("1.00"), ctx(rs=rs))
    assert d.primary_reason == Reason.MAX_LOT_SIZE and d.max_allowed_lots == D("0.50")
    rs = ruleset(leverage={"max_by_class": {"fx": "1"}})
    d = E.evaluate(order("1.00"), ctx(rs=rs))
    # 1:1 leverage on 100k equity, 1 lot notional ~110k -> max 0.90
    assert d.primary_reason == Reason.LEVERAGE and d.max_allowed_lots == D("0.90")


def test_instrument_restrictions():
    rs = ruleset(instrument_restrictions={"allowed_classes": ["fx"]})
    xau = order("0.01", symbol="XAUUSD", sl="2390.00")
    assert Reason.INSTRUMENT_NOT_ALLOWED in E.evaluate(xau, ctx(rs=rs)).reasons


def test_buffer_increases_with_uncertainty_and_volatility():
    base = E.evaluate(order("0.10"), ctx()).buffer_frac
    vol = E.evaluate(order("0.10"), ctx(volatility_ratio=D("2"))).buffer_frac
    assert vol > base
    rs = ruleset()
    lowconf = type(rs)(**{**rs.__dict__, "rules": tuple(
        type(r)(**{**r.__dict__, "confidence": D("0.8")}) if r.kind == "daily_loss_limit" else r
        for r in rs.rules)})
    assert E.evaluate(order("0.10"), ctx(rs=lowconf)).buffer_frac > base


def test_assess_triggers_kill_switches():
    a = E.assess(ctx(snap=snapshot(balance="96400")))
    kinds = {t.kind for t in a.kill_switch_triggers}
    assert KillSwitchKind.INTERNAL_DRAWDOWN_LIMIT in kinds
    a = E.assess(ctx(snap=snapshot(balance="94000")))
    assert KillSwitchKind.EXTERNAL_LIMIT_BREACHED in {t.kind for t in a.kill_switch_triggers}
    stale = {"EURUSD": quote(ts=NOW - timedelta(minutes=5))}
    a = E.assess(ctx(snap=snapshot(positions=[position(upnl="0")]), quotes=stale))
    assert KillSwitchKind.STALE_MARKET_DATA in {t.kind for t in a.kill_switch_triggers}
    a = E.assess(ctx(snap=snapshot(positions=[position(coid=None)])))
    assert KillSwitchKind.UNEXPECTED_MANUAL_TRADE in {t.kind for t in a.kill_switch_triggers}


def test_required_actions_flatten_before_weekend_and_on_internal_floor():
    rs = ruleset(weekend_holding={"allowed": "PROHIBITED", "close_by": "21:00", "tz": "UTC"})
    friday = NOW + timedelta(days=3, hours=10, minutes=45)  # Fri 20:45 UTC
    snap = snapshot(ts=friday, positions=[position(upnl="0")])
    acts = E.required_actions(ctx(rs=rs, now=friday, snap=snap, quotes={"EURUSD": quote(ts=friday)}))
    assert len(acts) == 1 and acts[0].intent == OrderIntent.CLOSE and not acts[0].emergency
    acts = E.required_actions(ctx(snap=snapshot(balance="96000", equity="96000",
                                                positions=[position(upnl="0")])))
    assert acts and acts[0].emergency
    assert E.required_actions(ctx(snap=snapshot(positions=[position(upnl="0")]))) == []
