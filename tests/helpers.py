"""Shared builders for tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from propguard.risk.engine import RiskContext
from propguard.risk.models import AccountSnapshot, InstrumentSpec, OrderRequest, Position, Quote, Side
from propguard.risk.policy import SafetyPolicy
from propguard.risk.state import new_state
from propguard.rules.ruleset import build_ruleset

# A Tuesday, mid-session, far from 00:00 Europe/Prague reset and from weekend
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

EURUSD = InstrumentSpec(symbol="EURUSD", asset_class="fx", contract_size=D("100000"),
                        commission_per_lot_round_turn=D("7"), base_ccy="EUR", quote_ccy="USD")
XAUUSD = InstrumentSpec(symbol="XAUUSD", asset_class="metals", contract_size=D("100"),
                        commission_per_lot_round_turn=D("7"), base_ccy="XAU", quote_ccy="USD")
INSTRUMENTS = {"EURUSD": EURUSD, "XAUUSD": XAUUSD}


def base_rules(**overrides):
    rules = {
        "profit_target": {"pct": 10},
        "daily_loss_limit": {"pct": 5, "pct_of": "initial_balance", "reference": "day_start_max_balance_equity",
                             "includes_floating": True, "reset_time": "00:00", "reset_tz": "Europe/Prague"},
        "max_loss": {"pct": 10, "mode": "static", "basis": "equity"},
        "api_trading": {"allowed": "ALLOWED"},
        "ea_policy": {"allowed": "ALLOWED"},
        "news_trading": {"allowed": "ALLOWED"},
        "weekend_holding": {"allowed": "ALLOWED", "tz": "Europe/Prague"},
        "overnight_holding": {"allowed": "ALLOWED"},
    }
    for k, v in overrides.items():
        if v is None:
            rules.pop(k, None)
        else:
            rules[k] = v
    return rules


def ruleset(initial=100000, verified_at=None, statuses=None, pending=False, **overrides):
    statuses = statuses or {}
    verified_at = verified_at or (NOW - timedelta(hours=1))
    rules = [{"type": k, "params": v, "status": statuses.get(k, "CONFIRMED")}
             for k, v in base_rules(**overrides).items()]
    return build_ruleset(firm_slug="testfirm", program_slug="2step", phase="phase1", initial_balance=initial,
                         rules=rules, verified_at=verified_at, pending_critical_change=pending)


def quote(symbol="EURUSD", bid="1.1000", ask="1.1001", ts=None):
    return Quote(symbol=symbol, bid=D(bid), ask=D(ask), ts=ts or NOW)


def snapshot(balance="100000", equity=None, positions=(), pending=(), ts=None, seq=1):
    return AccountSnapshot(account_id="acc1", ts=ts or NOW, balance=D(balance),
                           equity=D(equity if equity is not None else balance),
                           positions=tuple(positions), pending_orders=tuple(pending), sequence=seq)


def ctx(rs=None, snap=None, state=None, quotes=None, **kw):
    rs = rs or ruleset()
    snap = snap or snapshot()
    now = kw.get("now", NOW)
    if state is None:
        state = new_state("acc1", rs, snapshot(ts=now - timedelta(hours=2), seq=0))
    q = quotes if quotes is not None else {"EURUSD": quote(), "XAUUSD": quote("XAUUSD", "2400.00", "2400.30")}
    defaults = dict(now=NOW, ruleset=rs, state=state, snapshot=snap, instruments=INSTRUMENTS, quotes=q,
                    policy=SafetyPolicy(), reconciliation_ok=True, news_calendar_available=True)
    defaults.update(kw)
    return RiskContext(**defaults)


def order(lots="1.00", side=Side.BUY, sl="1.0952", coid="o1", **kw):
    return OrderRequest(client_order_id=coid, account_id="acc1", symbol=kw.pop("symbol", "EURUSD"), side=side,
                        lots=D(lots), stop_loss=D(sl) if sl is not None else None, created_at=NOW, **kw)


def position(pid="p1", side=Side.BUY, lots="1.00", entry="1.1000", sl="1.0950", upnl=None, coid="c1",
             symbol="EURUSD", opened_at=None):
    return Position(position_id=pid, symbol=symbol, side=side, lots=D(lots), entry_price=D(entry),
                    opened_at=opened_at or NOW - timedelta(minutes=30),
                    stop_loss=D(sl) if sl is not None else None, client_order_id=coid,
                    unrealized_pnl=D(upnl) if upnl is not None else None)
