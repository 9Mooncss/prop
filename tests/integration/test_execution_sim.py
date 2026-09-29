from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from propguard.execution.interfaces import OrderStatus, Signal
from propguard.execution.session import AccountSession, NewsCalendar
from propguard.execution.simulator import SimClock, SimConfig, SimulatedBroker, SimulatedMarket
from propguard.execution.stores import Stores
from propguard.risk.models import OrderRequest, OrderType, Side
from propguard.risk.policy import KillSwitchKind, Reason
from tests.helpers import INSTRUMENTS, ruleset

pytestmark = [pytest.mark.risk, pytest.mark.integration]
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)  # Tue 10:00 Prague


class Env:
    def __init__(self, config=None, rs_kwargs=None, stores=None, broker=None, clock=None, market=None):
        self.clock = clock or SimClock(T0)
        self.market = market or SimulatedMarket(self.clock, INSTRUMENTS)
        if market is None:
            self.market.set_price("EURUSD", "1.1000", "1.1001")
            self.market.set_price("XAUUSD", "2400.00", "2400.30")
        self.broker = broker or SimulatedBroker("acc1", self.market, D("100000"), config or SimConfig())
        self.rs_kwargs = rs_kwargs or {}
        self.stores = stores or Stores.memory()
        self.session = AccountSession("acc1", self.broker, self.market, self.rules, self.stores, self.clock.now,
                                      news=lambda: NewsCalendar((), True))

    def rules(self):
        return ruleset(verified_at=self.clock.now() - timedelta(minutes=5), **self.rs_kwargs)

    def price(self, bid, symbol="EURUSD", spread="0.0001"):
        self.market.set_price(symbol, bid, spread=spread)
        self.broker.on_price_update()

    def buy(self, coid="o1", lots="0.50", sl="1.0950", **kw):
        return self.session.submit(OrderRequest(client_order_id=coid, account_id="acc1", symbol="EURUSD",
                                                side=Side.BUY, lots=D(lots), stop_loss=D(sl) if sl else None,
                                                created_at=self.clock.now(), **kw))


@pytest.fixture
def env():
    e = Env()
    e.session.start()
    return e


def test_happy_path_fill_and_ledger(env):
    out = env.buy()
    assert out.status == OrderStatus.FILLED, out.message
    assert len(env.broker.positions) == 1
    assert env.stores.ledger.positions("acc1")
    tick = env.session.tick()
    assert tick.recon.ok and not tick.new_kill_switches
    kinds = [e["kind"] for e in env.stores.audit.entries]
    assert "risk.decision" in kinds and "order.report" in kinds


def test_duplicate_client_order_id_never_resubmitted(env):
    env.buy("dup")
    out = env.buy("dup")
    assert out.duplicate and len(env.broker.positions) == 1 and env.broker.submit_count == 1


def test_timeout_after_send_resolved_without_duplicate():
    e = Env(SimConfig(timeout_next_after_send=1))
    e.session.start()
    out = e.buy("t1")
    assert out.status == OrderStatus.FILLED
    assert len(e.broker.positions) == 1


def test_timeout_before_send_retried_once_with_fresh_approval():
    e = Env(SimConfig(timeout_next_before_send=1))
    e.session.start()
    out = e.buy("t2")
    assert out.status == OrderStatus.FILLED and len(e.broker.positions) == 1
    decisions = e.stores.audit.of_kind("risk.decision")
    assert [d["payload"]["attempt"] for d in decisions] == [0, 1]


def test_partial_fill_and_reject():
    e = Env(SimConfig(partial_fill_ratio=D("0.5"), reject_next=0))
    e.session.start()
    out = e.buy("pf", lots="0.60")
    assert out.status == OrderStatus.PARTIALLY_FILLED and out.report.filled_lots == D("0.30")
    assert e.session.tick().recon.ok
    e.broker.config.reject_next = 1
    out = e.buy("rj", lots="0.10")
    assert out.status == OrderStatus.REJECTED_BY_BROKER


