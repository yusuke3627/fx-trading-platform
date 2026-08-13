"""Virtual position ledger.

Append-only snapshot history; the current position is the row with MAX(as_of)
per (strategy_id, symbol). Satisfies the strategy layer's read-only
PortfolioView protocol.
"""
from __future__ import annotations

from decimal import Decimal

from trading.backtest.clock import Clock
from trading.domain.order import ExecutionSide
from trading.domain.position import PositionDirection, VirtualPosition


class VirtualPositionLedger:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._snapshots: list[VirtualPosition] = []

    def record(self, snapshot: VirtualPosition) -> None:
        self._snapshots.append(snapshot)

    def position(self, strategy_id: str, symbol: str) -> VirtualPosition | None:
        candidates = [
            s
            for s in self._snapshots
            if s.strategy_id == strategy_id and s.symbol == symbol
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.as_of)

    def positions_for_symbol(self, symbol: str) -> list[VirtualPosition]:
        latest: dict[str, VirtualPosition] = {}
        for s in self._snapshots:
            if s.symbol != symbol:
                continue
            held = latest.get(s.strategy_id)
            if held is None or s.as_of > held.as_of:
                latest[s.strategy_id] = s
        return [p for p in latest.values() if p.quantity != 0]

    def net_exposure(self, symbol: str) -> Decimal:
        return sum(
            (p.signed_quantity for p in self.positions_for_symbol(symbol)), Decimal(0)
        )

    def apply_fill(
        self,
        strategy_id: str,
        symbol: str,
        side: ExecutionSide,
        quantity: Decimal,
        price: Decimal,
    ) -> VirtualPosition:
        """Apply an attributed fill and record the resulting snapshot."""
        current = self.position(strategy_id, symbol)
        signed = current.signed_quantity if current else Decimal(0)
        delta = quantity if side is ExecutionSide.BUY else -quantity
        new_signed = signed + delta

        direction = (
            PositionDirection.LONG if new_signed >= 0 else PositionDirection.SHORT
        )
        snapshot = VirtualPosition(
            strategy_id=strategy_id,
            symbol=symbol,
            direction=direction,
            quantity=abs(new_signed),
            average_price=price if new_signed != 0 else None,
            as_of=self._clock.now(),
        )
        self.record(snapshot)
        return snapshot
