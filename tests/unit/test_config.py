from pathlib import Path

import pytest

from trading.config import load_config
from trading.strategy.base import StrategyStatus

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_demo_config_keeps_trading_disabled():
    config = load_config("demo", CONFIG_DIR)
    assert config.environment == "demo"
    assert config.risk.trading_enabled is False
    assert config.broker.expected_account_mode == "HEDGING"


def test_strategy_ids_and_timeframes_come_from_configuration():
    config = load_config("backtest", CONFIG_DIR)
    strategy = config.strategies["post_event_failed_breakout"]
    assert strategy.strategy_id == "post_event_failed_breakout"
    assert strategy.timeframes.role("regime") == "1h"
    assert strategy.timeframes.role("setup") == "15m"
    assert strategy.timeframes.role("entry") == "5m"
    # Attribute access as used in strategy code.
    assert strategy.timeframes.entry == "5m"


def test_all_strategies_start_research_only_in_base():
    config = load_config("demo", CONFIG_DIR)
    for strategy in config.strategies.values():
        assert strategy.status is StrategyStatus.RESEARCH_ONLY
        assert strategy.enabled is False


def test_micro_live_overlay_caps_and_enables():
    config = load_config("micro_live", CONFIG_DIR)
    assert config.risk.trading_enabled is True
    assert config.risk.max_units_per_symbol["USDJPY"] == 1000
    assert config.risk.require_broker_stop_loss is True

    strategy = config.strategies["post_event_failed_breakout"]
    assert strategy.status is StrategyStatus.MICRO_LIVE
    assert strategy.enabled is True
    # Base parameters survive the overlay merge.
    assert strategy.parameters["resistance_lookback"] == 20


def test_backtest_enables_risk_gate_for_simulated_orders():
    # The broker is unreachable in backtest by wiring (simulator only);
    # trading_enabled must not zero out simulated fills.
    config = load_config("backtest", CONFIG_DIR)
    assert config.risk.trading_enabled is True


def test_unknown_environment_rejected():
    with pytest.raises(ValueError):
        load_config("staging", CONFIG_DIR)


def test_no_hardcoded_instruments_in_strategy_config():
    config = load_config("demo", CONFIG_DIR)
    for strategy in config.strategies.values():
        assert strategy.instruments, "instruments must come from configuration"