def test_risk_rejection_is_audited_and_not_sent(env):
    out = env.buy("big", lots="50")
    assert out.status == OrderStatus.REJECTED_BY_RISK
    assert env.broker.submit_count == 0
    rec = env.stores.audit.of_kind("risk.decision")[-1]["payload"]
    assert rec["decision"]["action"] == "DENY" and rec["decision"]["max_allowed_lots"] == "1.82"
    assert rec["state_snapshot"]["balance"] == "100000"


def test_manual_trade_triggers_kill_switch_but_close_still_possible(env):
    env.buy("mine")
    manual = env.broker.inject_manual_trade("EURUSD", Side.SELL, D("0.2"))
    tick = env.session.tick()
    assert "UNEXPECTED_MANUAL_TRADE" in tick.new_kill_switches
    out = env.buy("new", lots="0.1")
    assert out.status == OrderStatus.REJECTED_BY_RISK and Reason.KILL_SWITCH_ACTIVE in out.decision.reasons
    close = env.session.close_position(manual.position_id, reason="remove-manual")
    assert close.status == OrderStatus.FILLED


def test_silent_position_loss_is_reconciliation_mismatch(env):
    env.buy()
    pid = next(iter(env.broker.positions))
    env.broker.remove_position_silently(pid)
    tick = env.session.tick()
    assert "RECONCILIATION_MISMATCH" in tick.new_kill_switches


def test_contradictory_balance_and_stale_feed_kill_switches(env):
    env.buy()
    env.broker.corrupt_equity(D("-750"))
    assert "CONTRADICTORY_BALANCES" in env.session.tick().new_kill_switches
    env.broker.corrupt_equity(D("0"))
    env.market.freeze("EURUSD")
    env.clock.advance(seconds=60)
    assert "STALE_MARKET_DATA" in env.session.tick().new_kill_switches


def test_duplicate_broker_events_processed_once():
    e = Env(SimConfig(duplicate_events=True))
    e.session.start()
    e.buy()
    pid = next(iter(e.broker.positions))
    e.session.close_position(pid)
    e.session.tick()
    e.session.tick()
    closed = [x for x in e.stores.audit.of_kind("broker.event") if x["payload"]["kind"] == "position_closed"]
    assert len(closed) == 1


def test_stop_loss_gap_through_and_closed_pnl_recorded(env):
    env.buy(lots="1.00", sl="1.0950")
    env.clock.advance(minutes=5)
    env.price("1.0900")  # gap through the stop
    env.session.tick()
    assert not env.broker.positions
    st = env.stores.state.load("acc1")
    assert sum(st.daily_closed_pnl.values()) < D("-1000")  # worse than stop distance (gap)


def test_restart_mid_session_resumes_state_and_reconciles(env):
    env.buy()
    env.clock.advance(minutes=1)
    env.price("1.1050")
    env.session.tick()
    hwm_before = env.stores.state.load("acc1").hwm_equity
    # "restart": new session object, same persistent stores and platform
    e2 = Env(stores=env.stores, broker=env.broker, clock=env.clock, market=env.market)
    e2.broker.disconnect()
    tick = e2.session.start()
    assert tick.recon.ok
    assert e2.stores.state.load("acc1").hwm_equity == hwm_before
    assert e2.buy("after-restart", lots="0.1").status == OrderStatus.FILLED


