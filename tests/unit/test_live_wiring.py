"""Live wiring: configuration names strategies, the registry supplies them."""
from types import SimpleNamespace

import pytest

from tests.support import FakeBarRepository, FakeTickRepository, FixedClock, usdjpy_spec
from trading.config import load_config
from trading.data.market.stored import StoredMarketData
from trading.live.clock import CycleClock
from trading.live.wiring import UnknownStrategyError, build_runner, traded_symbols
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.strategy.base import StrategyConfig, StrategyStatus
from trading.strategy.registry import STRATEGIES


def services():
    clock = CycleClock(FixedClock())
    market = StoredMarketData(
        FakeTickRepository(), FakeBarRepository(), clock, {"USDJPY": usdjpy_spec()}
    )
    return {"market": market, "clock": clock, "ledger": VirtualPositionLedger(clock)}


def config_with(*strategy_ids, enabled=True):
    return SimpleNamespace(
        strategies={
            strategy_id: StrategyConfig(
                strategy_id=strategy_id,
                enabled=enabled,
                status=StrategyStatus.SHADOW,
                instruments=["USDJPY"],
            )
            for strategy_id in strategy_ids
        }
    )


def test_every_configured_strategy_is_bound():
    config = config_with("failed_spike_reversal", "post_event_failed_breakout")

    runner = build_runner(config, **services())

    assert {b.strategy.strategy_id for b in runner.bindings} == set(config.strategies)


def test_disabled_strategies_are_bound_too():
    # A strategy missing from the runner because of a config flag looks
    # exactly like one that was never wired; the runner is what gates it.
    config = config_with("failed_spike_reversal", enabled=False)

    runner = build_runner(config, **services())

    assert [b.strategy.strategy_id for b in runner.bindings] == ["failed_spike_reversal"]
    assert runner.bindings[0].context.config.enabled is False


def test_each_binding_carries_its_own_configuration():
    config = config_with("failed_spike_reversal", "monetary_policy_convergence")

    runner = build_runner(config, **services())

    for binding in runner.bindings:
        assert binding.context.config.strategy_id == binding.strategy.strategy_id


def test_an_unregistered_strategy_id_stops_the_build():
    # Refusing to start beats starting without a strategy the operator
    # switched on and then waiting for trades that can never come.
    config = config_with("no_such_strategy")

    with pytest.raises(UnknownStrategyError):
        build_runner(config, **services())


def test_traded_symbols_collects_what_a_running_strategy_declares():
    # Only the runner's symbol gets an instrument spec loaded, so this is what
    # decides whether a --symbol is one any evaluation would actually reach.
    config = SimpleNamespace(
        strategies={
            "a": _config("a", ["USDJPY"]),
            "b": _config("b", ["EURUSD", "USDJPY"]),
        }
    )

    assert traded_symbols(config) == {"USDJPY", "EURUSD"}


def test_traded_symbols_excludes_strategies_that_never_run():
    # A symbol only a switched-off strategy names is one no evaluation reaches,
    # which for a runner keyed to a single symbol is the same as naming a
    # symbol nobody configured at all.
    config = SimpleNamespace(
        strategies={
            "a": _config("a", ["USDJPY"]),
            "b": _config("b", ["EURUSD"], enabled=False),
            "c": _config("c", ["GBPJPY"], status=StrategyStatus.DISABLED),
        }
    )

    assert traded_symbols(config) == {"USDJPY"}


def _config(strategy_id, instruments, *, enabled=True, status=StrategyStatus.SHADOW):
    return StrategyConfig(
        strategy_id=strategy_id,
        enabled=enabled,
        status=status,
        instruments=instruments,
    )


@pytest.mark.parametrize("environment", ["shadow", "micro_live", "production"])
def test_the_shipped_configuration_names_only_strategies_that_exist(environment):
    config = load_config(environment)

    assert set(config.strategies) <= set(STRATEGIES)
