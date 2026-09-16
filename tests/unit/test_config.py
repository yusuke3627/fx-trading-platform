from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.config import (
    EventRiskWindowSettings,
    InstrumentPolicy,
    MarketConfig,
    load_config,
)
from trading.domain.risk import EventRiskMode
from trading.strategy.base import LIVE_ELIGIBLE_STATUSES, StrategyStatus
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
    assert strategy.status is StrategyStatus.SHADOW
    assert strategy.enabled is True
    # Base parameters survive the overlay merge.
    assert strategy.params_for("USDJPY").param("resistance_lookback", 0) == 20


def test_micro_live_has_no_live_eligible_strategy_until_one_is_promoted():
    # live_eligible な status は実注文を出す側（runner.StrategyBinding.live_eligible）。
    # 昇格判定を通った戦略は現時点で無い（post_event_failed_breakout は H5 判定で
    # 根拠なし）ので、合成後の設定に live 対象の status が無いことをここで固定する。
    # 昇格判定済みの戦略が出たら、その strategy_id をこの期待値に足す。
    config = load_config("micro_live", CONFIG_DIR)

    live_eligible = sorted(
        strategy_id
        for strategy_id, strategy in config.strategies.items()
        if strategy.status in LIVE_ELIGIBLE_STATUSES
    )
    assert live_eligible == []


def test_backtest_enables_risk_gate_for_simulated_orders():
    # The broker is unreachable in backtest by wiring (simulator only);
    # trading_enabled must not zero out simulated fills.
    config = load_config("backtest", CONFIG_DIR)
    assert config.risk.trading_enabled is True


def test_backtest_relaxes_account_level_loss_halts() -> None:
    config = load_config("backtest", CONFIG_DIR)
    assert config.risk.daily_loss_halt_pct == Decimal("100.00")
    assert config.risk.rolling_24h_loss_halt_pct == Decimal("100.00")
    assert config.risk.high_water_mark_drawdown_halt_pct == Decimal("100.00")

    # 研究でも建玉・数量・イベントモードの制約は緩和しない。
    assert config.risk.max_open_positions_per_symbol == 1
    assert config.risk.max_units_per_symbol["USDJPY"] == 1000
    assert config.risk.event_mode_default is EventRiskMode.REDUCED

    # demo は trading_enabled だけを上書きするため、その他の risk 設定を比較できる。
    demo = load_config("demo", CONFIG_DIR)
    excluded = {
        "trading_enabled",
        "daily_loss_halt_pct",
        "rolling_24h_loss_halt_pct",
        "high_water_mark_drawdown_halt_pct",
    }
    assert config.risk.model_dump(exclude=excluded) == demo.risk.model_dump(exclude=excluded)


def test_live_and_shadow_overlays_keep_account_level_loss_halts() -> None:
    for env in ("shadow", "demo", "micro_live", "production"):
        config = load_config(env, CONFIG_DIR)
        assert config.risk.daily_loss_halt_pct == Decimal("0.75")
        assert config.risk.rolling_24h_loss_halt_pct == Decimal("1.00")
        assert config.risk.high_water_mark_drawdown_halt_pct == Decimal("3.00")


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


def test_unknown_session_profile_reference_is_rejected(tmp_path):
    (tmp_path / "base.yaml").write_text(
        """
session_profiles:
  usdjpy_core:
    tokyo: ALLOWED
strategies:
  probe:
    parameters:
      instruments:
        USDJPY:
          session_profile: missing_profile
""",
        encoding="utf-8",
    )
    (tmp_path / "demo.yaml").write_text("", encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown session_profile"):
        load_config("demo", tmp_path)


def test_base_config_binds_session_profiles_to_strategies():
    config = load_config("demo", CONFIG_DIR)

    assert (
        config.strategies["monetary_policy_convergence"].session_profile_for("USDJPY")
        == config.session_profiles["usdjpy_core"]
    )
    assert (
        config.strategies["failed_spike_reversal"].session_profile_for("USDJPY")
        == config.session_profiles["usdjpy_scalp_research"]
    )
    assert config.strategies["post_event_failed_breakout"].session_profile_for("USDJPY") is None


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


def test_broker_rate_limits_load_from_base_config():
    config = load_config("demo", CONFIG_DIR)

    assert config.broker.rate_limit.per_symbol_requests_per_second == 5
    assert config.broker.rate_limit.market_entries_per_second == 1


def test_base_config_fixes_arbitrator_coefficients():
    config = load_config("shadow", CONFIG_DIR)

    assert config.arbitrator.existing_exposure_penalty_r == Decimal("0.10")
    assert config.arbitrator.max_pairs_per_triangle == 2


def test_range_edge_reversal_loads_preregistered_parameters():
    config = load_config("backtest", CONFIG_DIR)
    strategy = config.strategies["range_edge_reversal"]
    assert strategy.enabled is False
    assert strategy.status is StrategyStatus.RESEARCH_ONLY
    assert strategy.instruments == ["USDJPY"]
    assert strategy.timeframes.role("regime") == "1h"
    assert strategy.timeframes.role("entry") == "5m"
    expected = {
        "range_lookback_bars": 24,
        "range_stale_hours": 72,
        "range_slope_lookback": 6,
        "range_slope_max_atr": 0.5,
        "range_width_min_atr": 1.5,
        "ema_period": 20,
        "atr_period": 14,
        "reentry_max_bars": 6,
        "entry_band_fraction": 0.2,
        "stop_buffer_atr": 0.25,
        "min_reward_to_risk": 1.5,
        "take_profit_enabled": True,
        "horizon_exit_enabled": True,
        "expected_horizon_seconds": 3600,
        "session_end_buffer_seconds": 3600,
        "spread_gate": {"max_spread_to_atr": "0.5"},
    }
    params = strategy.params_for("USDJPY")
    for key, value in expected.items():
        actual = params.param(key, None)
        assert actual == value, key
        assert type(actual) is type(value), key
    assert params.param("absolute_max_spread_pips", None) == "1.5"
    assert strategy.session_profile_for("USDJPY") == config.session_profiles["usdjpy_core"]


@pytest.mark.parametrize("environment", ["backtest", "demo", "shadow", "micro_live", "production"])
def test_range_edge_reversal_stays_disabled_in_every_overlay(environment):
    strategy = load_config(environment, CONFIG_DIR).strategies["range_edge_reversal"]
    assert strategy.enabled is False
    assert strategy.status is StrategyStatus.RESEARCH_ONLY
    assert strategy.runs is False
