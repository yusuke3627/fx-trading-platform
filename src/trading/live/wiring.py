"""Configuration to a runnable set of strategies."""
from __future__ import annotations

from typing import TYPE_CHECKING

from trading.backtest.clock import Clock
from trading.data.market import MarketDataService
from trading.indicators import IndicatorService
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RuleBasedRegimeService
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.runner import StrategyBinding, StrategyRunner
from trading.strategy.base import StrategyContext
from trading.strategy.registry import STRATEGIES

if TYPE_CHECKING:
    from trading.config import AppConfig


class UnknownStrategyError(ValueError):
    """Configuration names a strategy this build does not contain."""


def traded_symbols(config: AppConfig) -> set[str]:
    """Every instrument a strategy that actually runs declares.

    Disabled strategies are excluded: a symbol only they name is one no
    evaluation will ever reach, which for a runner keyed to a single symbol is
    the same as naming a symbol nobody configured.
    """
    return {
        instrument
        for strategy in config.strategies.values()
        if strategy.runs
        for instrument in strategy.instruments
    }


def build_runner(
    config: AppConfig,
    *,
    market: MarketDataService,
    clock: Clock,
    ledger: VirtualPositionLedger,
) -> StrategyRunner:
    """One binding per configured strategy, sharing the read-only services.

    Disabled strategies are bound too. StrategyRunner is what decides who runs
    on an event, and a strategy missing from the runner because of a config
    flag would look exactly like one that was never wired at all.

    An id with no class behind it raises rather than being skipped: a strategy
    the operator switched on and that then never trades is worse than a
    process that refuses to start.
    """
    features = InMemoryFeatureStore()
    regime = RuleBasedRegimeService(features)
    indicators = IndicatorService(market)

    bindings: list[StrategyBinding] = []
    for strategy_id, strategy_config in config.strategies.items():
        strategy_class = STRATEGIES.get(strategy_id)
        if strategy_class is None:
            raise UnknownStrategyError(f"no strategy class registered for {strategy_id!r}")
        bindings.append(
            StrategyBinding(
                strategy=strategy_class(),
                context=StrategyContext(
                    clock=clock,
                    market=market,
                    indicators=indicators,
                    features=features,
                    regime=regime,
                    portfolio=ledger,
                    config=strategy_config,
                ),
            )
        )
    return StrategyRunner(bindings)
