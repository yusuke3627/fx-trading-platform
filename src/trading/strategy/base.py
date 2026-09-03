"""Strategy base: Strategy ABC, StrategyContext, StrategyConfig.

The context deliberately exposes read-only services only. MT5 clients, broker
credentials, execution adapters, OMS write interfaces and raw DB connections
must never be reachable from a strategy: a strategy cannot call the broker,
and this is enforced structurally here and by invariant tests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading.backtest.clock import Clock
from trading.data.market import MarketDataService
from trading.domain.event import EventEnvelope
from trading.domain.market import TIMEFRAME_SECONDS
from trading.domain.position import PositionDirection, VirtualPosition
from trading.domain.signal import StrategySignal
from trading.indicators import IndicatorService
from trading.intelligence.currency import CurrencyStateView
from trading.intelligence.features import FeatureStore
from trading.intelligence.regime import CurrencyRegimeService, RegimeService
from trading.strategy.parameters import (
    ResolvedStrategyParameters,
    StrategyParameterResolver,
    StrategyParameters,
)
from trading.strategy.sessions import SessionProfile


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

    def all(self) -> tuple[str, ...]:
        """Every configured timeframe once, shortest first.

        Ordering by duration rather than by role name keeps anything derived
        from this — the bar builders of a replay, for one — identical across
        runs of the same configuration.
        """
        distinct = {str(value) for value in (self.model_extra or {}).values()}
        return tuple(sorted(distinct, key=lambda timeframe: TIMEFRAME_SECONDS[timeframe]))


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    enabled: bool = False
    status: StrategyStatus = StrategyStatus.RESEARCH_ONLY

    instruments: list[str] = Field(default_factory=list)
    timeframes: TimeframeMap = Field(default_factory=TimeframeMap)
    parameters: StrategyParameters = Field(default_factory=StrategyParameters)
    # 参照できる session profile の一覧。load_config が config/base.yaml の
    # session_profiles を全 strategy へ同じものとして渡す。
    session_profiles: dict[str, SessionProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _session_profile_references_exist(self) -> StrategyConfig:
        # 未知の profile 名は最初の市場イベントではなく設定境界で落とす（ADR-023）。
        layers = [self.parameters.defaults, *self.parameters.instruments.values()]
        for layer in layers:
            name = layer.get("session_profile")
            if name is None:
                continue
            if not isinstance(name, str) or name not in self.session_profiles:
                raise ValueError(f"unknown session_profile {name!r} for {self.strategy_id}")
        return self

    def params_for(self, symbol: str) -> ResolvedStrategyParameters:
        # model_copy(update=...) は validator を通らないため、parameters を
        # 差し替える呼び出し側は StrategyParameters を渡す（raw dict 不可）。
        return StrategyParameterResolver(self.parameters).resolve(symbol)

    def session_profile_for(self, symbol: str) -> SessionProfile | None:
        name = self.params_for(symbol).session_profile
        return None if name is None else self.session_profiles[name]

    @property
    def runs(self) -> bool:
        """Whether a strategy with this configuration is evaluated at all.

        Two independent switches: `enabled` is the operator's, `status` is the
        strategy's own lifecycle stage. Anything asking "will this strategy
        actually see events" has to ask both, so the pair lives here rather
        than being spelled out at each caller.
        """
        return self.enabled and self.status is not StrategyStatus.DISABLED


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
    # 通貨単位の方向感と regime（ADR-018 / 021 / 022）。どちらも refresh の
    # たびに中身が入れ替わる read-only の器で、リポジトリは持たない。
    currency_states: CurrencyStateView
    currency_regime: CurrencyRegimeService
    portfolio: PortfolioView
    config: StrategyConfig


# The stretch a closed market adds to a calendar span: a lead-in landing on
# a weekend (or a holiday joining one) still has to reach real ticks.
CLOSED_MARKET_ALLOWANCE = timedelta(days=2)


def market_span_to_calendar(seconds: float) -> timedelta:
    """Calendar time holding `seconds` of market time in recorded history.

    Weekends quote nothing (7/5), plus the closed-market allowance for a
    span whose calendar start falls inside a closure."""
    return timedelta(seconds=seconds * 7 / 5) + CLOSED_MARKET_ALLOWANCE


class Strategy(ABC):
    strategy_id: ClassVar[str]
    strategy_version: ClassVar[str]
    horizon: ClassVar[StrategyHorizon]

    @classmethod
    def warmup(cls, config: StrategyConfig) -> timedelta:
        """Recorded history the slowest indicator window needs before the
        first evaluation sees it populated, computed from the configuration
        the run actually evaluates with. A research replay reads this much
        lead-in ahead of its period and starts asking the strategy at the
        period's opening instant."""
        return timedelta(0)

    @classmethod
    def tick_window_seconds(cls, config: StrategyConfig) -> float:
        """The widest raw-tick window on_event may request from
        market.ticks() under this configuration. The replay engine sizes its
        tick retention from it, so a re-tuned window is retained instead of
        refused mid-replay."""
        return 0.0

    @classmethod
    def bar_window(cls, config: StrategyConfig) -> int:
        """The most bars of one timeframe on_event may request — directly or
        through an indicator — under this configuration. The replay engine
        sizes its bar retention from it, the same way tick_window_seconds
        sizes the tick horizon."""
        return 0

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

    def _session_permits_entry(self, ctx: StrategyContext, symbol: str) -> bool:
        """session profile を参照する instrument は、いま開いている session の
        policy が許すときだけ評価に進む。profile 未参照なら常に進む。"""
        profile = ctx.config.session_profile_for(symbol)
        if profile is None:
            return True
        return profile.permits_entry(
            ctx.clock.now(), live=ctx.config.status in LIVE_ELIGIBLE_STATUSES
        )

    def make_signal(
        self,
        context: StrategyContext,
        *,
        symbol: str,
        direction: PositionDirection,
        conviction: float,
        expected_edge_r: Decimal = Decimal(1),
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
            expected_edge_r=expected_edge_r,
            expected_horizon_seconds=expected_horizon_seconds,
            stop_distance_pips=stop_distance_pips,
            reason_codes=reason_codes,
            generated_at=context.clock.now(),
        )
