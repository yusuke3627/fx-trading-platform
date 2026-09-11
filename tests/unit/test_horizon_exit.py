"""保有期限の決済と、通常の setup 評価から独立した重複防止。"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import T0, FixedClock, at, held, make_event, manager_with, sizing
from tests.unit.test_session_entry_gate import OFF_SESSION, TOKYO_ONLY, USDJPY_CORE
from trading.domain.position import PositionAction, PositionDirection
from trading.strategy.base import (
    HORIZON_EXPIRED,
    Strategy,
    StrategyConfig,
    StrategyHorizon,
    StrategyStatus,
)
from trading.strategy.intraday.post_event_failed_breakout import PostEventFailedBreakoutStrategy
from trading.strategy.scalp.failed_spike_reversal import FailedSpikeReversalStrategy


class HorizonProbe(Strategy):
    strategy_id = "test_strategy"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.SCALP

    async def on_event(self, event, context):
        return []


def ctx_for(position, instant=T0, *, strategy_id=HorizonProbe.strategy_id, parameters=None):
    return SimpleNamespace(
        clock=FixedClock(instant),
        config=StrategyConfig(
            strategy_id=strategy_id,
            instruments=["USDJPY"],
            parameters=parameters if parameters is not None else {
                "horizon_exit_enabled": True, "expected_horizon_seconds": 300,
            },
        ),
        portfolio=SimpleNamespace(position=lambda _strategy, _symbol: position),
    )


def test_horizon_exit_is_disabled_by_default():
    probe = HorizonProbe()
    ctx = ctx_for(held(PositionDirection.LONG), at(seconds=600), parameters={})
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None


@pytest.mark.parametrize("direction", list(PositionDirection))
def test_horizon_boundary_and_signal_fields(direction):
    probe = HorizonProbe()
    ctx = ctx_for(held(direction), at(seconds=299))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=600) is None
    ctx.clock.advance(seconds=1)
    signal = probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=600)
    assert signal is not None
    assert signal.exit_only is True
    assert signal.desired_direction is not direction
    assert signal.reason_codes == [HORIZON_EXPIRED]
    assert signal.stop_distance_pips == 0
    assert signal.conviction == 1.0
    assert signal.expected_horizon_seconds == 300
    assert signal.generated_at == ctx.clock.now()


@pytest.mark.parametrize("zero_quantity", [False, True])
def test_horizon_fires_once_and_resets_after_flat(zero_quantity):
    probe = HorizonProbe()
    position = held(PositionDirection.LONG)
    ctx = ctx_for(position, at(seconds=300))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is not None
    ctx.clock.advance(seconds=1)
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None
    flat = position.model_copy(update={"quantity": Decimal(0)}) if zero_quantity else None
    assert probe._horizon_exit(ctx_for(flat), "USDJPY", default_horizon_seconds=300) is None
    new_position = position.model_copy(update={"as_of": at(seconds=400)})
    ctx = ctx_for(new_position, at(seconds=700))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is not None


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_increase_preserves_first_observed_start(initially_enabled):
    probe = HorizonProbe()
    position = held(PositionDirection.LONG)
    ctx = ctx_for(position, parameters={"horizon_exit_enabled": initially_enabled})
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None
    increased = position.model_copy(update={"quantity": Decimal(2000), "as_of": at(seconds=200)})
    ctx = ctx_for(increased, at(seconds=300))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is not None


def test_reversal_without_flat_resets_start():
    probe = HorizonProbe()
    ctx = ctx_for(held(PositionDirection.LONG))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None
    reversed_position = held(PositionDirection.SHORT).model_copy(update={"as_of": at(seconds=250)})
    ctx = ctx_for(reversed_position, at(seconds=300))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None
    ctx.clock.advance(seconds=250)
    signal = probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300)
    assert signal is not None
    assert signal.desired_direction is PositionDirection.LONG
    assert signal.exit_only is True


def test_horizon_dedupe_is_independent_of_setup_dedupe():
    probe = HorizonProbe()
    direction = PositionDirection.SHORT
    assert probe._new_setup("USDJPY", direction, "first", exit_only=True)
    before = dict(probe._signaled_setups)
    ctx = ctx_for(held(PositionDirection.LONG), at(seconds=300))
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is not None
    assert probe._signaled_setups == before
    assert probe._new_setup("USDJPY", direction, "second", exit_only=True)
    after = dict(probe._signaled_setups)
    ctx.clock.advance(seconds=1)
    assert probe._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300) is None
    assert probe._signaled_setups == after


@pytest.mark.parametrize("direction", list(PositionDirection))
def test_horizon_signal_produces_only_close_intent(direction):
    position = held(direction)
    ctx = ctx_for(position, at(seconds=300))
    signal = HorizonProbe()._horizon_exit(ctx, "USDJPY", default_horizon_seconds=300)
    intents = manager_with(position).intents_from_signal(signal, sizing())
    assert [intent.action for intent in intents] == [PositionAction.CLOSE]
    assert intents[0].direction is direction
    assert intents[0].target_quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy_class", "horizon"),
    [(FailedSpikeReversalStrategy, 300), (PostEventFailedBreakoutStrategy, 21600)],
)
async def test_on_event_skips_evaluation_only_when_horizon_fires(monkeypatch, strategy_class, horizon):
    strategy = strategy_class()
    position = held(PositionDirection.LONG).model_copy(update={"strategy_id": strategy.strategy_id})
    ctx = ctx_for(
        position, at(seconds=horizon), strategy_id=strategy.strategy_id,
        parameters={"horizon_exit_enabled": True},
    )

    def unexpected_evaluation(symbol, context):
        pytest.fail("時間切れ発火イベントで通常評価が呼ばれた")

    monkeypatch.setattr(strategy, "_evaluate", unexpected_evaluation)
    assert await strategy.on_event(make_event(event_type="account.snapshot"), ctx) == []
    signals = await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx)
    assert len(signals) == 1
    assert signals[0].exit_only is True
    assert signals[0].expected_horizon_seconds == horizon
    evaluated = []

    def record_evaluation(symbol, context):
        evaluated.append(symbol)

    monkeypatch.setattr(strategy, "_evaluate", record_evaluation)
    ctx.clock.advance(seconds=1)
    assert await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx) == []
    assert evaluated == ["USDJPY"]


@pytest.mark.asyncio
async def test_closed_session_flat_observation_clears_horizon_memo(monkeypatch):
    strategy = FailedSpikeReversalStrategy()
    position = held(PositionDirection.LONG).model_copy(
        update={"strategy_id": strategy.strategy_id, "as_of": TOKYO_ONLY}
    )
    ctx = ctx_for(position, TOKYO_ONLY, strategy_id=strategy.strategy_id)
    ctx.config = StrategyConfig(
        strategy_id=strategy.strategy_id, instruments=["USDJPY"],
        status=StrategyStatus.MICRO_LIVE, session_profiles={"probe": USDJPY_CORE},
        parameters={"session_profile": "probe", "horizon_exit_enabled": True},
    )
    evaluated = []

    def record_evaluation(symbol, context):
        evaluated.append(symbol)

    monkeypatch.setattr(strategy, "_evaluate", record_evaluation)
    assert await strategy.on_event(make_event(known_at=TOKYO_ONLY), ctx) == []
    ctx.clock = FixedClock(OFF_SESSION)
    ctx.portfolio = SimpleNamespace(position=lambda _strategy, _symbol: None)
    assert await strategy.on_event(make_event(known_at=OFF_SESSION), ctx) == []
    assert evaluated == ["USDJPY"]
    next_open = TOKYO_ONLY + timedelta(days=1)
    position = position.model_copy(update={"as_of": next_open})
    ctx.clock = FixedClock(next_open)
    ctx.portfolio = SimpleNamespace(position=lambda _strategy, _symbol: position)
    assert await strategy.on_event(make_event(known_at=next_open), ctx) == []
    assert evaluated == ["USDJPY", "USDJPY"]
    ctx.clock.advance(seconds=300)
    signals = await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx)
    assert len(signals) == 1
    assert signals[0].reason_codes == [HORIZON_EXPIRED]
