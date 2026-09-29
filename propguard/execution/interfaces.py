"""Strategy-agnostic trading interfaces.

``BrokerAdapter`` implementations only implement the protected ``_place/_cancel/...`` hooks. The
public ``submit`` path is defined once here and *cannot* be overridden (enforced in
``__init_subclass__``); it accepts only an ``ApprovedOrder`` minted by ``PreTradeGuard``, verifies
its signature, expiry and single use before anything reaches the platform.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Iterable, Protocol

from propguard.risk.models import (
    AccountSnapshot,
    InstrumentSpec,
    OrderIntent,
    OrderRequest,
    Quote,
)

if TYPE_CHECKING:
    from propguard.execution.guard import ApprovedOrder


class OrderStatus(StrEnum):
    RESERVED = "RESERVED"  # idempotency key stored, not yet evaluated
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
    UNKNOWN = "UNKNOWN"  # timeout / ambiguous: must be resolved by lookup, never blind resubmission
    FAILED = "FAILED"


TERMINAL = {OrderStatus.REJECTED_BY_RISK, OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED_BY_BROKER, OrderStatus.FAILED}


class BrokerError(Exception):
    """Definitive error: the platform did NOT accept the order."""


class BrokerTimeout(Exception):
    """Ambiguous: the order may or may not have reached the platform."""


class BrokerDisconnected(BrokerError):
    """Connection is down before sending (safe to retry after reconnect + lookup)."""


class BypassAttempt(RuntimeError):
    """Raised when something tries to reach a broker without a valid PreTradeGuard approval."""


@dataclass(frozen=True)
class ExecutionReport:
    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    filled_lots: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    position_id: str | None = None
    message: str = ""
    ts: datetime | None = None


@dataclass(frozen=True)
class BrokerEvent:
    event_id: str  # unique per platform event; duplicates are dropped by the consumer
    sequence: int
    kind: str  # fill | partial_fill | reject | cancel | position_closed | sl_hit | swap | manual_trade
    ts: datetime
    client_order_id: str | None = None
    broker_order_id: str | None = None
    position_id: str | None = None
    lots: Decimal = Decimal("0")
    price: Decimal | None = None
    pnl: Decimal | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterCapabilities:
    name: str
    is_live: bool  # True if orders reach a real account (real or prop-firm evaluation money)
    channel: str  # "api" | "ea" -- maps to firm's api_trading / ea_policy rule
    supports_client_order_id: bool = True
    supports_partial_fills: bool = True
    requires_os: tuple[str, ...] = ()  # e.g. ("windows",) for terminal-bound platforms
    notes: str = ""


_PROTECTED_PUBLIC = frozenset({"submit", "__init_subclass__", "_verify_and_consume"})


class BrokerAdapter(abc.ABC):
    capabilities: AdapterCapabilities

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        for name in _PROTECTED_PUBLIC:
            if name in cls.__dict__:
                raise TypeError(f"{cls.__name__} may not override BrokerAdapter.{name} (PreTradeGuard bypass)")

    # -- the ONLY way to send an order-affecting request to a platform
    def submit(self, approved: "ApprovedOrder") -> ExecutionReport:
        order = self._verify_and_consume(approved)
        if order.intent == OrderIntent.CANCEL:
            return self._cancel(order)
        if order.intent in (OrderIntent.REDUCE, OrderIntent.CLOSE):
            return self._close(order)
        if order.intent == OrderIntent.MODIFY_SL:
            return self._modify_sl(order)
        return self._place(order)

    def _verify_and_consume(self, approved: "ApprovedOrder") -> OrderRequest:
        from propguard.execution.guard import verify_and_consume

        return verify_and_consume(approved, adapter_name=self.capabilities.name)

    # -- hooks implemented by platform adapters (never call directly)
    @abc.abstractmethod
    def _place(self, order: OrderRequest) -> ExecutionReport: ...

    @abc.abstractmethod
    def _close(self, order: OrderRequest) -> ExecutionReport: ...

    @abc.abstractmethod
    def _cancel(self, order: OrderRequest) -> ExecutionReport: ...

    @abc.abstractmethod
    def _modify_sl(self, order: OrderRequest) -> ExecutionReport: ...

    # -- read-only API
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    def get_snapshot(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    def find_order(self, client_order_id: str) -> ExecutionReport | None:
        """Look up an order by client id (used to resolve timeouts without resubmitting)."""

    @abc.abstractmethod
    def events_since(self, sequence: int) -> list[BrokerEvent]: ...

    def reconstruct_day_start(self, reset_at: datetime) -> tuple[Decimal, Decimal] | None:
        """Optional: (balance, equity) at ``reset_at`` from deal history; None if unsupported."""
        return None


class MarketDataProvider(Protocol):
    def quote(self, symbol: str) -> Quote | None: ...

    def instrument(self, symbol: str) -> InstrumentSpec | None: ...

    def instruments(self) -> dict[str, InstrumentSpec]: ...

    def volatility_ratio(self, symbol: str) -> Decimal: ...

    def is_open(self, symbol: str) -> bool: ...


@dataclass(frozen=True)
class Signal:
    """A strategy's *intent*. Size is decided by PositionSizer and capped by the Risk Engine."""

    signal_id: str
    symbol: str
    side: str  # BUY | SELL | FLAT
    stop_loss: Decimal | None
    take_profit: Decimal | None = None
    risk_frac: Decimal | None = None  # desired risk as fraction of initial balance
    strategy_id: str = "strategy"
    ts: datetime | None = None
    note: str = ""


class SignalProvider(Protocol):
    def signals(self, now: datetime) -> Iterable[Signal]: ...


class Strategy(abc.ABC):
    """User-selected trading logic. Receives market data, emits Signals. Never sees the adapter."""

    strategy_id: str = "strategy"

    @abc.abstractmethod
    def on_bar(self, now: datetime, market: MarketDataProvider, snapshot: AccountSnapshot) -> list[Signal]: ...


class PositionSizer(Protocol):
    def size(self, signal: Signal, snapshot: AccountSnapshot, spec: InstrumentSpec, quote: Quote,
             initial_balance: Decimal) -> Decimal: ...
