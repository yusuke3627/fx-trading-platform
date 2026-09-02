from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.config import (
    AppConfig,
    EventRiskWindowSettings,
    InstrumentPolicy,
    MarketConfig,
    load_config,
)
from trading.strategy.base import StrategyConfig, StrategyStatus
from trading.strategy.sessions import SessionEntryPolicy

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_demo_config_keeps_trading_disabled():
    config = load_config("demo", CONFIG_DIR)
    assert config.environment == "demo"
    assert config.risk.trading_enabled is False
    assert config.broker.expected_account_mode == "HEDGING"


def test_instrument_policies_separate_platform_from_trading():
    # 設計書 §30: 4 ペアとも platform 対応、live 発注は USDJPY のみ。
    config = load_config("production", CONFIG_DIR)
    assert config.instruments["USDJPY"].trading_enabled is True
    for symbol in ("EURUSD", "GBPUSD", "GBPJPY"):
        assert config.instruments[symbol].platform_enabled is True
        assert config.instruments[symbol].trading_enabled is False


def test_trading_without_platform_is_a_config_error():
    with pytest.raises(ValidationError):
        InstrumentPolicy(platform_enabled=False, trading_enabled=True)


def test_position_caps_stay_single_in_live_overlays():
    # 多ペア live を明示的に判断するまで、live 系 overlay は portfolio 全体
    # でも従来どおり 1 本に固定する。
    for env in ("production", "micro_live"):
        config = load_config(env, CONFIG_DIR)
        assert config.risk.max_open_positions_per_symbol == 1
        assert config.risk.max_open_positions_portfolio == 1


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
    assert strategy.params_for("USDJPY").param("resistance_lookback", 0) == 20


def test_backtest_enables_risk_gate_for_simulated_orders():
    # The broker is unreachable in backtest by wiring (simulator only);
    # trading_enabled must not zero out simulated fills.
    config = load_config("backtest", CONFIG_DIR)
    assert config.risk.trading_enabled is True


@pytest.mark.parametrize("interval", [0, -0.1, float("inf"), float("nan")])
def test_tick_poll_interval_must_be_a_positive_duration(interval):
    # An unusable interval has to fail at load: reaching time.sleep() with it
    # means the collector already started, so the host just restarts it.
    with pytest.raises(ValidationError):
        MarketConfig(tick_poll_interval_seconds=interval)


@pytest.mark.parametrize("bound", ["pre_hours", "post_hours"])
def test_event_window_bounds_may_not_be_negative(bound):
    # A negative bound inverts the window: its start lands after its end, so
    # active_at() is never true and a configured halt stops applying without
    # anything reporting it.
    with pytest.raises(ValidationError):
        EventRiskWindowSettings(**{bound: -1})


def test_an_unrecognised_event_mode_is_rejected():
    # Falling back to NORMAL on a typo would read as "nothing is near".
    with pytest.raises(ValidationError):
        EventRiskWindowSettings(scalp="PAUSE")


def test_unknown_environment_rejected():
    with pytest.raises(ValueError):
        load_config("staging", CONFIG_DIR)


def test_no_hardcoded_instruments_in_strategy_config():
    config = load_config("demo", CONFIG_DIR)
    for strategy in config.strategies.values():
        assert strategy.instruments, "instruments must come from configuration"


def test_every_platform_instrument_has_a_unit_cap():
    # trading_enabled の昇格だけで取引可能になるよう、platform 対応ペアには
    # 必ず unit cap を定義しておく（欠けると SYMBOL_LIMIT_CONFIGURED で
    # 全 reject になり、昇格手順が config 2 箇所の同時変更になってしまう）。
    config = load_config("production", CONFIG_DIR)
    for symbol, policy in config.instruments.items():
        if policy.platform_enabled:
            assert symbol in config.risk.max_units_per_symbol, symbol


def test_base_session_profiles_load_as_typed_policies():
    config = load_config("demo", CONFIG_DIR)

    assert (
        config.session_profiles["usdjpy_core"].sessions["new_york"]
        is SessionEntryPolicy.PREFERRED
    )
    assert config.session_profiles["usdjpy_core"].entry_allowed("tokyo") is True


def test_unknown_session_profile_reference_is_rejected():
    with pytest.raises(ValidationError):
        AppConfig(
            environment="demo",
            strategies={
                "probe": StrategyConfig(
                    strategy_id="probe",
                    parameters={
                        "instruments": {
                            "USDJPY": {"session_profile": "missing_profile"}
                        }
                    },
                )
            },
        )


def test_every_platform_instrument_has_a_spread_ceiling():
    config = load_config("production", CONFIG_DIR)

    for symbol, policy in config.instruments.items():
        if policy.platform_enabled:
            assert symbol in config.risk.absolute_max_spread_pips, symbol


def test_existing_strategy_parameter_formats_load_together():
    config = load_config("backtest", CONFIG_DIR)

    scalp = config.strategies["failed_spike_reversal"]
    intraday = config.strategies["post_event_failed_breakout"]
    swing = config.strategies["monetary_policy_convergence"]
    assert scalp.params_for("USDJPY").param("atr_period", 0) == 14
    assert intraday.params_for("USDJPY").param("resistance_lookback", 0) == 20
    assert swing.params_for("USDJPY").param("support_lookback", 0) == 30
