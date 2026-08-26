"""Virtual position ledger.

Append-only snapshot history; the current position is the row with MAX(as_of)
per (strategy_id, symbol), ties broken by insertion order (newest wins).
Under a ReplayClock multiple fills can share one timestamp, so insertion
order is part of the convention, not an edge case. Satisfies the strategy
layer's read-only PortfolioView protocol.
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
        best: VirtualPosition | None = None
        for s in self._snapshots:
            if s.strategy_id != strategy_id or s.symbol != symbol:
                continue
            if best is None or s.as_of >= best.as_of:
                best = s
        return best

    def positions_for_symbol(self, symbol: str) -> list[VirtualPosition]:
        latest: dict[str, VirtualPosition] = {}
        for s in self._snapshots:
            if s.symbol != symbol:
                continue
            held = latest.get(s.strategy_id)
            if held is None or s.as_of >= held.as_of:
                latest[s.strategy_id] = s
        return [p for p in latest.values() if p.quantity != 0]

    def open_positions(self) -> list[VirtualPosition]:
        """All symbols' current holdings — the portfolio-wide position count."""
        latest: dict[tuple[str, str], VirtualPosition] = {}
        for s in self._snapshots:
            key = (s.strategy_id, s.symbol)
            held = latest.get(key)
            if held is None or s.as_of >= held.as_of:
                latest[key] = s
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
        """Apply an attributed fill and record the resulting snapshot.

        average_price is the volume-weighted cost basis: increases blend it,
        reductions keep it, a flip through zero restarts it at the fill price.
        """
        current = self.position(strategy_id, symbol)
        signed = current.signed_quantity if current else Decimal(0)
        delta = quantity if side is ExecutionSide.BUY else -quantity
        new_signed = signed + delta

        snapshot = VirtualPosition(
            strategy_id=strategy_id,
            symbol=symbol,
            direction=(
                PositionDirection.LONG if new_signed >= 0 else PositionDirection.SHORT
            ),
            quantity=abs(new_signed),
            average_price=self._cost_basis(current, signed, delta, new_signed, price),
            as_of=self._clock.now(),
        )
        self.record(snapshot)
        return snapshot

    @staticmethod
    def _cost_basis(
        current: VirtualPosition | None,
        signed: Decimal,
        delta: Decimal,
        new_signed: Decimal,
        price: Decimal,
    ) -> Decimal | None:
        if new_signed == 0:
            return None
        if signed == 0 or (signed > 0) != (new_signed > 0):
            return price
        if (signed > 0) == (delta > 0):
            old_average = (
                current.average_price if current and current.average_price else price
            )
            return (old_average * abs(signed) + price * abs(delta)) / abs(new_signed)
        return current.average_price if current else None
