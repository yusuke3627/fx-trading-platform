"""Portfolio manager.

Converts signals into position intents with a proposed size (final quantity
permission belongs to the risk engine), tracks per-strategy targets, and
aggregates them into the desired broker net exposure. Under netting, OMS
orders the difference between desired and current broker exposure — never raw
strategy quantities.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from trading.backtest.clock import Clock
from trading.domain.intent import PositionIntent, ProtectionSpec
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.signal import StrategySignal
from trading.portfolio.virtual_ledger import VirtualPositionLedger


@dataclass(frozen=True)
class SizingInput:
    equity: Decimal
    max_risk_per_trade_pct: Decimal
    pip_size: Decimal
    volume_step: Decimal
    entry_price: Decimal
    max_unprotected_seconds: int = 30


class PortfolioManager:
    def __init__(self, ledger: VirtualPositionLedger, clock: Clock) -> None:
        self._ledger = ledger
        self._clock = clock
        self._targets: dict[tuple[str, str], Decimal] = {}

    def intent_from_signal(self, signal: StrategySignal, sizing: SizingInput) -> PositionIntent | None:
        """Proposed intent: risk-budget sizing (equity x per-trade risk %
        against the signal's stop distance), quantized to volume_step."""
        if signal.stop_distance_pips <= 0:
            return None
        risk_budget = sizing.equity * sizing.max_risk_per_trade_pct / Decimal(100)
        loss_per_unit = signal.stop_distance_pips * sizing.pip_size
        raw_quantity = risk_budget / loss_per_unit
        quantity = (raw_quantity // sizing.volume_step) * sizing.volume_step
        if quantity <= 0:
            return None

        current = self._ledger.position(signal.strategy_id, signal.symbol)
        if current is None or current.quantity == 0:
            action = PositionAction.OPEN
        elif current.direction == signal.desired_direction:
            action = PositionAction.INCREASE
        else:
            # Direction change: close the existing virtual position first.
            action = PositionAction.CLOSE

        stop_offset = signal.stop_distance_pips * sizing.pip_size
        if signal.desired_direction is PositionDirection.LONG:
            stop_price = sizing.entry_price - stop_offset
        else:
            stop_price = sizing.entry_price + stop_offset

        return PositionIntent(
            intent_id=uuid4(),
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            symbol=signal.symbol,
            action=action,
            direction=signal.desired_direction,
            target_quantity=quantity if action is not PositionAction.CLOSE else Decimal(0),
            delta_quantity=None,
            protection=ProtectionSpec(
                stop_loss_price=stop_price,
                take_profit_price=None,
                maximum_unprotected_seconds=sizing.max_unprotected_seconds,
                source="STRATEGY",
            ),
            reason_codes=signal.reason_codes,
            generated_at=self._clock.now(),
        )

    def set_target(self, strategy_id: str, symbol: str, signed_quantity: Decimal) -> None:
        self._targets[(strategy_id, symbol)] = signed_quantity

    def target(self, strategy_id: str, symbol: str) -> Decimal:
        return self._targets.get((strategy_id, symbol), Decimal(0))

    def desired_net_exposure(self, symbol: str) -> Decimal:
        """Aggregate of all strategy targets for a symbol (signed units)."""
        return sum(
            (qty for (sid, sym), qty in self._targets.items() if sym == symbol),
            Decimal(0),
        )
