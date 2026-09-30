"""AccountSession: owns one trading account's refresh loop, context building and supervision.

Order of operations each tick:
  1. pull snapshot + new broker events (dedup by event id, applied in sequence order)
  2. update persistent RiskState (rollover, HWM) -- out-of-order snapshots are rejected
  3. reconcile ledger vs platform -> kill switches on mismatch / unknown / manual positions
  4. account assessment -> kill switches on internal/external limits, inconsistent balances, stale data
  5. deterministic protective actions (flatten before prohibited holding, internal-floor exits)
Strategies only receive ``submit_signal``; they never see the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from propguard.execution.engine import ExecutionEngine, ExecutionOutcome, make_client_order_id
from propguard.execution.guard import PreTradeGuard
from propguard.execution.interfaces import (
    BrokerAdapter,
    BrokerError,
    BrokerTimeout,
    MarketDataProvider,
    OrderStatus,
    Signal,
)
from propguard.execution.live_gate import PAPER_ONLY_GATE, LiveGate
from propguard.execution.reconciliation import ReconciliationService, ReconResult
from propguard.execution.sizing import FixedFractionalSizer
from propguard.execution.stores import Stores
from propguard.risk.engine import NewsEvent, RiskContext, RiskEngine
from propguard.risk.models import AccountSnapshot, OrderIntent, OrderRequest, Side
from propguard.risk.policy import KillSwitchKind, SafetyPolicy
from propguard.risk.state import new_state, record_activity, record_closed_pnl, reset_spec, update_state
from propguard.risk.tradingday import previous_reset
from propguard.rules.ruleset import RuleSet


@dataclass
class NewsCalendar:
    events: tuple[NewsEvent, ...] = ()
    available: bool = True


@dataclass
class TickResult:
    snapshot: AccountSnapshot | None
    recon: ReconResult | None
    new_kill_switches: list[str] = field(default_factory=list)
    actions: list[ExecutionOutcome] = field(default_factory=list)
    events_applied: int = 0
    notes: list[str] = field(default_factory=list)


class AccountSession:
    def __init__(self, account_id: str, adapter: BrokerAdapter, market: MarketDataProvider,
                 rules: Callable[[], RuleSet], stores: Stores, clock: Callable[[], datetime],
                 policy: SafetyPolicy | None = None, news: Callable[[], NewsCalendar] | None = None,
                 live_gate: LiveGate = PAPER_ONLY_GATE, challenge_active: Callable[[], bool] | None = None,
                 automation_conditions_acknowledged: bool = False) -> None:
        self.account_id = account_id
        self._adapter = adapter
        self.market = market
        self.rules = rules
        self.stores = stores
        self.clock = clock
        self.policy = policy or SafetyPolicy()
        self.news = news or (lambda: NewsCalendar((), False))
        self.challenge_active = challenge_active or (lambda: True)
        self.ack = automation_conditions_acknowledged
        self.risk = RiskEngine(self.policy)
        self.recon_service = ReconciliationService(stores.orders, stores.ledger)
        self._snapshot: AccountSnapshot | None = None
        self._recon_ok = False
        self.executor = ExecutionEngine(account_id, adapter, stores, self.context,
                                        PreTradeGuard(self.risk), live_gate)
        self.sizer = FixedFractionalSizer()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> TickResult:
        """Connect and recover. After a restart, state is loaded from the store; if the daily reset
        was missed while down, the day-start reference is reconstructed from platform history or
        flagged unknown (which blocks new risk for the rest of that trading day)."""
        self._adapter.connect()
        existing = self.stores.state.load(self.account_id)
        if existing is None:
            snap = self._adapter.get_snapshot()
            rs = self.rules()
            self.stores.state.save(new_state(self.account_id, rs, snap))
            self.stores.audit.record("session.new_state", self.account_id, {"balance": str(snap.balance)})
        else:
            self.stores.audit.record("session.resume", self.account_id,
                                     {"last_snapshot_ts": existing.last_snapshot_ts.isoformat()
                                      if existing.last_snapshot_ts else None})
        return self.tick(protective=False)

    def tick(self, protective: bool = True) -> TickResult:
        self.clock()
        rs = self.rules()
        res = TickResult(None, None)
        if not self._adapter.is_connected():
            try:
                self._adapter.connect()
            except Exception as exc:  # noqa: BLE001
                res.notes.append(f"reconnect failed: {exc}")
            if self._adapter.is_connected():
                self._raise(KillSwitchKind.RECONNECT_UNCERTAIN_STATE,
                            "reconnected; state must be reconciled and reviewed", res)
        try:
            snap = self._adapter.get_snapshot()
        except (BrokerError, BrokerTimeout) as exc:
            self._snapshot = None
            self._recon_ok = False
            res.notes.append(f"snapshot unavailable: {exc}")
            return res
        state = self.stores.state.load(self.account_id)
        res.events_applied = self._apply_events(rs, state)
        state = self.stores.state.load(self.account_id)
        override = None
        rt, tz = reset_spec(rs)
        if state and state.trading_date is not None:
            from propguard.risk.tradingday import trading_date
            if trading_date(snap.ts, rt, tz) != state.trading_date:
                override = self._adapter.reconstruct_day_start(previous_reset(snap.ts, rt, tz))
        upd = update_state(state, snap, rs, day_start_override=override)
        if not upd.accepted:
            res.notes.extend(upd.events)
            self.stores.audit.record("state.snapshot_rejected", self.account_id, {"events": upd.events})
        else:
            self.stores.state.save(upd.state)
            if upd.events:
                self.stores.audit.record("state.events", self.account_id, {"events": upd.events})
        self._snapshot = snap if upd.accepted else self._snapshot
        res.snapshot = self._snapshot
        recon = self.recon_service.reconcile(self.account_id, snap, self._adapter)
        res.recon = recon
        self._recon_ok = recon.ok
        for kind, msg in recon.issues:
            self._raise(kind, msg, res)
        if recon.healed:
            self.stores.audit.record("recon.healed", self.account_id, {"items": recon.healed})
        ctx = self.context()
        assessment = self.risk.assess(ctx)
        for trig in assessment.kill_switch_triggers:
            self._raise(trig.kind, trig.reason, res, trig.details)
        # rules freshness at account level -> kill switch (new risk is blocked by engine anyway)
        for r in rs.non_confirmed_critical():
            self._raise(KillSwitchKind.RULE_VERIFICATION_FAILURE, f"{r.kind} is {r.status.value}", res)
        if protective:
            for action in self.risk.required_actions(self.context()):
                res.actions.append(self.executor.execute(action))
        return res

    # ------------------------------------------------------------------ context
    def context(self) -> RiskContext:
        now = self.clock()
        snap = self._snapshot
        symbols = set()
        if snap:
            symbols |= {p.symbol for p in snap.positions} | {o.symbol for o in snap.pending_orders}
        instruments = self.market.instruments()
        symbols |= set(instruments)
        quotes = {s: q for s in symbols if (q := self.market.quote(s)) is not None}
        cal = self.news()
        vol = max((self.market.volatility_ratio(s) for s in symbols), default=Decimal("1"))
        return RiskContext(
            now=now, ruleset=self.rules(), state=self.stores.state.load(self.account_id), snapshot=snap,
            instruments=instruments, quotes=quotes, policy=self.policy,
            active_kill_switches=tuple(self.stores.kill_switches.active(self.account_id)),
            reconciliation_ok=self._recon_ok, news_events=cal.events, news_calendar_available=cal.available,
            volatility_ratio=vol, automation_channel=self._adapter.capabilities.channel,
            automation_conditions_acknowledged=self.ack,
            seen_client_order_ids=self.stores.orders.seen_ids(self.account_id),
            challenge_active=self.challenge_active(),
            known_positions={pid: (d.get("side", ""), Decimal(d.get("lots", "0")), d.get("symbol", ""))
                             for pid, d in self.stores.ledger.positions(self.account_id).items()},
            closed_markets=frozenset(s for s in symbols if not getattr(self.market, "is_open", lambda _s: True)(s)),
        )

    # ------------------------------------------------------------------ order entry points
    def submit(self, order: OrderRequest) -> ExecutionOutcome:
        # `emergency` may only be set by the deterministic supervisor (required_actions -> executor)
        if order.emergency:
            order = replace(order, emergency=False)
        out = self.executor.execute(order)
        if out.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and order.intent == OrderIntent.OPEN:
            st = self.stores.state.load(self.account_id)
            if st:
                self.stores.state.save(record_activity(st, self.clock(), self.rules()))
        # a successful action changes platform state -> refresh snapshot before the next decision
        if out.report is not None:
            try:
                self._snapshot = self._adapter.get_snapshot()
                st = self.stores.state.load(self.account_id)
                upd = update_state(st, self._snapshot, self.rules())
                if upd.accepted:
                    self.stores.state.save(upd.state)
            except (BrokerError, BrokerTimeout):
                self._snapshot = None
        return out

    def submit_signal(self, sig: Signal) -> ExecutionOutcome | None:
        if sig.side == "FLAT":
            outs = [self.close_position(p.position_id, reason=sig.signal_id)
                    for p in (self._snapshot.positions if self._snapshot else ()) if p.symbol == sig.symbol]
            return outs[-1] if outs else None
        spec, q = self.market.instrument(sig.symbol), self.market.quote(sig.symbol)
        rs = self.rules()
        if spec is None or q is None or self._snapshot is None:
            return None
        lots = self.sizer.size(sig, self._snapshot, spec, q, rs.initial_balance)
        order = OrderRequest(
            client_order_id=make_client_order_id(self.account_id, sig.strategy_id, sig.signal_id, "OPEN"),
            account_id=self.account_id, symbol=sig.symbol, side=Side(sig.side), lots=lots,
            stop_loss=sig.stop_loss, take_profit=sig.take_profit, strategy_id=sig.strategy_id,
            created_at=self.clock())
        return self.submit(order)

    def close_position(self, position_id: str, reason: str = "manual") -> ExecutionOutcome:
        snap = self._snapshot
        pos = next((p for p in snap.positions if p.position_id == position_id), None) if snap else None
        if pos is None:
            raise ValueError(f"position {position_id} not in current snapshot")
        order = OrderRequest(
            client_order_id=make_client_order_id(self.account_id, "close", f"{position_id}:{reason}", "CLOSE"),
            account_id=self.account_id, symbol=pos.symbol, side=pos.side.opposite(), lots=pos.lots,
            intent=OrderIntent.CLOSE, position_id=position_id, strategy_id="close", created_at=self.clock())
        return self.submit(order)

    def cancel_order(self, broker_order_id: str) -> ExecutionOutcome:
        snap = self._snapshot
        po = next((o for o in snap.pending_orders if o.broker_order_id == broker_order_id), None) if snap else None
        if po is None:
            raise ValueError(f"pending order {broker_order_id} not found")
        order = OrderRequest(
            client_order_id=make_client_order_id(self.account_id, "cancel", broker_order_id, "CANCEL"),
            account_id=self.account_id, symbol=po.symbol, side=po.side, lots=po.lots, intent=OrderIntent.CANCEL,
            target_broker_order_id=broker_order_id, strategy_id="cancel", created_at=self.clock())
        return self.submit(order)

    def activate_kill_switch(self, reason: str, actor: str = "owner") -> None:
        self.stores.kill_switches.activate(self.account_id, KillSwitchKind.MANUAL, reason, {"actor": actor})
        self.stores.audit.record("killswitch.activate", self.account_id, {"kind": "MANUAL", "reason": reason,
                                                                          "actor": actor})

    def clear_kill_switch(self, kind: KillSwitchKind, actor: str, note: str) -> bool:
        """Manual clear only. The next tick re-raises it if the underlying condition persists."""
        ok = self.stores.kill_switches.clear(self.account_id, kind, actor, note)
        self.stores.audit.record("killswitch.clear", self.account_id, {"kind": kind.value, "actor": actor,
                                                                       "note": note, "ok": ok})
        return ok

    def set_day_start_manual(self, balance: Decimal, equity: Decimal, actor: str, note: str) -> None:
        """Owner-supplied start-of-day reference (e.g. read from the firm's own dashboard) when the
        reset was missed and the platform cannot reconstruct it. Audited."""
        st = self.stores.state.load(self.account_id)
        if st is None:
            raise ValueError("no state")
        st.day_start_balance, st.day_start_equity, st.day_start_known = Decimal(balance), Decimal(equity), True
        self.stores.state.save(st)
        self.stores.audit.record("state.day_start_manual", self.account_id, {
            "balance": str(balance), "equity": str(equity), "actor": actor, "note": note,
            "trading_date": st.trading_date.isoformat() if st.trading_date else None})

    @property
    def snapshot(self) -> AccountSnapshot | None:
        return self._snapshot

    # ------------------------------------------------------------------ internals
    def _apply_events(self, rs: RuleSet, state) -> int:
        n = 0
        last = self.stores.ledger.last_sequence(self.account_id)
        events = sorted(self._adapter.events_since(last), key=lambda e: e.sequence)
        for ev in events:
            if not self.stores.ledger.mark_event(self.account_id, ev.event_id, ev.sequence):
                continue  # duplicate delivery
            n += 1
            if ev.kind in ("position_closed", "sl_hit", "tp_hit") and ev.position_id:
                led = self.stores.ledger.positions(self.account_id).get(ev.position_id)
                if led and Decimal(led.get("lots", "0")) <= ev.lots:
                    self.stores.ledger.remove_position(self.account_id, ev.position_id)
                elif led:
                    self.stores.ledger.upsert_position(self.account_id, ev.position_id,
                                                       {**led, "lots": str(Decimal(led["lots"]) - ev.lots)})
                if ev.pnl is not None and state is not None:
                    state = record_closed_pnl(state, ev.ts, ev.pnl, rs)
                    self.stores.state.save(state)
            elif ev.kind in ("fill", "partial_fill") and ev.position_id and ev.client_order_id:
                rec = self.stores.orders.get(ev.client_order_id)
                if rec is not None:
                    prev = self.stores.ledger.positions(self.account_id).get(ev.position_id, {})
                    req = rec.get("request") or {}
                    self.stores.ledger.upsert_position(self.account_id, ev.position_id, {
                        **prev, "lots": str(ev.lots), "client_order_id": ev.client_order_id,
                        "side": prev.get("side") or req.get("side"), "symbol": prev.get("symbol") or req.get("symbol")})
            self.stores.audit.record("broker.event", self.account_id, {
                "event_id": ev.event_id, "seq": ev.sequence, "kind": ev.kind, "position_id": ev.position_id,
                "client_order_id": ev.client_order_id, "lots": str(ev.lots),
                "pnl": None if ev.pnl is None else str(ev.pnl)})
        return n

    def _raise(self, kind: KillSwitchKind, reason: str, res: TickResult, details: dict | None = None) -> None:
        if self.stores.kill_switches.activate(self.account_id, kind, reason, details):
            res.new_kill_switches.append(kind.value)
            self.stores.audit.record("killswitch.activate", self.account_id, {"kind": kind.value, "reason": reason})
