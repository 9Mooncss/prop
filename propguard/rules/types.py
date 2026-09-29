"""Normalized, machine-readable rule types.

Every rule kind is a small pydantic model registered in ``RULE_TYPES``. New kinds are added by
defining a model and decorating it with ``@register_rule`` -- nothing else in the system needs to
change: unknown kinds are stored as ``custom`` and are treated as *uncertain* by the Risk Engine
if they are flagged critical.

The human-readable original text, provenance, confidence and interpretation status are stored
alongside the params (see ``RuleRecord``); the params models here only describe the semantics.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Tri(StrEnum):
    """Policy answer for permission-style rules."""

    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    CONDITIONAL = "CONDITIONAL"
    UNKNOWN = "UNKNOWN"


class InterpretationStatus(StrEnum):
    CONFIRMED = "CONFIRMED"  # confirmed from primary source(s), no open conflict
    UNCERTAIN = "UNCERTAIN"  # parsed, but ambiguous / low confidence / changed and not re-reviewed
    CONFLICT = "CONFLICT"  # official sources disagree
    UNVERIFIED = "UNVERIFIED"  # only non-primary evidence


class Criticality(StrEnum):
    CRITICAL = "CRITICAL"  # affects account survival / eligibility -> fail closed when uncertain
    HIGH = "HIGH"
    NORMAL = "NORMAL"


class RuleParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ClassVar[str]
    criticality: ClassVar[Criticality] = Criticality.NORMAL


RULE_TYPES: dict[str, type[RuleParams]] = {}


def _validate_hhmm(v: str) -> None:
    try:
        hh, mm = v.split(":")
        if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError
    except ValueError:
        raise ValueError(f"invalid HH:MM time: {v!r}") from None


def _validate_tz(v: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone: {v!r}") from None


def register_rule(cls: type[RuleParams]) -> type[RuleParams]:
    if cls.kind in RULE_TYPES:
        raise ValueError(f"duplicate rule kind {cls.kind}")
    RULE_TYPES[cls.kind] = cls
    return cls


# --------------------------------------------------------------------------- drawdown / targets


@register_rule
class ProfitTarget(RuleParams):
    kind: ClassVar[str] = "profit_target"
    pct: Decimal = Field(gt=0, le=100)
    basis: Literal["initial_balance"] = "initial_balance"
    measured_on: Literal["balance", "equity"] = "balance"


DailyReference = Literal[
    "initial_balance",
    "day_start_balance",
    "day_start_equity",
    "day_start_max_balance_equity",
]


@register_rule
class DailyLossLimit(RuleParams):
    """Daily loss: violated when measured value <= reference - pct * base."""

    kind: ClassVar[str] = "daily_loss_limit"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    # enabled=False explicitly records "this program has no daily loss limit" (confirmed from source)
    enabled: bool = True
    pct: Decimal | None = Field(default=None, gt=0, le=100)
    # what the percentage is taken of
    pct_of: Literal["initial_balance", "reference"] = "initial_balance"
    # level from which the day's loss is measured
    reference: DailyReference = "day_start_max_balance_equity"
    # True -> measured on equity (floating PnL counts); False -> on balance (closed PnL only)
    includes_floating: bool = True
    counts_commissions: bool = True
    counts_swaps: bool = True
    reset_time: str = "00:00"  # HH:MM in reset_tz
    reset_tz: str = "UTC"

    @model_validator(mode="after")
    def _check(self) -> "DailyLossLimit":
        if self.enabled and self.pct is None:
            raise ValueError("pct is required when daily loss limit is enabled")
        _validate_hhmm(self.reset_time)
        _validate_tz(self.reset_tz)
        return self


@register_rule
class MaxLoss(RuleParams):
    kind: ClassVar[str] = "max_loss"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    pct: Decimal = Field(gt=0, le=100)
    # static: floor = initial*(1-pct)
    # trailing: floor = HWM - pct*initial (HWM intraday on `basis`)
    # eod_trailing: floor = end-of-day HWM - pct*initial
    # trailing_lock_at_initial: trailing, but floor never exceeds initial balance
    mode: Literal["static", "trailing", "eod_trailing", "trailing_lock_at_initial"] = "static"
    basis: Literal["equity", "balance"] = "equity"


@register_rule
class MinTradingDays(RuleParams):
    kind: ClassVar[str] = "min_trading_days"
    days: int = Field(ge=0)


@register_rule
class MaxDuration(RuleParams):
    kind: ClassVar[str] = "max_duration"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    days: int | None = Field(default=None, ge=1)  # None = unlimited


@register_rule
class InactivityLimit(RuleParams):
    kind: ClassVar[str] = "inactivity_limit"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    days: int = Field(ge=1)


@register_rule
class TradingDay(RuleParams):
    """Definition of the trading day (if distinct from the daily loss reset)."""

    kind: ClassVar[str] = "trading_day"
    rollover_time: str = "00:00"
    tz: str = "UTC"
    counts_if: Literal["any_trade_opened", "any_trade_closed", "any_activity"] = "any_trade_opened"


@register_rule
class CostTreatment(RuleParams):
    kind: ClassVar[str] = "cost_treatment"
    commissions_count: bool = True
    swaps_count: bool = True
    spread_in_equity: bool = True


# --------------------------------------------------------------------------- sizing / instruments


@register_rule
class Leverage(RuleParams):
    kind: ClassVar[str] = "leverage"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    max_by_class: dict[str, Decimal] = Field(default_factory=dict)  # {"fx": 100, "indices": 20}


@register_rule
class MaxLotSize(RuleParams):
    kind: ClassVar[str] = "max_lot_size"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    max_lots: Decimal = Field(gt=0)
    scope: Literal["per_order", "per_symbol", "total"] = "per_order"


@register_rule
class MaxOpenPositions(RuleParams):
    kind: ClassVar[str] = "max_open_positions"
    max_positions: int = Field(ge=1)


@register_rule
class MaxRiskPerTrade(RuleParams):
    kind: ClassVar[str] = "max_risk_per_trade"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    pct: Decimal = Field(gt=0, le=100)  # of initial balance


@register_rule
class StopLossRequired(RuleParams):
    kind: ClassVar[str] = "stop_loss_required"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    required: bool = True
    within_seconds: int = 0


@register_rule
class InstrumentRestrictions(RuleParams):
    kind: ClassVar[str] = "instrument_restrictions"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    allowed_classes: list[str] | None = None  # None = not restricted by class
    prohibited_symbols: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- holding / timing


@register_rule
class OvernightHolding(RuleParams):
    kind: ClassVar[str] = "overnight_holding"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    allowed: Tri = Tri.UNKNOWN


@register_rule
class WeekendHolding(RuleParams):
    kind: ClassVar[str] = "weekend_holding"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    allowed: Tri = Tri.UNKNOWN
    # Friday cutoff, local to tz, before which positions must be flat if not allowed
    close_by: str = "21:00"
    tz: str = "UTC"


@register_rule
class NewsTrading(RuleParams):
    kind: ClassVar[str] = "news_trading"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    allowed: Tri = Tri.UNKNOWN
    blackout_before_min: int = Field(default=0, ge=0)
    blackout_after_min: int = Field(default=0, ge=0)
    impact_levels: list[str] = Field(default_factory=lambda: ["high"])
    applies_to: Literal["open", "open_and_close", "holding"] = "open_and_close"


# --------------------------------------------------------------------------- automation / conduct


@register_rule
class EAPolicy(RuleParams):
    kind: ClassVar[str] = "ea_policy"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    allowed: Tri = Tri.UNKNOWN
    conditions: str = ""


@register_rule
class APITrading(RuleParams):
    kind: ClassVar[str] = "api_trading"
    criticality: ClassVar[Criticality] = Criticality.CRITICAL
    allowed: Tri = Tri.UNKNOWN
    conditions: str = ""


@register_rule
class CopyTrading(RuleParams):
    kind: ClassVar[str] = "copy_trading"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    allowed: Tri = Tri.UNKNOWN
    conditions: str = ""


@register_rule
class HFTRestriction(RuleParams):
    kind: ClassVar[str] = "hft_restriction"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    allowed: Tri = Tri.UNKNOWN
    min_hold_seconds: int | None = None
    max_orders_per_minute: int | None = None


@register_rule
class LatencyArbitrage(RuleParams):
    kind: ClassVar[str] = "latency_arbitrage"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    allowed: Tri = Tri.PROHIBITED


@register_rule
class IPVPSRestriction(RuleParams):
    kind: ClassVar[str] = "ip_vps_restriction"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    vps_allowed: Tri = Tri.UNKNOWN
    vpn_allowed: Tri = Tri.UNKNOWN
    notes: str = ""


@register_rule
class ConsistencyRule(RuleParams):
    kind: ClassVar[str] = "consistency_rule"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    # best single day profit must be <= pct of total profit
    max_single_day_pct_of_total: Decimal | None = Field(default=None, gt=0, le=100)
    applies_to: Literal["evaluation", "funded", "payout", "all"] = "all"


@register_rule
class ProhibitedStrategies(RuleParams):
    kind: ClassVar[str] = "prohibited_strategies"
    criticality: ClassVar[Criticality] = Criticality.HIGH
    strategies: list[str] = Field(default_factory=list)  # e.g. ["martingale", "grid", "tick_scalping"]


@register_rule
class PlatformRequirement(RuleParams):
    kind: ClassVar[str] = "platform_requirement"
    platforms: list[str] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------- commercial


@register_rule
class Scaling(RuleParams):
    kind: ClassVar[str] = "scaling"
    description: str = ""


@register_rule
class PayoutEligibility(RuleParams):
    kind: ClassVar[str] = "payout_eligibility"
    min_days_before_first: int | None = None
    min_profit_pct: Decimal | None = None
    notes: str = ""


@register_rule
class PayoutFrequency(RuleParams):
    kind: ClassVar[str] = "payout_frequency"
    every_days: int | None = None
    on_demand: bool = False


@register_rule
class ProfitSplit(RuleParams):
    kind: ClassVar[str] = "profit_split"
    pct: Decimal = Field(gt=0, le=100)


@register_rule
class RefundPolicy(RuleParams):
    kind: ClassVar[str] = "refund_policy"
    refundable: Tri = Tri.UNKNOWN
    condition: str = ""


@register_rule
class Custom(RuleParams):
    """Escape hatch for rule kinds not yet modelled. Stored verbatim, evaluated as uncertain."""

    model_config = ConfigDict(extra="allow", frozen=True)
    kind: ClassVar[str] = "custom"
    name: str = "unspecified"


def parse_params(kind: str, params: dict[str, Any] | None) -> RuleParams:
    """Validate raw params for ``kind``. Raises ``ValueError`` for unknown kinds or bad params."""
    cls = RULE_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"unknown rule kind: {kind}")
    try:
        return cls.model_validate(params or {})
    except ValidationError as exc:  # re-raise as ValueError with a compact message
        raise ValueError(f"invalid params for {kind}: {exc.errors(include_url=False)}") from exc


def criticality_of(kind: str) -> Criticality:
    cls = RULE_TYPES.get(kind)
    return cls.criticality if cls else Criticality.HIGH


CRITICAL_KINDS = frozenset(k for k, c in RULE_TYPES.items() if c.criticality == Criticality.CRITICAL)
