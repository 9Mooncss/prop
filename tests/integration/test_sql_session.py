"""Execution session on persistent SQL stores: restart recovery and live-gate behaviour."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from propguard.db.stores import SqlLiveApprovals, sql_stores
from propguard.execution.interfaces import AdapterCapabilities, OrderStatus
from propguard.execution.live_gate import LiveGate, LiveNotAllowed
from propguard.execution.session import AccountSession, NewsCalendar
from propguard.execution.simulator import SimClock, SimulatedBroker, SimulatedMarket
from propguard.risk.models import OrderRequest, Side
from tests.helpers import INSTRUMENTS, ruleset

pytestmark = [pytest.mark.risk, pytest.mark.integration]
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)


def _session(sf, clock, market, broker, gate=None):
    kw = {"live_gate": gate} if gate else {}
    return AccountSession("acc1", broker, market, lambda: ruleset(verified_at=clock.now() - timedelta(minutes=1)),
                          sql_stores(sf), clock.now, news=lambda: NewsCalendar((), True), **kw)


def test_restart_with_sql_stores_keeps_idempotency_and_state(sf):
    clock = SimClock(T0)
    m = SimulatedMarket(clock, INSTRUMENTS)
    m.set_price("EURUSD", "1.1000", "1.1001")
    b = SimulatedBroker("acc1", m, D("100000"))
    s1 = _session(sf, clock, m, b)
    s1.start()
    o = OrderRequest(client_order_id="x1", account_id="acc1", symbol="EURUSD", side=Side.BUY, lots=D("0.5"),
                     stop_loss=D("1.0950"), created_at=clock.now())
    assert s1.submit(o).status == OrderStatus.FILLED
    clock.advance(minutes=2)
    s2 = _session(sf, clock, m, b)  # process restart
    tick = s2.start()
    assert tick.recon.ok, tick.recon.issues
    again = s2.submit(o)
    assert again.duplicate and len(b.positions) == 1
    st = s2.stores.state.load("acc1")
    assert st.day_start_known and st.trading_days


def test_live_adapter_refused_without_all_gate_conditions(sf, tmp_path):
    clock = SimClock(T0)
    m = SimulatedMarket(clock, INSTRUMENTS)
    m.set_price("EURUSD", "1.1000", "1.1001")
    live_caps = AdapterCapabilities(name="fake-live", is_live=True, channel="api")
    b = SimulatedBroker("acc1", m, D("100000"), capabilities=live_caps)
    with pytest.raises(LiveNotAllowed):
        _session(sf, clock, m, b)  # default PAPER_ONLY gate
    gate = LiveGate(allow_live_env=True, approvals=SqlLiveApprovals(sf), acceptance_marker=tmp_path / "none.json")
    with pytest.raises(LiveNotAllowed, match="no explicit LIVE approval"):
        _session(sf, clock, m, b, gate)
