"""Internal safety policy, reason codes, kill-switch kinds and decision objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Reason(StrEnum):
    OK = "OK"
    # system / state
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    STATE_STALE = "STATE_STALE"
    STATE_MISSING = "STATE_MISSING"
    OUT_OF_ORDER_STATE = "OUT_OF_ORDER_STATE"
    BALANCE_INCONSISTENT = "BALANCE_INCONSISTENT"
    CALC_MISMATCH = "CALC_MISMATCH"
    RECONCILIATION_NOT_OK = "RECONCILIATION_NOT_OK"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    DAY_STATE_UNCERTAIN = "DAY_STATE_UNCERTAIN"
    ROLLOVER_GUARD = "ROLLOVER_GUARD"
    # market data
    NO_QUOTE = "NO_QUOTE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    INVALID_QUOTE = "INVALID_QUOTE"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    # rules
    RULES_STALE = "RULES_STALE"
    RULES_INCOMPLETE = "RULES_INCOMPLETE"
    RULE_UNCERTAIN = "RULE_UNCERTAIN"
    RULE_CONFLICT = "RULE_CONFLICT"
    RULE_CHANGE_PENDING = "RULE_CHANGE_PENDING"
    CHALLENGE_NOT_ACTIVE = "CHALLENGE_NOT_ACTIVE"
    # limits
    EXTERNAL_DAILY_LIMIT_BREACHED = "EXTERNAL_DAILY_LIMIT_BREACHED"
    EXTERNAL_MAX_LOSS_BREACHED = "EXTERNAL_MAX_LOSS_BREACHED"
    INTERNAL_DAILY_LIMIT = "INTERNAL_DAILY_LIMIT"
    INTERNAL_MAX_LOSS = "INTERNAL_MAX_LOSS"
    RISK_PER_TRADE = "RISK_PER_TRADE"
    STOP_LOSS_REQUIRED = "STOP_LOSS_REQUIRED"
    INVALID_STOP_LOSS = "INVALID_STOP_LOSS"
    MAX_LOT_SIZE = "MAX_LOT_SIZE"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    LEVERAGE = "LEVERAGE"
    SIZE_BELOW_MIN = "SIZE_BELOW_MIN"
    INVALID_SIZE = "INVALID_SIZE"
    # conduct / timing
    INSTRUMENT_NOT_ALLOWED = "INSTRUMENT_NOT_ALLOWED"
    AUTOMATION_NOT_PERMITTED = "AUTOMATION_NOT_PERMITTED"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"
    NEWS_CALENDAR_UNAVAILABLE = "NEWS_CALENDAR_UNAVAILABLE"
    WEEKEND_HOLDING = "WEEKEND_HOLDING"
    OVERNIGHT_HOLDING = "OVERNIGHT_HOLDING"
    TARGET_REACHED_RISK_CAP = "TARGET_REACHED_RISK_CAP"
    # risk-reducing validation
    NOT_RISK_REDUCING = "NOT_RISK_REDUCING"
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"


class KillSwitchKind(StrEnum):
    MANUAL = "MANUAL"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CONTRADICTORY_BALANCES = "CONTRADICTORY_BALANCES"
    STALE_RULES = "STALE_RULES"
    RULE_VERIFICATION_FAILURE = "RULE_VERIFICATION_FAILURE"
    INTERNAL_DRAWDOWN_LIMIT = "INTERNAL_DRAWDOWN_LIMIT"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    UNEXPECTED_MANUAL_TRADE = "UNEXPECTED_MANUAL_TRADE"
    RECONNECT_UNCERTAIN_STATE = "RECONNECT_UNCERTAIN_STATE"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    ACCOUNT_STATE_INCONSISTENT = "ACCOUNT_STATE_INCONSISTENT"
    CALCULATION_MISMATCH = "CALCULATION_MISMATCH"
    AMBIGUOUS_EXECUTION_EVENT = "AMBIGUOUS_EXECUTION_EVENT"
    EXTERNAL_LIMIT_BREACHED = "EXTERNAL_LIMIT_BREACHED"


@dataclass(frozen=True)
class SafetyPolicy:
    """Configurable internal safety margins. Defaults are deliberately conservative.

    Buffer fractions are fractions of the *external allowance* (e.g. of the 5% daily loss amount).
    internal_floor = external_floor + buffer_frac * allowance.
    """

    base_buffer_frac: Decimal = Decimal("0.30")
    uncertainty_buffer_frac: Decimal = Decimal("0.20")  # added when drawdown-rule confidence < 0.95
    stale_data_buffer_frac: Decimal = Decimal("0.10")  # added when quotes older than half the max age
    volatility_buffer_per_unit: Decimal = Decimal("0.10")  # per unit of (vol_ratio - 1), capped below
    volatility_buffer_cap: Decimal = Decimal("0.25")
    max_buffer_frac: Decimal = Decimal("0.90")
    min_buffer_abs: Decimal = Decimal("0")  # money floor for buffer
    slippage_spread_mult: Decimal = Decimal("1.0")  # assumed slippage = mult * current spread (each side)
    min_slippage_frac_of_price: Decimal = Decimal("0.00005")  # 0.5bp minimum assumed slippage
    gap_move_frac_by_class: dict[str, Decimal] = field(
        default_factory=lambda: {
            "fx": Decimal("0.02"),
            "metals": Decimal("0.04"),
            "indices": Decimal("0.05"),
            "energy": Decimal("0.08"),
            "crypto": Decimal("0.15"),
            "stocks": Decimal("0.15"),
        }
    )
    stop_gap_mult: Decimal = Decimal("1.0")  # extra stop slippage assumption in spreads
    require_stop_loss: bool = True
    max_risk_per_trade_frac: Decimal = Decimal("0.01")  # of initial balance
    post_target_risk_frac: Decimal = Decimal("0.001")  # after profit target is reached
    max_quote_age_s: int = 5
    max_snapshot_age_s: int = 15
    max_rules_age_h: int = 24
    balance_tolerance_abs: Decimal = Decimal("1.00")
    balance_tolerance_frac: Decimal = Decimal("0.0005")
    calc_tolerance_frac: Decimal = Decimal("0.002")
    rollover_guard_min: int = 10  # no new risk this many minutes before/after daily reset
    weekend_guard_min: int = 120  # no new risk this long before Friday cutoff if weekend holding not allowed
    overnight_guard_min: int = 60
    flatten_lead_min: int = 30  # supervisor flattens this long before a holding cutoff
    news_extra_buffer_min: int = 5  # added on both sides of firm blackout
    unknown_news_blackout_min: int = 15  # default blackout when firm news policy is UNKNOWN
    allow_downsize: bool = False  # MODIFY a too-large order to max allowed size instead of DENY
    treat_conditional_automation_as_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


@dataclass(frozen=True)
class LimitView:
    name: str  # daily_loss | max_loss
    rule_id: str | None
    measured_on: str
    current_value: Decimal
    external_floor: Decimal
    internal_floor: Decimal
    allowance: Decimal  # external allowance amount (e.g. 5% of initial)
    buffer_frac: Decimal
    buffer_amount: Decimal
    external_headroom: Decimal  # current - external floor
    internal_headroom: Decimal  # current - internal floor
    projected_worst_value: Decimal | None = None  # after open risk (and new order if evaluated)

    @property
    def used_frac_of_external(self) -> Decimal:
        if self.allowance <= 0:
            return Decimal("0")
        used = self.allowance - self.external_headroom
        return max(Decimal("0"), used / self.allowance)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["used_frac_of_external"] = self.used_frac_of_external
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


class Action(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    MODIFY = "MODIFY"  # allowed with reduced size


@dataclass(frozen=True)
class RiskDecision:
    action: Action
    reasons: tuple[Reason, ...]
    explanation: str
    client_order_id: str
    rule_ids: tuple[str, ...] = ()
    limits: tuple[LimitView, ...] = ()
    buffer_frac: Decimal | None = None
    requested_lots: Decimal | None = None
    approved_lots: Decimal | None = None
    max_allowed_lots: Decimal | None = None
    new_order_risk: Decimal | None = None
    open_risk: Decimal | None = None
    freshness: dict[str, Any] = field(default_factory=dict)
    ruleset_id: str = ""
    engine_version: str = ""
    evaluated_at: str = ""

    @property
    def allowed(self) -> bool:
        return self.action in (Action.ALLOW, Action.MODIFY)

    @property
    def primary_reason(self) -> Reason:
        return self.reasons[0] if self.reasons else Reason.OK

    def to_dict(self) -> dict[str, Any]:
        def conv(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, (list, tuple)):
                return [conv(x) for x in v]
            if isinstance(v, LimitView):
                return v.to_dict()
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            return v

        return {
            "action": self.action.value,
            "allowed": self.allowed,
            "reasons": [r.value for r in self.reasons],
            "explanation": self.explanation,
            "client_order_id": self.client_order_id,
            "rule_ids": list(self.rule_ids),
            "limits": [lv.to_dict() for lv in self.limits],
            "buffer_frac": conv(self.buffer_frac),
            "requested_lots": conv(self.requested_lots),
            "approved_lots": conv(self.approved_lots),
            "max_allowed_lots": conv(self.max_allowed_lots),
            "new_order_risk": conv(self.new_order_risk),
            "open_risk": conv(self.open_risk),
            "freshness": conv(self.freshness),
            "ruleset_id": self.ruleset_id,
            "engine_version": self.engine_version,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class KillSwitchTrigger:
    kind: KillSwitchKind
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
