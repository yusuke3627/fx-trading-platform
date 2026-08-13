"""Strategy base: Strategy ABC, StrategyContext, StrategyConfig.

The context deliberately exposes read-only services only. MT5 clients, broker
credentials, execution adapters, OMS write interfaces and raw DB connections
must never be reachable from a strategy: a strategy cannot call the broker,
and this is enforced structurally here and by invariant tests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from trading.backtest.clock import Clock
from trading.data.market import MarketDataService
from trading.domain.event import EventEnvelope
from trading.domain.position import PositionDirection, VirtualPosition
from trading.domain.signal import StrategySignal
from trading.indicators import IndicatorService
from trading.intelligence.features import FeatureStore
from trading.intelligence.regime import RegimeService


class StrategyStatus(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    BACKTEST_ELIGIBLE = "BACKTEST_ELIGIBLE"
    SHADOW = "SHADOW"
    MICRO_LIVE = "MICRO_LIVE"
    LIMITED_LIVE = "LIMITED_LIVE"
    PRODUCTION = "PRODUCTION"
    DISABLED = "DISABLED"


LIVE_ELIGIBLE_STATUSES = frozenset(
    {StrategyStatus.MICRO_LIVE, StrategyStatus.LIMITED_LIVE, StrategyStatus.PRODUCTION}
)


class StrategyHorizon(StrEnum):
    SCALP = "SCALP"
    INTRADAY = "INTRADAY"
    SWING = "SWING"


class TimeframeMap(BaseModel):
    """Role -> timeframe mapping (e.g. regime: 1h, setup: 15m, entry: 5m).

    Roles are strategy-defined; extra keys are allowed so configuration owns
    timeframe selection. Accessible as attributes: config.timeframes.regime.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    def role(self, name: str, default: str | None = None) -> str:
        value = (self.model_extra or {}).get(name, default)
        if value is None:
            raise KeyError(f"timeframe role {name!r} is not configured")
        return str(value)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    enabled: bool = False
    status: StrategyStatus = StrategyStatus.RESEARCH_ONLY

    instruments: list[str] = Field(default_factory=list)
    timeframes: TimeframeMap = Field(default_factory=TimeframeMap)
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)

    def param(self, name: str, default: float | str | bool):
        return self.parameters.get(name, default)


class PortfolioView(Protocol):
    """Read-only view of the strategy's own virtual position."""

    def position(self, strategy_id: str, symbol: str) -> VirtualPosition | None: ...


@dataclass(frozen=True)
class StrategyContext:
    clock: Clock
    market: MarketDataService
    indicators: IndicatorService
    features: FeatureStore
    regime: RegimeService
    portfolio: PortfolioView
    config: StrategyConfig


class Strategy(ABC):
    strategy_id: ClassVar[str]
    strategy_version: ClassVar[str]
    horizon: ClassVar[StrategyHorizon]

    @abstractmethod
    async def on_event(
        self,
        event: EventEnvelope,
        context: StrategyContext,
    ) -> list[StrategySignal]: ...

    def _new_setup(self, symbol: str, direction: PositionDirection, setup_id: object) -> bool:
        """One setup, one signal.

        Every market event re-evaluates the same closed bars, so a persisting
        condition would emit a fresh signal (and a fresh intent) on every
        tick. Records the latest setup identity per (symbol, direction) and
        returns False when it already produced a signal.
        """
        memo: dict[tuple[str, PositionDirection], object] = self.__dict__.setdefault(
            "_signaled_setups", {}
        )
        slot = (symbol, direction)
        if memo.get(slot) == setup_id:
            return False
        memo[slot] = setup_id
        return True

    def make_signal(
        self,
        context: StrategyContext,
        *,
        symbol: str,
        direction: PositionDirection,
        conviction: float,
        stop_distance_pips: Decimal,
        expected_horizon_seconds: int,
        reason_codes: list[str],
    ) -> StrategySignal:
        return StrategySignal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            symbol=symbol,
            desired_direction=direction,
            conviction=conviction,
            expected_horizon_seconds=expected_horizon_seconds,
            stop_distance_pips=stop_distance_pips,
            reason_codes=reason_codes,
            generated_at=context.clock.now(),
        )
