"""Regression tests for the independent security/risk review findings."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from propguard.execution.interfaces import BrokerTimeout, OrderStatus
from propguard.execution.reconciliation import ReconciliationService
from propguard.execution.session import AccountSession, NewsCalendar
from propguard.execution.simulator import SimClock, SimulatedBroker, SimulatedMarket
from propguard.execution.stores import MemoryLedger, MemoryOrderStore, Stores
from propguard.logging_setup import redact
from propguard.registry.eligibility import Profile, Verdict, evaluate
from propguard.risk import limits as L
from propguard.risk.engine import RiskEngine
from propguard.risk.models import OrderIntent, OrderRequest, Side
from propguard.risk.policy import Action, KillSwitchKind, Reason
from propguard.risk.state import new_state
from tests.helpers import EURUSD, INSTRUMENTS, NOW, ctx, order, position, quote, ruleset, snapshot

pytestmark = pytest.mark.risk
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)


class FlakyBroker(SimulatedBroker):
    """Places the order, loses the response, and cannot answer lookups (finding 1)."""

    def _place(self, order):
        super()._place(order)
        raise BrokerTimeout("response lost")

    def find_order(self, client_order_id):
        raise BrokerTimeout("lookup timed out")


def _session(broker_cls=SimulatedBroker):
    clock = SimClock(T0)
    m = SimulatedMarket(clock, INSTRUMENTS)
    m.set_price("EURUSD", "1.1000", "1.1001")
    b = broker_cls("acc1", m, D("100000"))
    stores = Stores.memory()
    s = AccountSession("acc1", b, m, lambda: ruleset(verified_at=clock.now() - timedelta(minutes=1)), stores,
                       clock.now, news=lambda: NewsCalendar((), True))
    return s, b, stores, clock


def test_timeout_with_failing_lookup_never_resends():
    s, b, stores, clock = _session(FlakyBroker)
    s.start()
    out = s.submit(OrderRequest(client_order_id="t", account_id="acc1", symbol="EURUSD", side=Side.BUY,
                                lots=D("0.5"), stop_loss=D("1.0950"), created_at=clock.now()))
    assert b.submit_count == 1 and len(b.positions) == 1
    assert out.status == OrderStatus.UNKNOWN
    assert KillSwitchKind.AMBIGUOUS_EXECUTION_EVENT in stores.kill_switches.active("acc1")


def test_stateless_reduce_cannot_exceed_known_position():
    known = {"p1": ("BUY", D("1.00"), "EURUSD")}
    E = RiskEngine()
    big = order("500", side=Side.SELL, sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    assert E.evaluate(big, ctx(snap=None, known_positions=known)).action == Action.DENY
    unknown = order("0.5", side=Side.SELL, sl=None, intent=OrderIntent.REDUCE, position_id="p-x")
    assert E.evaluate(unknown, ctx(snap=None, known_positions=known)).primary_reason == Reason.STATE_MISSING
    same_side = order("0.5", side=Side.BUY, sl=None, intent=OrderIntent.REDUCE, position_id="p1")
    assert E.evaluate(same_side, ctx(snap=None, known_positions=known)).action == Action.DENY
    ok = order("1.00", side=Side.SELL, sl=None, intent=OrderIntent.CLOSE, position_id="p1")
    assert E.evaluate(ok, ctx(snap=None, known_positions=known)).allowed


def test_emergency_flag_cannot_be_set_by_callers():
    s, b, stores, clock = _session()
    s.start()
    o = OrderRequest(client_order_id="e", account_id="acc1", symbol="EURUSD", side=Side.BUY, lots=D("0.1"),
                     stop_loss=D("1.0950"), created_at=clock.now(), emergency=True)
    s.submit(o)
    req = stores.orders.get("e")["request"]
    assert req["emergency"] is False


def test_reconcile_lot_increase_raises_manual_trade_kill_switch():
    orders, ledger = MemoryOrderStore(), MemoryLedger()
    orders.reserve("c1", "acc1", {})
    ledger.upsert_position("acc1", "p1", {"lots": "1.00", "side": "BUY", "symbol": "EURUSD", "client_order_id": "c1"})
    snap = snapshot(positions=[position(lots="3.00", coid="c1")])
    res = ReconciliationService(orders, ledger).reconcile("acc1", snap, adapter=None)
    assert not res.ok and res.issues[0][0] == KillSwitchKind.UNEXPECTED_MANUAL_TRADE
    shrink = snapshot(positions=[position(lots="0.50", coid="c1")])
    res2 = ReconciliationService(orders, ledger).reconcile("acc1", shrink, adapter=None)
    assert res2.ok and ledger.positions("acc1")["p1"]["lots"] == "0.50"


def test_eligibility_never_passes_unset_inputs():
    firm = {"slug": "f", "status": "WATCHLIST", "jurisdiction": {"ukraine_citizens": "ALLOWED",
            "ukraine_residents": "ALLOWED"}, "kyc": {"required_before": "payout"}, "payout_classification":
            "DIRECT_CRYPTO", "automation": {"api_trading": "ALLOWED"}, "platforms": []}
    r = evaluate(Profile(residence_country="PL", ip_location_country="PL"), firm)
    v = {c.name: c.verdict for c in r.checks}
    assert v["tax_residency"] == Verdict.UNKNOWN and v["kyc_documents"] == Verdict.UNKNOWN


def test_redaction_covers_db_urls_and_registered_secrets():
    out = redact("OperationalError: connection to postgresql+psycopg://propguard:s3cr3tPass@db:5432/x failed")
    assert "s3cr3tPass" not in out and "***:***@" in out


def test_consumed_approvals_are_pruned():
    from propguard.execution import guard
    guard._CONSUMED["old"] = 0.0
    g = guard.PreTradeGuard()
    clock = SimClock(NOW)
    m = SimulatedMarket(clock, INSTRUMENTS)
    m.set_price("EURUSD", "1.1000", "1.1001")
    b = SimulatedBroker("acc1", m, D("100000"))
    b.connect()
    b.submit(g.authorize(order(coid="prune"), ctx(), adapter_name="simulated").approved)
    assert "old" not in guard._CONSUMED


money = st.decimals(min_value=D("92000"), max_value=D("112000"), places=2)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(balance=money, move=st.integers(min_value=-300, max_value=300), dstart=money,
       ml_basis=st.sampled_from(["equity", "balance"]), floating=st.booleans(),
       mode=st.sampled_from(["static", "trailing", "eod_trailing", "trailing_lock_at_initial"]),
       req=st.decimals(min_value=D("0.01"), max_value=D("5"), places=2), sl_pips=st.integers(10, 300),
       pending=st.booleans())
def test_invariant_holds_for_balance_basis_and_non_floating_rules(balance, move, dstart, ml_basis, floating, mode,
                                                                  req, sl_pips, pending):
    from propguard.risk.models import OrderType, PendingOrder
    rs = ruleset(daily_loss_limit={"pct": 5, "includes_floating": floating, "reset_tz": "Europe/Prague"},
                 max_loss={"pct": 10, "mode": mode, "basis": ml_basis})
    state = new_state("acc1", rs, snapshot(balance=str(dstart), ts=NOW - timedelta(hours=2), seq=0))
    q = quote()
    pos = position(entry=str(D("1.1000") - D(move) / D(10000)), sl=str(D("1.0900") - D(move) / D(10000)))
    u = L.computed_unrealized(pos, EURUSD, q)
    pos = position(entry=pos.entry_price, sl=pos.stop_loss, upnl=str(u))
    pend = [PendingOrder("b1", "cx", "EURUSD", Side.BUY, D("0.3"), OrderType.LIMIT, D("1.0950"), D("1.0900"))] \
        if pending else []
    snap = snapshot(balance=str(balance), equity=str(balance + u), positions=[pos], pending=pend)
    sl = q.ask - D(sl_pips) / D(10000)
    d = RiskEngine().evaluate(order(str(req), sl=str(sl)), ctx(rs=rs, snap=snap, state=state, quotes={"EURUSD": q}))
    if d.allowed:
        worst = snap.equity - d.open_risk - d.new_order_risk
        for lv in d.limits:
            assert worst >= lv.internal_floor, (lv.name, worst, lv.internal_floor)
