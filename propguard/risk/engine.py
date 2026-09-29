"""Deterministic Risk Engine.

Pure function of its inputs (``RiskContext``). No I/O, no clocks (``now`` is an input), no LLM.
Every order the system sends goes through ``RiskEngine.evaluate`` via ``PreTradeGuard``.

Decision philosophy:
* New risk (OPEN) must pass *every* check; any uncertainty denies (fail closed).
* Risk-reducing actions (REDUCE / CLOSE / CANCEL) are allowed even under kill switches, stale
  rules or stale data, because refusing to reduce risk can itself cause a violation. They are only
  denied if they would not actually reduce exposure, or (news blackout that also bans closing)
  unless flagged ``emergency`` by the deterministic supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from propguard.risk import limits as L
from propguard.risk.models import (
    RISK_REDUCING_INTENTS,
    ZERO,
    AccountSnapshot,
    InstrumentSpec,
    OrderIntent,
    OrderRequest,
    OrderType,
    Quote,
    RiskState,
    Side,
)
from propguard.risk.policy import (
    Action,
    KillSwitchKind,
    KillSwitchTrigger,
    LimitView,
    Reason,
    RiskDecision,
    SafetyPolicy,
)
from propguard.risk.state import reset_spec
from propguard.risk.tradingday import (
    friday_cutoff_utc,
    is_weekend_window,
    minutes_since_reset,
    minutes_to_next_reset,
    trading_date,
)
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import (
    APITrading,
    DailyLossLimit,
    EAPolicy,
    InstrumentRestrictions,
    InterpretationStatus,
    Leverage,
    MaxLoss,
    MaxLotSize,
    MaxOpenPositions,
    MaxRiskPerTrade,
    NewsTrading,
    OvernightHolding,
    ProfitTarget,
    StopLossRequired,
    Tri,
    WeekendHolding,
)

ENGINE_VERSION = "risk-engine/1.0.0"


@dataclass(frozen=True)
class NewsEvent:
    ts: datetime
    currency: str
    impact: str  # low | medium | high
    title: str = ""


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    ruleset: RuleSet
    state: RiskState | None
    snapshot: AccountSnapshot | None
    instruments: dict[str, InstrumentSpec]
    quotes: dict[str, Quote]
    policy: SafetyPolicy = field(default_factory=SafetyPolicy)
    active_kill_switches: tuple[KillSwitchKind, ...] = ()
    reconciliation_ok: bool = False
    news_events: tuple[NewsEvent, ...] = ()
    news_calendar_available: bool = False
    volatility_ratio: Decimal = Decimal("1")
    automation_channel: str = "api"  # "api" | "ea" -- how orders reach the platform
    automation_conditions_acknowledged: bool = False
    seen_client_order_ids: frozenset[str] = frozenset()
    challenge_active: bool = True


class RiskEngine:
    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.default_policy = policy or SafetyPolicy()

    # ------------------------------------------------------------------ public API

    def evaluate(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        if order.client_order_id in ctx.seen_client_order_ids:
            return self._deny(order, ctx, [Reason.DUPLICATE_ORDER],
                              "client_order_id already used; refusing duplicate submission")
        if order.intent in RISK_REDUCING_INTENTS:
            return self._evaluate_reducing(order, ctx)
        if order.intent == OrderIntent.MODIFY_SL:
            return self._evaluate_modify_sl(order, ctx)
        return self._evaluate_open(order, ctx)

    def assess(self, ctx: RiskContext) -> "AccountAssessment":
        """Account-level evaluation: limits, headroom, breaches and kill switches to raise."""
        triggers: list[KillSwitchTrigger] = []
        notes: list[str] = []
        views: list[LimitView] = []
        open_risk = ZERO
        if ctx.snapshot is None or ctx.state is None:
            return AccountAssessment((), ZERO, (), (KillSwitchTrigger(
                KillSwitchKind.RECONNECT_UNCERTAIN_STATE, "no account snapshot/state available"),), ("no state",))
        snap, state = ctx.snapshot, ctx.state
        policy = ctx.policy
        age = (ctx.now - snap.ts).total_seconds()
        if age > policy.max_snapshot_age_s:
            notes.append(f"snapshot age {age:.0f}s > {policy.max_snapshot_age_s}s")
        consistency = self._consistency(ctx)
        for reason, msg in consistency:
            kind = {
                Reason.BALANCE_INCONSISTENT: KillSwitchKind.CONTRADICTORY_BALANCES,
                Reason.CALC_MISMATCH: KillSwitchKind.CALCULATION_MISMATCH,
                Reason.UNKNOWN_POSITION: KillSwitchKind.UNEXPECTED_MANUAL_TRADE,
            }.get(reason, KillSwitchKind.ACCOUNT_STATE_INCONSISTENT)
            triggers.append(KillSwitchTrigger(kind, msg))
        stale_syms = [p.symbol for p in snap.positions if self._quote_age(ctx, p.symbol) > policy.max_quote_age_s * 3]
        if stale_syms:
            triggers.append(KillSwitchTrigger(KillSwitchKind.STALE_MARKET_DATA,
                                              f"stale quotes for open positions: {sorted(set(stale_syms))}"))
        open_risk, missing = self._open_risk(ctx)
        buffer_frac = self._buffer(ctx, None)
        views = L.limit_views(ctx.ruleset, state, snap, policy, buffer_frac, snap.equity - open_risk)
        for v in views:
            if v.external_headroom <= 0:
                triggers.append(KillSwitchTrigger(KillSwitchKind.EXTERNAL_LIMIT_BREACHED,
                                                  f"{v.name} external floor {v.external_floor} reached",
                                                  {"limit": v.to_dict()}))
            elif v.internal_headroom <= 0:
                triggers.append(KillSwitchTrigger(KillSwitchKind.INTERNAL_DRAWDOWN_LIMIT,
                                                  f"{v.name} internal floor {v.internal_floor} reached",
                                                  {"limit": v.to_dict()}))
        return AccountAssessment(tuple(views), open_risk, tuple(missing), tuple(triggers), tuple(notes))

    def required_actions(self, ctx: RiskContext) -> list[OrderRequest]:
        """Deterministic protective actions (flatten before prohibited holding periods, emergency
        exits when the *external* floor is threatened by open risk). Never opens risk."""
        actions: list[OrderRequest] = []
        if ctx.snapshot is None or ctx.state is None:
            return actions
        rs, policy, now = ctx.ruleset, ctx.policy, ctx.now
        reasons: list[str] = []
        wk, _ = rs.get(WeekendHolding)
        if wk is None or wk.allowed != Tri.ALLOWED:
            tz = wk.tz if wk else "UTC"
            close_by = wk.close_by if wk else "21:00"
            cutoff = friday_cutoff_utc(now, close_by, tz)
            if cutoff - timedelta(minutes=policy.flatten_lead_min) <= now < cutoff + timedelta(hours=60):
                reasons.append("weekend_holding_not_allowed")
        on, _ = rs.get(OvernightHolding)
        if on is not None and on.allowed != Tri.ALLOWED:
            rt, tz = reset_spec(rs)
            if minutes_to_next_reset(now, rt, tz) <= policy.flatten_lead_min:
                reasons.append("overnight_holding_not_allowed")
        emergency = False
        views = L.limit_views(rs, ctx.state, ctx.snapshot, policy, self._buffer(ctx, None))
        for v in views:
            # current value at/below the internal floor -> flatten (risk-reducing only)
            if v.internal_headroom <= 0:
                reasons.append(f"{v.name}_internal_floor")
                emergency = True
        if not reasons:
            return actions
        for p in ctx.snapshot.positions:
            actions.append(OrderRequest(
                client_order_id=f"flatten:{p.position_id}:{now.strftime('%Y%m%d%H%M')}",
                account_id=ctx.snapshot.account_id,
                symbol=p.symbol,
                side=p.side.opposite(),
                lots=p.lots,
                intent=OrderIntent.CLOSE,
                position_id=p.position_id,
                strategy_id="supervisor:" + ",".join(reasons),
                created_at=now,
                emergency=emergency,
            ))
        for po in ctx.snapshot.pending_orders:
            actions.append(OrderRequest(
                client_order_id=f"cancel:{po.broker_order_id}:{now.strftime('%Y%m%d%H%M')}",
                account_id=ctx.snapshot.account_id,
                symbol=po.symbol,
                side=po.side,
                lots=po.lots,
                intent=OrderIntent.CANCEL,
                target_broker_order_id=po.broker_order_id,
                strategy_id="supervisor:" + ",".join(reasons),
                created_at=now,
                emergency=emergency,
            ))
        return actions

    # ------------------------------------------------------------------ OPEN

    def _evaluate_open(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        reasons: list[Reason] = []
        msgs: list[str] = []
        rule_ids: list[str] = []
        policy, rs, now = ctx.policy, ctx.ruleset, ctx.now

        def fail(reason: Reason, msg: str, rid: str | None = None) -> None:
            reasons.append(reason)
            msgs.append(msg)
            if rid:
                rule_ids.append(rid)

        # -- hard gates (system state)
        if ctx.active_kill_switches:
            fail(Reason.KILL_SWITCH_ACTIVE, f"kill switch active: {[k.value for k in ctx.active_kill_switches]}")
        if not ctx.challenge_active:
            fail(Reason.CHALLENGE_NOT_ACTIVE, "challenge/account is not active")
        if ctx.snapshot is None or ctx.state is None:
            fail(Reason.STATE_MISSING, "no account state available")
            return self._deny(order, ctx, reasons, "; ".join(msgs), rule_ids)
        snap, state = ctx.snapshot, ctx.state
        if order.lots <= 0:
            fail(Reason.INVALID_SIZE, "lots must be positive")
        snap_age = (now - snap.ts).total_seconds()
        if snap_age > policy.max_snapshot_age_s or snap_age < -5:
            fail(Reason.STATE_STALE, f"account snapshot age {snap_age:.1f}s exceeds {policy.max_snapshot_age_s}s")
        if not ctx.reconciliation_ok:
            fail(Reason.RECONCILIATION_NOT_OK, "broker/platform state not reconciled")
        for reason, msg in self._consistency(ctx):
            fail(reason, msg)
        if not state.day_start_known:
            fail(Reason.DAY_STATE_UNCERTAIN, "start-of-day reference unknown (missed daily reset)")
        rt_, tz_ = reset_spec(rs)
        if state.trading_date != trading_date(now, rt_, tz_):
            fail(Reason.DAY_STATE_UNCERTAIN,
                 f"risk state is for trading day {state.trading_date}, current is {trading_date(now, rt_, tz_)}")

        # -- rules freshness / certainty
        self._check_rules(ctx, fail)

        # -- instrument / quote
        spec = ctx.instruments.get(order.symbol)
        quote = ctx.quotes.get(order.symbol)
        if spec is None:
            fail(Reason.UNKNOWN_INSTRUMENT, f"no instrument spec for {order.symbol}")
        if quote is None:
            fail(Reason.NO_QUOTE, f"no quote for {order.symbol}")
        elif quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            fail(Reason.INVALID_QUOTE, f"invalid quote bid={quote.bid} ask={quote.ask}")
        else:
            qa = (now - quote.ts).total_seconds()
            if qa > policy.max_quote_age_s or qa < -5:
                fail(Reason.STALE_MARKET_DATA, f"quote age {qa:.1f}s exceeds {policy.max_quote_age_s}s")
        if order.order_type != OrderType.MARKET and order.price is None:
            fail(Reason.INVALID_SIZE, "limit/stop order requires price")

        # -- conduct / timing rules
        self._check_conduct(order, ctx, spec, fail)

        if reasons or spec is None or quote is None:
            return self._deny(order, ctx, reasons, "; ".join(msgs), rule_ids)

        # -- numerical limits
        cost = L.CostModel(policy)
        buffer_frac = self._buffer(ctx, quote)
        open_risk, missing = self._open_risk(ctx)
        if missing:
            return self._deny(order, ctx, [Reason.NO_QUOTE],
                              f"cannot value open exposure, missing quotes/specs: {missing}", rule_ids)
        per_lot = L.per_lot_risk_new(order, spec, quote, cost)
        if per_lot is None:
            return self._deny(order, ctx, [Reason.INVALID_STOP_LOSS], "stop loss on wrong side of entry", rule_ids)
        sl_rule, sl_rec = rs.get(StopLossRequired)
        if order.stop_loss is None and (policy.require_stop_loss or (sl_rule and sl_rule.required)):
            return self._deny(order, ctx, [Reason.STOP_LOSS_REQUIRED],
                              "new positions must carry a stop loss (internal policy / firm rule)",
                              rule_ids + ([sl_rec.rule_id] if sl_rec else []))

        base_views = L.limit_views(rs, state, snap, policy, buffer_frac)
        if not base_views:
            return self._deny(order, ctx, [Reason.RULES_INCOMPLETE], "no drawdown rules in rule set", rule_ids)
        for v in base_views:
            if v.rule_id:
                rule_ids.append(v.rule_id)
            if v.external_headroom <= 0:
                reasons.append(Reason.EXTERNAL_DAILY_LIMIT_BREACHED if v.name == "daily_loss"
                               else Reason.EXTERNAL_MAX_LOSS_BREACHED)
                msgs.append(f"{v.name}: external floor {v.external_floor} reached (value {v.current_value})")
        if reasons:
            return self._deny(order, ctx, reasons, "; ".join(msgs), rule_ids, views=base_views)

        # capacity in money for new risk under each internal floor
        worst_now = snap.equity - open_risk
        caps: dict[str, Decimal] = {}
        for v in base_views:
            caps[v.name] = worst_now - v.internal_floor
        risk_cap_frac = policy.max_risk_per_trade_frac
        mr, mr_rec = rs.get(MaxRiskPerTrade)
        if mr is not None:
            risk_cap_frac = min(risk_cap_frac, mr.pct / 100)
            rule_ids.append(mr_rec.rule_id)
        pt, _ = rs.get(ProfitTarget)
        if state.target_reached or (pt and snap.balance >= state.initial_balance * (1 + pt.pct / 100)):
            risk_cap_frac = min(risk_cap_frac, policy.post_target_risk_frac)
            msgs.append("profit target reached: risk per trade capped")
        caps["risk_per_trade"] = state.initial_balance * risk_cap_frac

        max_lots_by = {k: (c / per_lot if c > 0 else ZERO) for k, c in caps.items()}
        ml_rule, ml_rec = rs.get(MaxLotSize)
        if ml_rule is not None:
            rule_ids.append(ml_rec.rule_id)
            if ml_rule.scope == "per_order":
                max_lots_by["max_lot_size"] = ml_rule.max_lots
            else:
                existing = sum((p.lots for p in snap.positions
                                if ml_rule.scope == "total" or p.symbol == order.symbol), ZERO)
                max_lots_by["max_lot_size"] = max(ZERO, ml_rule.max_lots - existing)
        lev, lev_rec = rs.get(Leverage)
        if lev is not None and spec.asset_class in lev.max_by_class:
            rule_ids.append(lev_rec.rule_id)
            max_notional = lev.max_by_class[spec.asset_class] * snap.equity
            used = sum((L.notional(ctx.instruments[p.symbol], p.lots, ctx.quotes[p.symbol])
                        for p in snap.positions if p.symbol in ctx.quotes and p.symbol in ctx.instruments), ZERO)
            per_lot_notional = L.notional(spec, Decimal("1"), quote)
            max_lots_by["leverage"] = max(ZERO, (max_notional - used) / per_lot_notional)
        mop, mop_rec = rs.get(MaxOpenPositions)
        if mop is not None and len(snap.positions) + len(snap.pending_orders) >= mop.max_positions:
            return self._deny(order, ctx, [Reason.MAX_OPEN_POSITIONS],
                              f"open positions+orders {len(snap.positions) + len(snap.pending_orders)} "
                              f">= {mop.max_positions}", rule_ids + [mop_rec.rule_id])

        max_allowed = L.round_down_lots(min(max_lots_by.values()), spec)
        binding = min(max_lots_by, key=lambda k: max_lots_by[k])
        new_risk = per_lot * order.lots
        proj_views = L.limit_views(rs, state, snap, policy, buffer_frac, worst_now - new_risk)

        if order.lots <= max_allowed:
            return self._decision(Action.ALLOW, order, ctx, [Reason.OK],
                                  "; ".join(["within internal limits"] + msgs), rule_ids, proj_views,
                                  buffer_frac, order.lots, max_allowed, new_risk, open_risk, quote)
        reason = {
            "daily_loss": Reason.INTERNAL_DAILY_LIMIT,
            "max_loss": Reason.INTERNAL_MAX_LOSS,
            "risk_per_trade": Reason.TARGET_REACHED_RISK_CAP if state.target_reached else Reason.RISK_PER_TRADE,
            "max_lot_size": Reason.MAX_LOT_SIZE,
            "leverage": Reason.LEVERAGE,
        }[binding]
        expl = (f"requested {order.lots} lots > max allowed {max_allowed} (binding constraint: {binding}); "
                f"per-lot worst-case risk {per_lot:.2f}, open risk {open_risk:.2f}")
        if policy.allow_downsize and max_allowed >= spec.min_lot:
            return self._decision(Action.MODIFY, order, ctx, [reason], expl + "; downsized", rule_ids, proj_views,
                                  buffer_frac, max_allowed, max_allowed, per_lot * max_allowed, open_risk, quote)
        if max_allowed < spec.min_lot:
            expl += f"; below minimum lot {spec.min_lot}"
        return self._decision(Action.DENY, order, ctx, [reason], expl, rule_ids, proj_views,
                              buffer_frac, ZERO, max_allowed, new_risk, open_risk, quote)

    # ------------------------------------------------------------------ REDUCE / CLOSE / CANCEL

    def _evaluate_reducing(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        snap = ctx.snapshot
        if snap is None:
            # without state we cannot prove the action reduces risk; still, the platform will reject
            # a close for a non-existing position. Allow cancels/closes by id only.
            if order.intent == OrderIntent.CANCEL or order.position_id:
                return self._decision(Action.ALLOW, order, ctx, [Reason.OK],
                                      "risk-reducing action allowed without state (by id)", [], (),
                                      None, order.lots, None, ZERO, None, None)
            return self._deny(order, ctx, [Reason.STATE_MISSING], "no state to validate risk reduction")
        if order.intent == OrderIntent.CANCEL:
            if not any(po.broker_order_id == order.target_broker_order_id for po in snap.pending_orders):
                return self._deny(order, ctx, [Reason.ORDER_NOT_FOUND],
                                  f"pending order {order.target_broker_order_id} not found")
            return self._decision(Action.ALLOW, order, ctx, [Reason.OK], "cancel reduces pending exposure",
                                  [], (), None, order.lots, None, ZERO, None, None)
        pos = next((p for p in snap.positions if p.position_id == order.position_id), None)
        if pos is None:
            return self._deny(order, ctx, [Reason.POSITION_NOT_FOUND], f"position {order.position_id} not found")
        if order.side != pos.side.opposite() or order.lots <= 0 or order.lots > pos.lots:
            return self._deny(order, ctx, [Reason.NOT_RISK_REDUCING],
                              f"order side/size does not reduce position {pos.position_id}")
        if order.intent == OrderIntent.CLOSE and order.lots != pos.lots:
            return self._deny(order, ctx, [Reason.NOT_RISK_REDUCING], "CLOSE must be for full position size")
        # News blackout that also forbids closing -> deny unless emergency
        nt, nt_rec = ctx.ruleset.get(NewsTrading)
        spec = ctx.instruments.get(order.symbol)
        if nt is not None and nt.applies_to == "open_and_close" and nt.allowed != Tri.ALLOWED and spec:
            ev = self._news_hit(ctx, spec, nt)
            if ev is not None and not order.emergency:
                return self._deny(order, ctx, [Reason.NEWS_BLACKOUT],
                                  f"closing inside news blackout ({ev.title} {ev.ts.isoformat()}) is restricted "
                                  f"by firm rule; supervisor may override only in emergency",
                                  [nt_rec.rule_id])
        msg = "risk-reducing action allowed"
        if ctx.active_kill_switches:
            msg += " (kill switch active: reductions remain permitted)"
        return self._decision(Action.ALLOW, order, ctx, [Reason.OK], msg, [], (), None, order.lots, None,
                              ZERO, None, ctx.quotes.get(order.symbol))

    def _evaluate_modify_sl(self, order: OrderRequest, ctx: RiskContext) -> RiskDecision:
        snap = ctx.snapshot
        pos = next((p for p in snap.positions if p.position_id == order.position_id), None) if snap else None
        if pos is None:
            return self._deny(order, ctx, [Reason.POSITION_NOT_FOUND], "position not found")
        new_sl = order.stop_loss
        if new_sl is None:
            return self._deny(order, ctx, [Reason.STOP_LOSS_REQUIRED], "cannot remove stop loss")
        tighter = pos.stop_loss is None or (
            new_sl >= pos.stop_loss if pos.side is Side.BUY else new_sl <= pos.stop_loss)
        if not tighter:
            return self._deny(order, ctx, [Reason.NOT_RISK_REDUCING], "moving stop further away increases risk")
        return self._decision(Action.ALLOW, order, ctx, [Reason.OK], "stop tightened", [], (), None,
                              None, None, ZERO, None, None)

    # ------------------------------------------------------------------ helpers

    def _check_rules(self, ctx: RiskContext, fail) -> None:
        rs, policy, now = ctx.ruleset, ctx.policy, ctx.now
        dl, _ = rs.get(DailyLossLimit)
        ml, _ = rs.get(MaxLoss)
        if dl is None or ml is None:
            fail(Reason.RULES_INCOMPLETE, "daily_loss_limit and max_loss rules are mandatory")
        if rs.pending_critical_change:
            fail(Reason.RULE_CHANGE_PENDING, "critical rule change detected and not yet verified")
        for r in rs.non_confirmed_critical():
            if r.status == InterpretationStatus.CONFLICT:
                fail(Reason.RULE_CONFLICT, f"conflicting sources for {r.kind}", r.rule_id)
            else:
                fail(Reason.RULE_UNCERTAIN, f"{r.kind} interpretation {r.status.value}", r.rule_id)
        oldest = rs.oldest_critical_verification()
        if oldest is None:
            fail(Reason.RULES_STALE, "critical rules have no verification timestamp")
        elif now - oldest > timedelta(hours=policy.max_rules_age_h):
            fail(Reason.RULES_STALE, f"critical rules last verified {oldest.isoformat()} "
                                     f"(> {policy.max_rules_age_h}h)")

    def _check_conduct(self, order: OrderRequest, ctx: RiskContext, spec: InstrumentSpec | None, fail) -> None:
        rs, policy, now = ctx.ruleset, ctx.policy, ctx.now
        # automation permission
        auto_cls = EAPolicy if ctx.automation_channel == "ea" else APITrading
        ap, ap_rec = rs.get(auto_cls)
        if ap is None:
            fail(Reason.AUTOMATION_NOT_PERMITTED, f"no {auto_cls.kind} rule: automated trading not confirmed")
        elif ap.allowed == Tri.CONDITIONAL:
            if not (policy.treat_conditional_automation_as_allowed and ctx.automation_conditions_acknowledged):
                fail(Reason.AUTOMATION_NOT_PERMITTED,
                     f"{auto_cls.kind} is CONDITIONAL ({ap.conditions}); conditions not acknowledged",
                     ap_rec.rule_id)
        elif ap.allowed != Tri.ALLOWED:
            fail(Reason.AUTOMATION_NOT_PERMITTED, f"{auto_cls.kind} is {ap.allowed.value}", ap_rec.rule_id)
        # instruments
        ir, ir_rec = rs.get(InstrumentRestrictions)
        if ir is not None and spec is not None:
            if order.symbol in ir.prohibited_symbols or (
                    ir.allowed_classes is not None and spec.asset_class not in ir.allowed_classes):
                fail(Reason.INSTRUMENT_NOT_ALLOWED, f"{order.symbol} not allowed", ir_rec.rule_id)
        # rollover guard
        rt, tz = reset_spec(rs)
        if (minutes_to_next_reset(now, rt, tz) < policy.rollover_guard_min
                or minutes_since_reset(now, rt, tz) < policy.rollover_guard_min):
            fail(Reason.ROLLOVER_GUARD, f"within {policy.rollover_guard_min} min of daily reset {rt} {tz}")
        # weekend
        wk, wk_rec = rs.get(WeekendHolding)
        if wk is None or wk.allowed != Tri.ALLOWED:
            wtz = wk.tz if wk else "UTC"
            close_by = wk.close_by if wk else "21:00"
            cutoff = friday_cutoff_utc(now, close_by, wtz)
            if is_weekend_window(now, close_by, wtz) or (
                    cutoff - timedelta(minutes=policy.weekend_guard_min) <= now < cutoff):
                fail(Reason.WEEKEND_HOLDING, "weekend holding not allowed/unknown and Friday cutoff is near",
                     wk_rec.rule_id if wk_rec else None)
        # overnight
        on, on_rec = rs.get(OvernightHolding)
        if on is not None and on.allowed != Tri.ALLOWED:
            if minutes_to_next_reset(now, rt, tz) < policy.overnight_guard_min:
                fail(Reason.OVERNIGHT_HOLDING, "overnight holding not allowed and reset is near", on_rec.rule_id)
        # news
        nt, nt_rec = rs.get(NewsTrading)
        if nt is None or nt.allowed != Tri.ALLOWED:
            if not ctx.news_calendar_available:
                fail(Reason.NEWS_CALENDAR_UNAVAILABLE,
                     "news trading not confirmed allowed and no news calendar available",
                     nt_rec.rule_id if nt_rec else None)
            elif spec is not None:
                ev = self._news_hit(ctx, spec, nt)
                if ev is not None:
                    fail(Reason.NEWS_BLACKOUT, f"news blackout: {ev.title} {ev.currency} {ev.ts.isoformat()}",
                         nt_rec.rule_id if nt_rec else None)

    def _news_hit(self, ctx: RiskContext, spec: InstrumentSpec, nt: NewsTrading | None) -> NewsEvent | None:
        policy = ctx.policy
        if nt is None or nt.allowed == Tri.UNKNOWN:
            before = after = policy.unknown_news_blackout_min
            impacts = {"high"}
        else:
            before, after = nt.blackout_before_min, nt.blackout_after_min
            impacts = set(nt.impact_levels)
        before += policy.news_extra_buffer_min
        after += policy.news_extra_buffer_min
        ccys = {c for c in (spec.base_ccy, spec.quote_ccy) if c}
        for ev in ctx.news_events:
            if ev.impact not in impacts or (ccys and ev.currency not in ccys):
                continue
            if ev.ts - timedelta(minutes=before) <= ctx.now <= ev.ts + timedelta(minutes=after):
                return ev
        return None

    def _consistency(self, ctx: RiskContext) -> list[tuple[Reason, str]]:
        out: list[tuple[Reason, str]] = []
        snap, policy = ctx.snapshot, ctx.policy
        if snap is None:
            return out
        tol = max(policy.balance_tolerance_abs, abs(snap.balance) * policy.balance_tolerance_frac)
        reported = [p.unrealized_pnl for p in snap.positions]
        if all(r is not None for r in reported):
            implied = snap.balance + sum((r + p.swap + p.commission for r, p in zip(reported, snap.positions)), ZERO)
            if abs(implied - snap.equity) > tol:
                out.append((Reason.BALANCE_INCONSISTENT,
                            f"equity {snap.equity} != balance + floating {implied} (tol {tol})"))
        computed_total, reported_total, comparable = ZERO, ZERO, True
        for p in snap.positions:
            spec, q = ctx.instruments.get(p.symbol), ctx.quotes.get(p.symbol)
            if spec is None or q is None or p.unrealized_pnl is None:
                comparable = False
                continue
            computed_total += L.computed_unrealized(p, spec, q)
            reported_total += p.unrealized_pnl
        if comparable and snap.positions:
            ctol = max(tol, abs(snap.equity) * policy.calc_tolerance_frac)
            if abs(computed_total - reported_total) > ctol:
                out.append((Reason.CALC_MISMATCH,
                            f"computed floating {computed_total:.2f} vs platform {reported_total:.2f}"))
        unknown = [p.position_id for p in snap.positions if p.client_order_id is None]
        if unknown:
            out.append((Reason.UNKNOWN_POSITION, f"positions not opened by this system: {unknown}"))
        return out

    def _open_risk(self, ctx: RiskContext) -> tuple[Decimal, list[str]]:
        snap = ctx.snapshot
        cost = L.CostModel(ctx.policy)
        total, missing = ZERO, []
        if snap is None:
            return total, missing
        for p in snap.positions:
            spec, q = ctx.instruments.get(p.symbol), ctx.quotes.get(p.symbol)
            if spec is None or q is None:
                missing.append(p.symbol)
                continue
            total += L.position_remaining_risk(p, spec, q, cost)
        for po in snap.pending_orders:
            spec, q = ctx.instruments.get(po.symbol), ctx.quotes.get(po.symbol)
            if spec is None or q is None:
                missing.append(po.symbol)
                continue
            total += L.pending_order_risk(po, spec, q, cost)
        return total, missing

    def _quote_age(self, ctx: RiskContext, symbol: str) -> float:
        q = ctx.quotes.get(symbol)
        return float("inf") if q is None else (ctx.now - q.ts).total_seconds()

    def _buffer(self, ctx: RiskContext, quote: Quote | None) -> Decimal:
        confs = [r.confidence for r in ctx.ruleset.rules if r.kind in ("daily_loss_limit", "max_loss")]
        qa = (ctx.now - quote.ts).total_seconds() if quote else 0.0
        return L.compute_buffer_frac(ctx.policy, min_rule_confidence=min(confs) if confs else Decimal("0"),
                                     quote_age_s=qa, volatility_ratio=ctx.volatility_ratio)

    def _freshness(self, ctx: RiskContext, quote: Quote | None) -> dict:
        oldest = ctx.ruleset.oldest_critical_verification()
        return {
            "snapshot_age_s": (ctx.now - ctx.snapshot.ts).total_seconds() if ctx.snapshot else None,
            "quote_age_s": (ctx.now - quote.ts).total_seconds() if quote else None,
            "rules_verified_at": oldest.isoformat() if oldest else None,
            "reconciliation_ok": ctx.reconciliation_ok,
            "day_start_known": ctx.state.day_start_known if ctx.state else None,
            "kill_switches": [k.value for k in ctx.active_kill_switches],
        }

    def _deny(self, order, ctx, reasons, explanation, rule_ids=(), views=()) -> RiskDecision:
        return self._decision(Action.DENY, order, ctx, reasons, explanation, rule_ids, views, None,
                              ZERO, None, None, None, ctx.quotes.get(order.symbol))

    def _decision(self, action, order, ctx, reasons, explanation, rule_ids, views, buffer_frac,
                  approved, max_allowed, new_risk, open_risk, quote) -> RiskDecision:
        return RiskDecision(
            action=action,
            reasons=tuple(reasons),
            explanation=explanation,
            client_order_id=order.client_order_id,
            rule_ids=tuple(dict.fromkeys(r for r in rule_ids if r)),
            limits=tuple(views),
            buffer_frac=buffer_frac,
            requested_lots=order.lots,
            approved_lots=approved,
            max_allowed_lots=max_allowed,
            new_order_risk=new_risk,
            open_risk=open_risk,
            freshness=self._freshness(ctx, quote),
            ruleset_id=ctx.ruleset.ruleset_id,
            engine_version=ENGINE_VERSION,
            evaluated_at=ctx.now.isoformat(),
        )


@dataclass(frozen=True)
class AccountAssessment:
    limits: tuple[LimitView, ...]
    open_risk: Decimal
    unvalued_symbols: tuple[str, ...]
    kill_switch_triggers: tuple[KillSwitchTrigger, ...]
    notes: tuple[str, ...]