def test_restart_after_missed_reset_blocks_new_risk_unless_reconstructed(env):
    env.buy()
    # app down across midnight Prague with position open -> cannot reconstruct -> unknown
    env.clock.set(datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
    env.price("1.1000")
    e2 = Env(stores=env.stores, broker=env.broker, clock=env.clock, market=env.market)
    e2.session.start()
    assert not e2.stores.state.load("acc1").day_start_known
    out = e2.buy("x", lots="0.1")
    assert Reason.DAY_STATE_UNCERTAIN in out.decision.reasons
    # flat account -> reconstructable from deal history
    f = Env()
    f.session.start()
    f.clock.set(datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
    f.price("1.1000")
    f2 = Env(stores=f.stores, broker=f.broker, clock=f.clock, market=f.market)
    f2.session.start()
    assert f2.stores.state.load("acc1").day_start_known


def test_reconnect_raises_kill_switch_until_reviewed(env):
    env.broker.disconnect()
    tick = env.session.tick()
    assert "RECONNECT_UNCERTAIN_STATE" in tick.new_kill_switches
    assert env.buy("n", lots="0.1").status == OrderStatus.REJECTED_BY_RISK
    assert env.session.clear_kill_switch(KillSwitchKind.RECONNECT_UNCERTAIN_STATE, "owner", "reviewed")
    env.session.tick()
    assert env.buy("n2", lots="0.1").status == OrderStatus.FILLED


def test_rule_change_pending_blocks_new_risk_allows_reduction():
    e = Env()
    e.session.start()
    e.buy()
    e.rs_kwargs = {}
    orig = e.rules
    e.rules = lambda: type(orig())(**{**orig().__dict__, "pending_critical_change": True})
    e.session.rules = e.rules
    e.session.executor.context_fn = e.session.context
    out = e.buy("blocked", lots="0.1")
    assert Reason.RULE_CHANGE_PENDING in out.decision.reasons
    pid = next(iter(e.broker.positions))
    assert e.session.close_position(pid).status == OrderStatus.FILLED


def test_uncertain_critical_rule_raises_verification_kill_switch():
    e = Env(rs_kwargs={"statuses": {"news_trading": "UNCERTAIN"}})
    tick = e.session.start()
    assert "RULE_VERIFICATION_FAILURE" in tick.new_kill_switches


def test_supervisor_flattens_before_weekend_and_cancels_pending():
    e = Env(rs_kwargs={"weekend_holding": {"allowed": "PROHIBITED", "close_by": "21:00", "tz": "UTC"}})
    e.session.start()
    e.buy()
    e.session.submit(OrderRequest(client_order_id="lim", account_id="acc1", symbol="EURUSD", side=Side.BUY,
                                  lots=D("0.1"), order_type=OrderType.LIMIT, price=D("1.0900"),
                                  stop_loss=D("1.0850"), created_at=e.clock.now()))
    assert e.broker.pending
    e.clock.set(datetime(2026, 10, 2, 20, 40, tzinfo=timezone.utc))
    e.price("1.1000")
    tick = e.session.tick()
    assert not e.broker.positions and not e.broker.pending
    assert all(a.status in (OrderStatus.FILLED, OrderStatus.CANCELLED) for a in tick.actions)


def test_internal_floor_emergency_exit_when_buffer_grows():
    from propguard.risk.policy import SafetyPolicy
    e = Env()
    e.session.policy = SafetyPolicy(max_risk_per_trade_frac=D("0.05"))
    e.session.start()
    assert e.buy("ok", lots="0.90", sl="1.0650").status == OrderStatus.FILLED
    # by construction the stop keeps the worst case above the internal floor ...
    e.clock.advance(minutes=1)
    e.price("1.0700")
    assert "INTERNAL_DRAWDOWN_LIMIT" not in e.session.tick().new_kill_switches
    assert e.broker.positions
    # ... until volatility rises: buffer grows 0.30 -> 0.50, internal floor moves up to 97500
    e.market.set_volatility_ratio("EURUSD", D("3"))
    tick = e.session.tick()
    assert "INTERNAL_DRAWDOWN_LIMIT" in tick.new_kill_switches
    assert not e.broker.positions  # flattened by supervisor (risk-reducing, emergency)
    assert tick.actions and tick.actions[0].status == OrderStatus.FILLED


def test_gap_through_internal_floor_raises_kill_switch():
    e = Env()
    e.session.start()
    e.buy("g", lots="0.20", sl="1.0600")
    e.clock.advance(minutes=1)
    e.price("0.9200")  # weekend-style gap far through the stop
    tick = e.session.tick()
    assert "INTERNAL_DRAWDOWN_LIMIT" in tick.new_kill_switches or "EXTERNAL_LIMIT_BREACHED" in tick.new_kill_switches


def test_signal_path_sizes_and_goes_through_guard(env):
    sig = Signal("s1", "EURUSD", "BUY", D("1.0970"), risk_frac=D("0.0025"), strategy_id="t")
    out = env.session.submit_signal(sig)
    assert out.status == OrderStatus.FILLED
    again = env.session.submit_signal(sig)
    assert again.duplicate
