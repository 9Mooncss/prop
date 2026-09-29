"""Value objects for the Risk Engine. All money values are ``Decimal``; all times are tz-aware UTC."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderIntent(StrEnum):
    OPEN = "OPEN"  # increases (or creates) exposure
    REDUCE = "REDUCE"  # partially closes an existing position
    CLOSE = "CLOSE"  # fully closes an existing position
    CANCEL = "CANCEL"  # cancels a pending order
    MODIFY_SL = "MODIFY_SL"  # moves a stop (only allowed if it does not increase risk)


RISK_REDUCING_INTENTS = frozenset({OrderIntent.REDUCE, OrderIntent.CLOSE, OrderIntent.CANCEL})


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: str = "fx"  # fx | indices | metals | energy | crypto | stocks
    contract_size: Decimal = Decimal("100000")  # units per 1.0 lot
    lot_step: Decimal = Decimal("0.01")
    min_lot: Decimal = Decimal("0.01")
    commission_per_lot_round_turn: Decimal = ZERO  # account currency
    base_ccy: str = ""
    quote_ccy: str = ""


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    ts: datetime
    # multiply a price difference (in quote ccy) * units to get account currency
    quote_to_account: Decimal = Decimal("1")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def mid(self) -> Decimal:
        return (self.ask + self.bid) / 2


@dataclass(frozen=True)
class Position:
    position_id: str
    symbol: str
    side: Side
    lots: Decimal
    entry_price: Decimal
    opened_at: datetime
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    client_order_id: str | None = None  # None -> not opened by this system (manual/unknown)
    unrealized_pnl: Decimal | None = None  # as reported by platform (account ccy)
    swap: Decimal = ZERO
    commission: Decimal = ZERO


@dataclass(frozen=True)
class PendingOrder:
    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    lots: Decimal
    order_type: OrderType
    price: Decimal
    stop_loss: Decimal | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    """Account state as reported by the platform (or simulator)."""

    account_id: str
    ts: datetime
    balance: Decimal
    equity: Decimal
    positions: tuple[Position, ...] = ()
    pending_orders: tuple[PendingOrder, ...] = ()
    currency: str = "USD"
    margin_used: Decimal = ZERO
    sequence: int = 0  # monotonically increasing per source, used to detect out-of-order updates


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    account_id: str
    symbol: str
    side: Side
    lots: Decimal
    intent: OrderIntent = OrderIntent.OPEN
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None  # for LIMIT/STOP
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    position_id: str | None = None  # for REDUCE/CLOSE/MODIFY_SL
    target_broker_order_id: str | None = None  # for CANCEL
    strategy_id: str = "manual"
    created_at: datetime | None = None
    # set only by the deterministic supervisor when an external hard floor is threatened
    emergency: bool = False

    def with_lots(self, lots: Decimal) -> "OrderRequest":
        return replace(self, lots=lots)


@dataclass
class RiskState:
    """Persistent per-account state required to evaluate drawdown rules across restarts."""

    account_id: str
    initial_balance: Decimal
    trading_date: date | None = None
    day_start_balance: Decimal | None = None
    day_start_equity: Decimal | None = None
    day_start_known: bool = False  # False -> reference captured late / reconstructed uncertainly
    hwm_balance: Decimal | None = None
    hwm_equity: Decimal | None = None
    eod_hwm_balance: Decimal | None = None
    eod_hwm_equity: Decimal | None = None
    min_equity_today: Decimal | None = None
    last_snapshot_ts: datetime | None = None
    last_sequence: int = -1
    trading_days: set[str] = field(default_factory=set)  # ISO dates with qualifying activity
    started_at: datetime | None = None
    last_activity_ts: datetime | None = None
    daily_closed_pnl: dict[str, Decimal] = field(default_factory=dict)  # ISO date -> realised PnL
    breached: list[str] = field(default_factory=list)  # external rule breaches observed
    target_reached: bool = False

    def to_dict(self) -> dict:
        def s(v):
            return None if v is None else str(v)

        return {
            "account_id": self.account_id,
            "initial_balance": str(self.initial_balance),
            "trading_date": self.trading_date.isoformat() if self.trading_date else None,
            "day_start_balance": s(self.day_start_balance),
            "day_start_equity": s(self.day_start_equity),
            "day_start_known": self.day_start_known,
            "hwm_balance": s(self.hwm_balance),
            "hwm_equity": s(self.hwm_equity),
            "eod_hwm_balance": s(self.eod_hwm_balance),
            "eod_hwm_equity": s(self.eod_hwm_equity),
            "min_equity_today": s(self.min_equity_today),
            "last_snapshot_ts": self.last_snapshot_ts.isoformat() if self.last_snapshot_ts else None,
            "last_sequence": self.last_sequence,
            "trading_days": sorted(self.trading_days),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_activity_ts": self.last_activity_ts.isoformat() if self.last_activity_ts else None,
            "daily_closed_pnl": {k: str(v) for k, v in self.daily_closed_pnl.items()},
            "breached": list(self.breached),
            "target_reached": self.target_reached,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        def dec(v):
            return None if v is None else Decimal(v)

        def dt(v):
            return None if v is None else datetime.fromisoformat(v)

        return cls(
            account_id=d["account_id"],
            initial_balance=Decimal(d["initial_balance"]),
            trading_date=date.fromisoformat(d["trading_date"]) if d.get("trading_date") else None,
            day_start_balance=dec(d.get("day_start_balance")),
            day_start_equity=dec(d.get("day_start_equity")),
            day_start_known=bool(d.get("day_start_known")),
            hwm_balance=dec(d.get("hwm_balance")),
            hwm_equity=dec(d.get("hwm_equity")),
            eod_hwm_balance=dec(d.get("eod_hwm_balance")),
            eod_hwm_equity=dec(d.get("eod_hwm_equity")),
            min_equity_today=dec(d.get("min_equity_today")),
            last_snapshot_ts=dt(d.get("last_snapshot_ts")),
            last_sequence=int(d.get("last_sequence", -1)),
            trading_days=set(d.get("trading_days", [])),
            started_at=dt(d.get("started_at")),
            last_activity_ts=dt(d.get("last_activity_ts")),
            daily_closed_pnl={k: Decimal(v) for k, v in d.get("daily_closed_pnl", {}).items()},
            breached=list(d.get("breached", [])),
            target_reached=bool(d.get("target_reached")),
        )
