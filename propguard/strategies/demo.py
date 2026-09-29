"""DEMONSTRATION ONLY -- NOT A TRADING RECOMMENDATION.

A simple moving-average crossover used exclusively for PAPER trading, simulation and end-to-end
tests of the risk envelope. It has no claimed edge. The live gate refuses to run any strategy whose
``demonstration_only`` flag is set against a live adapter.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal

from propguard.execution.interfaces import MarketDataProvider, Signal, Strategy
from propguard.risk.models import AccountSnapshot


class DemoSMACrossover(Strategy):
    strategy_id = "demo-sma-crossover"
    demonstration_only = True

    def __init__(self, symbol: str, fast: int = 5, slow: int = 20, stop_distance: Decimal = Decimal("0.0030"),
                 risk_frac: Decimal = Decimal("0.0025")) -> None:
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.symbol, self.fast, self.slow = symbol, fast, slow
        self.stop_distance, self.risk_frac = stop_distance, risk_frac
        self._px: deque[Decimal] = deque(maxlen=slow)
        self._prev_state: int | None = None
        self._n = 0

    def on_bar(self, now: datetime, market: MarketDataProvider, snapshot: AccountSnapshot) -> list[Signal]:
        q = market.quote(self.symbol)
        if q is None:
            return []
        self._px.append(q.mid)
        self._n += 1
        if len(self._px) < self.slow:
            return []
        px = list(self._px)
        fast = sum(px[-self.fast:]) / self.fast
        slow = sum(px) / self.slow
        state = 1 if fast > slow else -1
        prev, self._prev_state = self._prev_state, state
        if prev is None or prev == state:
            return []
        has_pos = any(p.symbol == self.symbol for p in snapshot.positions)
        sigs: list[Signal] = []
        if has_pos:
            sigs.append(Signal(f"{self._n}-flat", self.symbol, "FLAT", None, strategy_id=self.strategy_id, ts=now))
        side = "BUY" if state == 1 else "SELL"
        sl = q.ask - self.stop_distance if side == "BUY" else q.bid + self.stop_distance
        sigs.append(Signal(f"{self._n}-{side}", self.symbol, side, sl, risk_frac=self.risk_frac,
                           strategy_id=self.strategy_id, ts=now, note="DEMONSTRATION ONLY"))
        return sigs
