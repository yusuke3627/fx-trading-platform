"""Strategy runner.

All horizons (scalp / intraday / swing) run inside one trading application in
the modular-monolith stages — never as separate per-horizon processes. The
runner only dispatches events and collects signals; execution always flows
through Portfolio -> Risk -> OMS -> Execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.domain.event import EventEnvelope
from trading.domain.signal import StrategySignal
from trading.strategy.base import (
    LIVE_ELIGIBLE_STATUSES,
    Strategy,
    StrategyContext,
    StrategyStatus,
)


@dataclass(frozen=True)
class StrategyBinding:
    strategy: Strategy
    context: StrategyContext

    @property
    def status(self) -> StrategyStatus:
        return self.context.config.status

    @property
    def live_eligible(self) -> bool:
        return self.status in LIVE_ELIGIBLE_STATUSES


@dataclass(frozen=True)
class CollectedSignal:
    signal: StrategySignal
    status: StrategyStatus
    live_eligible: bool


class StrategyRunner:
    def __init__(self, bindings: list[StrategyBinding]) -> None:
        ids = [b.strategy.strategy_id for b in bindings]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate strategy_id in bindings: {ids}")
        self._bindings = bindings

    @property
    def bindings(self) -> list[StrategyBinding]:
        return list(self._bindings)

    async def dispatch(self, event: EventEnvelope) -> list[CollectedSignal]:
        """Run the event through every enabled strategy. Signals carry their
        strategy status so downstream layers can gate research/shadow output
        away from the live pipeline."""
        collected: list[CollectedSignal] = []
        for binding in self._bindings:
            if not binding.context.config.enabled:
                continue
            if binding.status is StrategyStatus.DISABLED:
                continue
            signals = await binding.strategy.on_event(event, binding.context)
            collected.extend(
                CollectedSignal(
                    signal=s,
                    status=binding.status,
                    live_eligible=binding.live_eligible,
                )
                for s in signals
            )
        return collected
