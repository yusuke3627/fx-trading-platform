"""Configuration to a runnable set of strategies."""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from trading.backtest.clock import Clock
from trading.data.market import MarketDataService
from trading.indicators import IndicatorService
from trading.intelligence.currency import CurrencyStateStore
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import (
    RuleBasedCurrencyRegimeService,
    RuleBasedRegimeService,
)
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
    evaluation will ever reach, so a runner must not load it among the symbols
    it evaluates.
    """
    return {
        instrument
        for strategy in config.strategies.values()
        if strategy.runs
        for instrument in strategy.instruments
    }


def runner_symbols(
    config: AppConfig, requested: Sequence[str] | None = None
) -> list[str]:
    """The symbols one shadow process evaluates, in evaluation order.

    `--symbol` narrows the run; otherwise market.primary_instruments. Each
    must be traded by a running strategy (a symbol no evaluation reaches
    would sit in the loop asking for quotes forever) and platform-enabled —
    the switch ADR-012 reserved for deciding what the runner evaluates. A
    contradiction between the three refuses to start rather than being
    filtered out quietly.
    """
    symbols = list(
        dict.fromkeys(
            requested if requested is not None else config.market.primary_instruments
        )
    )
    if not symbols:
        raise ValueError("no symbol to evaluate")

    traded = traded_symbols(config)
    for symbol in symbols:
        if symbol not in traded:
            raise ValueError(f"no enabled strategy trades {symbol}: {sorted(traded)}")
        policy = config.instruments.get(symbol)
        if policy is None or not policy.platform_enabled:
            raise ValueError(f"{symbol} is not platform_enabled in instruments")
    return symbols


def build_runner(
    config: AppConfig,
    *,
    market: MarketDataService,
    clock: Clock,
    ledger: VirtualPositionLedger,
    features: InMemoryFeatureStore | None = None,
    currency_states: CurrencyStateStore | None = None,
) -> StrategyRunner:
    """One binding per configured strategy, sharing the read-only services.

    Disabled strategies are bound too. StrategyRunner is what decides who runs
    on an event, and a strategy missing from the runner because of a config
    flag would look exactly like one that was never wired at all.

    An id with no class behind it raises rather than being skipped: a strategy
    the operator switched on and that then never trades is worse than a
    process that refuses to start.

    The caller that wants strategies to see features and currency state
    passes the stores it refreshes; the defaults are empty stores that
    stay empty.
    """
    features = features if features is not None else InMemoryFeatureStore()
    currency_states = (
        currency_states if currency_states is not None else CurrencyStateStore()
    )
    regime = RuleBasedRegimeService(features)
    currency_regime = RuleBasedCurrencyRegimeService(features)
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
                    currency_states=currency_states,
                    currency_regime=currency_regime,
                    portfolio=ledger,
                    config=strategy_config,
                ),
            )
        )
    return StrategyRunner(bindings)
