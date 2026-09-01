from decimal import Decimal

import pytest

from tests.support import FixedClock, make_event
from trading.domain.position import PositionDirection
from trading.runner import StrategyBinding, StrategyRunner
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    StrategyStatus,
)


class StubStrategy(Strategy):
    strategy_id = "stub_alpha"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.INTRADAY

    async def on_event(self, event, context):
        return [
            self.make_signal(
                context,
                symbol="USDJPY",
                direction=PositionDirection.SHORT,
                conviction=0.5,
                stop_distance_pips=Decimal(10),
                expected_horizon_seconds=60,
                reason_codes=["STUB"],
            )
        ]


def binding(status: StrategyStatus, enabled: bool = True) -> StrategyBinding:
    context = StrategyContext(
        clock=FixedClock(),
        market=None,
        indicators=None,
        features=None,
        regime=None,
        currency_states=None,
        currency_regime=None,
        portfolio=None,
        config=StrategyConfig(
            strategy_id=StubStrategy.strategy_id,
            enabled=enabled,
            status=status,
            instruments=["USDJPY"],
        ),
    )
    return StrategyBinding(strategy=StubStrategy(), context=context)


async def test_dispatch_collects_signals_with_status():
    runner = StrategyRunner([binding(StrategyStatus.SHADOW)])
    collected = await runner.dispatch(make_event())
    assert len(collected) == 1
    assert collected[0].status is StrategyStatus.SHADOW
    assert collected[0].live_eligible is False
    assert collected[0].signal.strategy_id == "stub_alpha"


async def test_live_eligibility_follows_status():
    runner = StrategyRunner([binding(StrategyStatus.MICRO_LIVE)])
    collected = await runner.dispatch(make_event())
    assert collected[0].live_eligible is True


async def test_disabled_strategies_are_skipped():
    for skipped in (
        binding(StrategyStatus.SHADOW, enabled=False),
        binding(StrategyStatus.DISABLED),
    ):
        runner = StrategyRunner([skipped])
        assert await runner.dispatch(make_event()) == []


def test_duplicate_strategy_ids_rejected():
    with pytest.raises(ValueError):
        StrategyRunner([binding(StrategyStatus.SHADOW), binding(StrategyStatus.SHADOW)])
