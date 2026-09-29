"""Position sizing. The Risk Engine independently caps whatever size is proposed here."""

from __future__ import annotations

from decimal import Decimal

from propguard.execution.interfaces import Signal
from propguard.risk import limits as L
from propguard.risk.models import AccountSnapshot, InstrumentSpec, OrderRequest, Quote, Side
from propguard.risk.policy import SafetyPolicy


class FixedFractionalSizer:
    """Size so that stop-to-stop worst-case loss ~= risk_frac * initial balance."""

    def __init__(self, default_risk_frac: Decimal = Decimal("0.0025"), policy: SafetyPolicy | None = None) -> None:
        self.default_risk_frac = default_risk_frac
        self.cost = L.CostModel(policy or SafetyPolicy())

    def size(self, signal: Signal, snapshot: AccountSnapshot, spec: InstrumentSpec, quote: Quote,
             initial_balance: Decimal) -> Decimal:
        risk_frac = signal.risk_frac or self.default_risk_frac
        probe = OrderRequest(client_order_id="probe", account_id=snapshot.account_id, symbol=signal.symbol,
                             side=Side(signal.side), lots=Decimal("1"), stop_loss=signal.stop_loss)
        per_lot = L.per_lot_risk_new(probe, spec, quote, self.cost)
        if per_lot is None or per_lot <= 0:
            return Decimal("0")
        return L.round_down_lots(initial_balance * risk_frac / per_lot, spec)
