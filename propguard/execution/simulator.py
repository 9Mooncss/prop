"""Simulated market and broker for PAPER trading, tests and failure injection.

Supports: spread, slippage, commissions, swaps, stop-loss/take-profit (with gap-through fills),
limit/stop pending orders, partial fills, broker rejects, ambiguous timeouts (order placed but
the response lost), disconnects, duplicate and out-of-order events, manual trades and corrupted
account state.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from propguard.execution.interfaces import (
    AdapterCapabilities,
    BrokerAdapter,
    BrokerDisconnected,
    BrokerError,
    BrokerEvent,
    BrokerTimeout,
    ExecutionReport,
    OrderStatus,
)
from propguard.risk.limits import computed_unrealized
from propguard.risk.models import (
    ZERO,
    AccountSnapshot,
    InstrumentSpec,
    OrderRequest,
    OrderType,
    PendingOrder,
    Position,
    Quote,
    Side,
)


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock requires tz-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kw: float) -> datetime:
        self._now += timedelta(**kw)
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = ts


class SimulatedMarket:
    """MarketDataProvider backed by explicitly set prices."""

    def __init__(self, clock: SimClock, instruments: dict[str, InstrumentSpec]) -> None:
        self.clock = clock
        self._instruments = dict(instruments)
        self._quotes: dict[str, Quote] = {}
        self._frozen: set[str] = set()
        self._vol: dict[str, Decimal] = {}
        self._closed = False

    def set_closed(self, closed: bool) -> None:
        self._closed = closed

    def is_open(self, symbol: str) -> bool:
        return not self._closed and symbol in self._instruments

    def set_price(self, symbol: str, bid: Decimal | str, ask: Decimal | str | None = None,
                  spread: Decimal | str | None = None) -> Quote:
        bid = Decimal(str(bid))
        if ask is None:
            ask = bid + Decimal(str(spread if spread is not None else "0.0001"))
        q = Quote(symbol=symbol, bid=bid, ask=Decimal(str(ask)), ts=self.clock.now())
        if symbol not in self._frozen:
            self._quotes[symbol] = q
        return q

    def freeze(self, symbol: str) -> None:
        """Simulate a stalled feed: quotes stop updating (timestamps age)."""
        self._frozen.add(symbol)

    def unfreeze(self, symbol: str) -> None:
        self._frozen.discard(symbol)

    def set_volatility_ratio(self, symbol: str, ratio: Decimal) -> None:
        self._vol[symbol] = ratio

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)

    def instruments(self) -> dict[str, InstrumentSpec]:
        return dict(self._instruments)

    def volatility_ratio(self, symbol: str) -> Decimal:
        return self._vol.get(symbol, Decimal("1"))


@dataclass
class SimConfig:
    slippage_points: Decimal = Decimal("0")  # price units added against the trader on market fills
    partial_fill_ratio: Decimal | None = None  # e.g. 0.5 -> next market order fills half (IOC)
    reject_next: int = 0
    timeout_next_after_send: int = 0  # order IS placed, response lost -> BrokerTimeout
    timeout_next_before_send: int = 0  # order NOT placed -> BrokerTimeout
    duplicate_events: bool = False
    seed: int = 7


@dataclass
class _SimOrder:
    report: ExecutionReport
    request: OrderRequest


class SimulatedBroker(BrokerAdapter):
    capabilities = AdapterCapabilities(name="simulated", is_live=False, channel="api",
                                       notes="In-process simulator; no real money")

    def __init__(self, account_id: str, market: SimulatedMarket, initial_balance: Decimal,
                 config: SimConfig | None = None, capabilities: AdapterCapabilities | None = None) -> None:
        self.account_id = account_id
        self.market = market
        self.balance = Decimal(initial_balance)
        self.config = config or SimConfig()
        if capabilities is not None:
            self.capabilities = capabilities
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, tuple[PendingOrder, OrderRequest]] = {}
        self.orders: dict[str, _SimOrder] = {}
        self.events: list[BrokerEvent] = []
        self.deals: list[tuple[datetime, Decimal]] = []  # (ts, balance after)
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._snap_seq = itertools.count(1)
        self._connected = False
        self._equity_corruption = ZERO
        self._rng = random.Random(self.config.seed)
        self.submit_count = 0
        self.deals.append((market.clock.now(), self.balance))

    # ---------------------------------------------------------------- connection
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("simulated broker disconnected")

    # ---------------------------------------------------------------- hooks
    def _place(self, order: OrderRequest) -> ExecutionReport:
        self._require()
        self.submit_count += 1
        cfg = self.config
        if cfg.timeout_next_before_send > 0:
            cfg.timeout_next_before_send -= 1
            raise BrokerTimeout("timeout before send")
        if order.client_order_id in self.orders:  # platform-side idempotency on client id
            return self.orders[order.client_order_id].report
        if cfg.reject_next > 0:
            cfg.reject_next -= 1
            rep = self._report(order, None, OrderStatus.REJECTED_BY_BROKER, message="rejected by simulator")
            self._event("reject", order=order)
            return rep
        spec = self.market.instrument(order.symbol)
        q = self.market.quote(order.symbol)
        if spec is None or q is None:
            raise BrokerError(f"unknown symbol {order.symbol}")
        boid = f"B{next(self._ids)}"
        if order.order_type != OrderType.MARKET:
            po = PendingOrder(boid, order.client_order_id, order.symbol, order.side, order.lots,
                              order.order_type, order.price, order.stop_loss)
            self.pending[boid] = (po, order)
            rep = self._report(order, boid, OrderStatus.ACCEPTED)
        else:
            lots = order.lots
            status = OrderStatus.FILLED
            if cfg.partial_fill_ratio is not None:
                lots = (order.lots * cfg.partial_fill_ratio).quantize(spec.lot_step)
                cfg.partial_fill_ratio = None
                status = OrderStatus.PARTIALLY_FILLED
            px = self._fill_price(order.side, q)
            pos = self._open_position(order, spec, lots, px)
            rep = self._report(order, boid, status, lots, px, pos.position_id)
            self._event("partial_fill" if status == OrderStatus.PARTIALLY_FILLED else "fill", order=order,
                        boid=boid, pos=pos.position_id, lots=lots, price=px)
        if cfg.timeout_next_after_send > 0:
            cfg.timeout_next_after_send -= 1
            raise BrokerTimeout("response lost after send")
        return rep

    def _close(self, order: OrderRequest) -> ExecutionReport:
        self._require()
        self.submit_count += 1
        if order.client_order_id in self.orders:
            return self.orders[order.client_order_id].report
        pos = self.positions.get(order.position_id or "")
        if pos is None:
            return self._report(order, None, OrderStatus.REJECTED_BY_BROKER, message="position not found")
        q = self.market.quote(pos.symbol)
        px = self._fill_price(order.side, q)
        lots = min(order.lots, pos.lots)
        pnl = self._realize(pos, lots, px)
        rep = self._report(order, f"B{next(self._ids)}", OrderStatus.FILLED, lots, px, pos.position_id)
        self._event("position_closed", order=order, pos=pos.position_id, lots=lots, price=px, pnl=pnl)
        return rep

    def _cancel(self, order: OrderRequest) -> ExecutionReport:
        self._require()
        if order.client_order_id in self.orders:
            return self.orders[order.client_order_id].report
        item = self.pending.pop(order.target_broker_order_id or "", None)
        if item is None:
            return self._report(order, None, OrderStatus.REJECTED_BY_BROKER, message="order not found")
        self._event("cancel", order=order, boid=order.target_broker_order_id)
        return self._report(order, order.target_broker_order_id, OrderStatus.CANCELLED)

    def _modify_sl(self, order: OrderRequest) -> ExecutionReport:
        self._require()
        pos = self.positions.get(order.position_id or "")
        if pos is None:
            return self._report(order, None, OrderStatus.REJECTED_BY_BROKER, message="position not found")
        self.positions[pos.position_id] = replace(pos, stop_loss=order.stop_loss)
        return self._report(order, None, OrderStatus.FILLED, position_id=pos.position_id)

    # ---------------------------------------------------------------- read API
    def get_snapshot(self) -> AccountSnapshot:
        self._require()
        positions = []
        floating = ZERO
        for p in self.positions.values():
            spec, q = self.market.instrument(p.symbol), self.market.quote(p.symbol)
            u = computed_unrealized(p, spec, q) if spec and q else ZERO
            positions.append(replace(p, unrealized_pnl=u))
            floating += u + p.swap
        pend = tuple(po for po, _ in self.pending.values())
        return AccountSnapshot(account_id=self.account_id, ts=self.market.clock.now(), balance=self.balance,
                               equity=self.balance + floating + self._equity_corruption,
                               positions=tuple(positions), pending_orders=pend, sequence=next(self._snap_seq))

    def find_order(self, client_order_id: str) -> ExecutionReport | None:
        self._require()
        o = self.orders.get(client_order_id)
        return o.report if o else None

    def events_since(self, sequence: int) -> list[BrokerEvent]:
        evs = [e for e in self.events if e.sequence > sequence]
        if self.config.duplicate_events and evs:
            evs = evs + [evs[-1]]  # re-deliver last event
        return evs

    def reconstruct_day_start(self, reset_at: datetime) -> tuple[Decimal, Decimal] | None:
        before = [b for ts, b in self.deals if ts <= reset_at]
        if not before or self.positions:
            return None  # cannot know floating PnL at reset time when positions were open
        return before[-1], before[-1]

    # ---------------------------------------------------------------- simulation controls
    def on_price_update(self) -> None:
        """Trigger pending orders and SL/TP with gap-through fills at the current (worse) price."""
        for boid, (po, req) in list(self.pending.items()):
            q = self.market.quote(po.symbol)
            if q is None:
                continue
            hit = ((po.order_type == OrderType.LIMIT and ((po.side is Side.BUY and q.ask <= po.price)
                                                          or (po.side is Side.SELL and q.bid >= po.price)))
                   or (po.order_type == OrderType.STOP and ((po.side is Side.BUY and q.ask >= po.price)
                                                           or (po.side is Side.SELL and q.bid <= po.price))))
            if hit:
                del self.pending[boid]
                spec = self.market.instrument(po.symbol)
                px = self._fill_price(po.side, q)
                pos = self._open_position(req, spec, po.lots, px)
                self.orders[req.client_order_id] = _SimOrder(
                    ExecutionReport(req.client_order_id, boid, OrderStatus.FILLED, po.lots, px, pos.position_id,
                                    ts=self.market.clock.now()), req)
                self._event("fill", order=req, boid=boid, pos=pos.position_id, lots=po.lots, price=px)
        for p in list(self.positions.values()):
            q = self.market.quote(p.symbol)
            if q is None:
                continue
            mark = q.bid if p.side is Side.BUY else q.ask
            sl_hit = p.stop_loss is not None and ((p.side is Side.BUY and mark <= p.stop_loss)
                                                  or (p.side is Side.SELL and mark >= p.stop_loss))
            tp_hit = p.take_profit is not None and ((p.side is Side.BUY and mark >= p.take_profit)
                                                    or (p.side is Side.SELL and mark <= p.take_profit))
            if sl_hit or tp_hit:
                px = mark  # gap-through: filled at current market, possibly worse than stop
                pnl = self._realize(p, p.lots, px)
                self._event("sl_hit" if sl_hit else "tp_hit", pos=p.position_id, lots=p.lots, price=px, pnl=pnl,
                            coid=p.client_order_id)

    def apply_swap(self, per_lot: Decimal) -> None:
        for pid, p in self.positions.items():
            self.positions[pid] = replace(p, swap=p.swap + per_lot * p.lots)
            self._event("swap", pos=pid, pnl=per_lot * p.lots)

    def inject_manual_trade(self, symbol: str, side: Side, lots: Decimal) -> Position:
        spec, q = self.market.instrument(symbol), self.market.quote(symbol)
        px = self._fill_price(side, q)
        pid = f"P{next(self._ids)}"
        pos = Position(position_id=pid, symbol=symbol, side=side, lots=lots, entry_price=px,
                       opened_at=self.market.clock.now(), client_order_id=None)
        self.positions[pid] = pos
        self.balance -= spec.commission_per_lot_round_turn * lots
        self._event("manual_trade", pos=pid, lots=lots, price=px)
        return pos

    def remove_position_silently(self, position_id: str) -> None:
        """Failure injection: position disappears without an event (platform-side liquidation)."""
        self.positions.pop(position_id, None)

    def corrupt_equity(self, delta: Decimal) -> None:
        self._equity_corruption = Decimal(delta)

    # ---------------------------------------------------------------- internals
    def _fill_price(self, side: Side, q: Quote) -> Decimal:
        slip = self.config.slippage_points
        return q.ask + slip if side is Side.BUY else q.bid - slip

    def _open_position(self, order: OrderRequest, spec: InstrumentSpec, lots: Decimal, px: Decimal) -> Position:
        pid = f"P{next(self._ids)}"
        pos = Position(position_id=pid, symbol=order.symbol, side=order.side, lots=lots, entry_price=px,
                       opened_at=self.market.clock.now(), stop_loss=order.stop_loss,
                       take_profit=order.take_profit, client_order_id=order.client_order_id)
        self.positions[pid] = pos
        comm = spec.commission_per_lot_round_turn * lots
        self.balance -= comm
        self.deals.append((self.market.clock.now(), self.balance))
        return pos

    def _realize(self, pos: Position, lots: Decimal, px: Decimal) -> Decimal:
        spec = self.market.instrument(pos.symbol)
        q = self.market.quote(pos.symbol)
        diff = (px - pos.entry_price) * pos.side.sign
        pnl = diff * lots * spec.contract_size * (q.quote_to_account if q else Decimal("1"))
        swap_part = pos.swap * (lots / pos.lots)
        pnl += swap_part
        self.balance += pnl
        remaining = pos.lots - lots
        if remaining > 0:
            self.positions[pos.position_id] = replace(pos, lots=remaining, swap=pos.swap - swap_part)
        else:
            del self.positions[pos.position_id]
        self.deals.append((self.market.clock.now(), self.balance))
        return pnl

    def _report(self, order, boid, status, lots=ZERO, px=None, pos_id=None, message="") -> ExecutionReport:
        rep = ExecutionReport(order.client_order_id, boid, status, lots, px, pos_id, message,
                              self.market.clock.now())
        self.orders[order.client_order_id] = _SimOrder(rep, order)
        return rep

    def _event(self, kind, order=None, boid=None, pos=None, lots=ZERO, price=None, pnl=None, coid=None):
        seq = next(self._seq)
        self.events.append(BrokerEvent(
            event_id=f"E{seq}", sequence=seq, kind=kind, ts=self.market.clock.now(),
            client_order_id=coid or (order.client_order_id if order else None), broker_order_id=boid,
            position_id=pos, lots=lots, price=price, pnl=pnl))


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)
