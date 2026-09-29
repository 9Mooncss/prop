"""Pure numerical functions: external/internal floors, safety buffers, worst-case exposure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from propguard.risk.models import (
    ZERO,
    AccountSnapshot,
    InstrumentSpec,
    OrderRequest,
    OrderType,
    PendingOrder,
    Position,
    Quote,
    RiskState,
    Side,
)
from propguard.risk.policy import LimitView, SafetyPolicy
from propguard.rules.ruleset import RuleSet
from propguard.rules.types import DailyLossLimit, MaxLoss

HUNDRED = Decimal("100")


def compute_buffer_frac(
    policy: SafetyPolicy,
    *,
    min_rule_confidence: Decimal = Decimal("1"),
    quote_age_s: float = 0.0,
    volatility_ratio: Decimal = Decimal("1"),
) -> Decimal:
    """Safety buffer grows with uncertainty, stale data and volatility; capped at max_buffer_frac."""
    frac = policy.base_buffer_frac
    if min_rule_confidence < Decimal("0.95"):
        frac += policy.uncertainty_buffer_frac
    if quote_age_s > policy.max_quote_age_s / 2:
        frac += policy.stale_data_buffer_frac
    if volatility_ratio > 1:
        frac += min(policy.volatility_buffer_cap, (volatility_ratio - 1) * policy.volatility_buffer_per_unit)
    return min(frac, policy.max_buffer_frac)


def _daily_reference(dl: DailyLossLimit, state: RiskState) -> Decimal:
    dsb = state.day_start_balance if state.day_start_balance is not None else state.initial_balance
    dse = state.day_start_equity if state.day_start_equity is not None else dsb
    match dl.reference:
        case "initial_balance":
            return state.initial_balance
        case "day_start_balance":
            return dsb
        case "day_start_equity":
            return dse
        case "day_start_max_balance_equity":
            return max(dsb, dse)
    raise ValueError(dl.reference)  # pragma: no cover


def daily_floor(dl: DailyLossLimit, state: RiskState) -> tuple[Decimal, Decimal]:
    """(external_floor, allowance) for the daily loss rule."""
    ref = _daily_reference(dl, state)
    base = state.initial_balance if dl.pct_of == "initial_balance" else ref
    allowance = base * dl.pct / HUNDRED  # type: ignore[operator]
    return ref - allowance, allowance


def max_loss_floor(ml: MaxLoss, state: RiskState, snapshot: AccountSnapshot | None = None) -> tuple[Decimal, Decimal]:
    """(external_floor, allowance) for the max loss rule. Includes the current value in HWM."""
    initial = state.initial_balance
    allowance = initial * ml.pct / HUNDRED
    if ml.mode == "static":
        return initial - allowance, allowance
    if ml.basis == "equity":
        hwm = state.hwm_equity or initial
        eod = state.eod_hwm_equity or initial
        cur = snapshot.equity if snapshot else None
    else:
        hwm = state.hwm_balance or initial
        eod = state.eod_hwm_balance or initial
        cur = snapshot.balance if snapshot else None
    if cur is not None:
        hwm = max(hwm, cur)
    hwm = max(hwm, initial)
    eod = max(eod, initial)
    if ml.mode == "trailing":
        return hwm - allowance, allowance
    if ml.mode == "eod_trailing":
        return eod - allowance, allowance
    if ml.mode == "trailing_lock_at_initial":
        return min(hwm - allowance, initial), allowance
    raise ValueError(ml.mode)  # pragma: no cover


def measured_value(snapshot: AccountSnapshot, on: str) -> Decimal:
    return snapshot.equity if on == "equity" else snapshot.balance


def limit_views(
    ruleset: RuleSet,
    state: RiskState,
    snapshot: AccountSnapshot,
    policy: SafetyPolicy,
    buffer_frac: Decimal,
    projected_worst_equity: Decimal | None = None,
) -> list[LimitView]:
    views: list[LimitView] = []
    dl, dl_rec = ruleset.get(DailyLossLimit)
    if dl is not None and dl.enabled:
        ext, allowance = daily_floor(dl, state)
        on = "equity" if dl.includes_floating else "balance"
        views.append(_view("daily_loss", dl_rec.rule_id if dl_rec else None, on, snapshot, ext, allowance,
                           policy, buffer_frac, projected_worst_equity))
    ml, ml_rec = ruleset.get(MaxLoss)
    if ml is not None:
        ext, allowance = max_loss_floor(ml, state, snapshot)
        views.append(_view("max_loss", ml_rec.rule_id if ml_rec else None, ml.basis, snapshot, ext, allowance,
                           policy, buffer_frac, projected_worst_equity))
    return views


def _view(name, rule_id, on, snapshot, ext_floor, allowance, policy, buffer_frac, projected):
    buffer_amount = max(allowance * buffer_frac, policy.min_buffer_abs)
    buffer_amount = min(buffer_amount, allowance)  # never beyond the allowance itself
    internal_floor = ext_floor + buffer_amount
    # Conservative: headroom is measured on the lower of balance/equity, whichever the rule uses
    # or would use once floating losses realise.
    cur = min(measured_value(snapshot, on), snapshot.equity)
    return LimitView(
        name=name,
        rule_id=rule_id,
        measured_on=on,
        current_value=measured_value(snapshot, on),
        external_floor=ext_floor,
        internal_floor=internal_floor,
        allowance=allowance,
        buffer_frac=buffer_frac,
        buffer_amount=buffer_amount,
        external_headroom=cur - ext_floor,
        internal_headroom=cur - internal_floor,
        projected_worst_value=projected,
    )


# --------------------------------------------------------------------------- exposure


@dataclass(frozen=True)
class CostModel:
    policy: SafetyPolicy

    def slippage(self, quote: Quote) -> Decimal:
        return max(quote.spread * self.policy.slippage_spread_mult,
                   quote.mid * self.policy.min_slippage_frac_of_price)

    def gap_move(self, spec: InstrumentSpec, price: Decimal) -> Decimal:
        frac = self.policy.gap_move_frac_by_class.get(spec.asset_class, Decimal("0.10"))
        return price * frac


def units(spec: InstrumentSpec, lots: Decimal) -> Decimal:
    return lots * spec.contract_size


def per_lot_risk_new(
    order: OrderRequest, spec: InstrumentSpec, quote: Quote, cost: CostModel
) -> Decimal | None:
    """Worst-case loss (account ccy) for 1.0 lot of a new OPEN order, stop-to-stop, incl. costs.

    Returns None if the stop is on the wrong side of entry (invalid stop).
    """
    slip = cost.slippage(quote)
    if order.order_type == OrderType.MARKET:
        entry = quote.ask + slip if order.side is Side.BUY else quote.bid - slip
    else:
        # limit/stop orders: assume filled at order price with slippage against us
        assert order.price is not None
        entry = order.price + slip if order.side is Side.BUY else order.price - slip
    if order.stop_loss is None:
        move = cost.gap_move(spec, entry)
    else:
        stop_slip = slip + quote.spread * cost.policy.stop_gap_mult
        if order.side is Side.BUY:
            if order.stop_loss >= entry:
                return None
            exit_px = order.stop_loss - stop_slip
            move = entry - exit_px
        else:
            if order.stop_loss <= entry:
                return None
            exit_px = order.stop_loss + stop_slip
            move = exit_px - entry
    return move * spec.contract_size * quote.quote_to_account + spec.commission_per_lot_round_turn


def position_remaining_risk(
    pos: Position, spec: InstrumentSpec, quote: Quote, cost: CostModel
) -> Decimal:
    """Additional loss from *current mark* to worst-case exit (stop or gap), incl. exit costs."""
    slip = cost.slippage(quote)
    mark = quote.bid if pos.side is Side.BUY else quote.ask
    if pos.stop_loss is None:
        move = cost.gap_move(spec, mark)
    else:
        stop_slip = slip + quote.spread * cost.policy.stop_gap_mult
        if pos.side is Side.BUY:
            move = mark - (pos.stop_loss - stop_slip)
        else:
            move = (pos.stop_loss + stop_slip) - mark
        # a stop already through the market: the exit is imminent at roughly mark +- slippage
        move = max(move, slip)
    commission_exit = spec.commission_per_lot_round_turn / 2 * pos.lots
    return move * units(spec, pos.lots) * quote.quote_to_account + commission_exit


def pending_order_risk(po: PendingOrder, spec: InstrumentSpec, quote: Quote, cost: CostModel) -> Decimal:
    req = OrderRequest(
        client_order_id=po.client_order_id or po.broker_order_id,
        account_id="",
        symbol=po.symbol,
        side=po.side,
        lots=po.lots,
        order_type=po.order_type,
        price=po.price,
        stop_loss=po.stop_loss,
    )
    per_lot = per_lot_risk_new(req, spec, quote, cost)
    if per_lot is None:  # invalid stop on a pending order -> treat as unprotected
        req = OrderRequest(**{**req.__dict__, "stop_loss": None})
        per_lot = per_lot_risk_new(req, spec, quote, cost)
    return (per_lot or ZERO) * po.lots


def notional(spec: InstrumentSpec, lots: Decimal, quote: Quote) -> Decimal:
    return units(spec, lots) * quote.mid * quote.quote_to_account


def round_down_lots(lots: Decimal, spec: InstrumentSpec) -> Decimal:
    if lots <= 0:
        return ZERO
    steps = (lots / spec.lot_step).to_integral_value(rounding=ROUND_DOWN)
    return steps * spec.lot_step


def computed_unrealized(pos: Position, spec: InstrumentSpec, quote: Quote) -> Decimal:
    mark = quote.bid if pos.side is Side.BUY else quote.ask
    diff = (mark - pos.entry_price) * pos.side.sign
    return diff * units(spec, pos.lots) * quote.quote_to_account
