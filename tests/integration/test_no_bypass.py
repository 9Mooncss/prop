"""Architectural guarantees that no order can reach a broker without PreTradeGuard."""
import ast
import dataclasses
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from propguard.execution.guard import PreTradeGuard
from propguard.execution.interfaces import BrokerAdapter, BypassAttempt
from propguard.execution.simulator import SimClock, SimulatedBroker, SimulatedMarket
from tests.helpers import INSTRUMENTS, NOW, ctx, order

pytestmark = pytest.mark.risk
ROOT = Path(__file__).resolve().parents[2] / "propguard"
PROTECTED_HOOKS = {"_place", "_close", "_cancel", "_modify_sl"}


def _broker():
    clock = SimClock(NOW)
    m = SimulatedMarket(clock, INSTRUMENTS)
    m.set_price("EURUSD", "1.1000", "1.1001")
    b = SimulatedBroker("acc1", m, D("100000"))
    b.connect()
    return b


def test_submit_rejects_raw_orders_and_forged_tokens():
    b = _broker()
    with pytest.raises(BypassAttempt):
        b.submit(order())
    g = PreTradeGuard()
    res = g.authorize(order(), ctx(), adapter_name="simulated")
    assert res.approved is not None
    tampered = dataclasses.replace(res.approved, order=dataclasses.replace(res.approved.order, lots=D("50")))
    with pytest.raises(BypassAttempt):
        b.submit(tampered)
    other = dataclasses.replace(res.approved, adapter_name="other")
    with pytest.raises(BypassAttempt):
        b.submit(other)
    b.submit(res.approved)
    with pytest.raises(BypassAttempt):  # single use
        b.submit(res.approved)


def test_expired_and_denied_approvals():
    b = _broker()
    g = PreTradeGuard(ttl_s=0.01)
    res = g.authorize(order(coid="exp"), ctx(), adapter_name="simulated")
    time.sleep(0.03)
    with pytest.raises(BypassAttempt):
        b.submit(res.approved)
    denied = PreTradeGuard().authorize(order("50", coid="big"), ctx(), adapter_name="simulated")
    assert denied.approved is None


def test_adapters_cannot_override_submit():
    with pytest.raises(TypeError):
        class Evil(SimulatedBroker):  # noqa: F841
            def submit(self, approved):
                return self._place(approved)


def _calls_in(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node.func.attr, node.lineno
        if isinstance(node, ast.Name) and node.id in ("_KEY", "_sign", "_CONSUMED"):
            yield node.id, node.lineno


def test_static_no_protected_hook_calls_outside_adapter_base():
    offenders = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        for name, line in _calls_in(p):
            if name in PROTECTED_HOOKS and rel != "execution/interfaces.py":
                offenders.append(f"{rel}:{line} calls {name}")
            if name in ("_KEY", "_sign", "_CONSUMED") and rel != "execution/guard.py":
                offenders.append(f"{rel}:{line} touches {name}")
    assert not offenders, offenders


def test_strategies_and_api_never_import_adapters_directly():
    forbidden = ("simulator", "adapters")
    for sub in ("strategies", "recommender"):
        for p in (ROOT / sub).rglob("*.py"):
            src = p.read_text()
            for f in forbidden:
                assert f"propguard.execution.{f}" not in src, f"{p} imports {f}"


def test_broker_adapter_base_defines_submit_only_once():
    assert "submit" in BrokerAdapter.__dict__
    assert "submit" not in SimulatedBroker.__dict__
