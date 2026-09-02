import pytest
from pydantic import ValidationError

from trading.strategy.base import StrategyConfig
from trading.strategy.parameters import StrategyParameters
from trading.strategy.scalp.failed_spike_reversal import FailedSpikeReversalStrategy
from trading.strategy.sessions import SessionEntryPolicy, SessionProfile


def test_flat_parameters_are_treated_as_defaults():
    config = StrategyConfig(strategy_id="probe", parameters={"period": 14})

    assert config.parameters.defaults == {"period": 14}
    assert config.params_for("USDJPY").param("period", 0) == 14


def test_model_copy_parameter_update_takes_validated_parameters():
    # model_copy(update=...) は validator を通らない。parameters を差し替える
    # 側（research の sweep 等）が StrategyParameters を渡す契約を固定する。
    config = StrategyConfig(strategy_id="probe").model_copy(
        update={"parameters": StrategyParameters.model_validate({"period": 21})}
    )

    assert config.params_for("USDJPY").param("period", 0) == 21


def test_instrument_override_wins_and_other_symbols_use_defaults():
    config = StrategyConfig(
        strategy_id="probe",
        parameters={
            "defaults": {"period": 14},
            "instruments": {"USDJPY": {"period": 21}},
        },
    )

    assert config.params_for("USDJPY").param("period", 0) == 21
    assert config.params_for("EURUSD").param("period", 0) == 14


def test_nested_parameter_groups_are_replaced_as_a_whole():
    config = StrategyConfig(
        strategy_id="probe",
        parameters={
            "defaults": {"entry": {"window_seconds": "300", "mode": "primary"}},
            "instruments": {"USDJPY": {"entry": {"window_seconds": "240"}}},
        },
    )

    assert config.params_for("USDJPY").param("entry", {}) == {"window_seconds": "240"}


def test_structured_parameters_reject_unknown_top_level_keys():
    with pytest.raises(ValidationError):
        StrategyConfig(
            strategy_id="probe",
            parameters={"defaults": {"period": 14}, "instrument": {}},
        )


def test_session_profile_is_resolved_per_instrument():
    config = StrategyConfig(
        strategy_id="probe",
        session_profiles={"usdjpy_core": {"tokyo": "ALLOWED"}},
        parameters={
            "instruments": {
                "USDJPY": {"session_profile": "usdjpy_core"},
            }
        },
    )

    assert config.params_for("USDJPY").session_profile == "usdjpy_core"
    assert config.params_for("EURUSD").session_profile is None
    assert config.session_profile_for("USDJPY") == SessionProfile(
        sessions={"tokyo": SessionEntryPolicy.ALLOWED}
    )
    assert config.session_profile_for("EURUSD") is None


@pytest.mark.parametrize(
    "parameters",
    [
        {"defaults": {"session_profile": "missing_profile"}},
        {"instruments": {"USDJPY": {"session_profile": "missing_profile"}}},
    ],
)
def test_unknown_session_profile_reference_is_rejected_at_the_config_boundary(parameters):
    with pytest.raises(ValidationError, match="unknown session_profile"):
        StrategyConfig(
            strategy_id="probe",
            session_profiles={"usdjpy_core": {"tokyo": "ALLOWED"}},
            parameters=parameters,
        )


def test_retention_window_uses_largest_instrument_override():
    config = StrategyConfig(
        strategy_id=FailedSpikeReversalStrategy.strategy_id,
        instruments=["USDJPY", "EURUSD"],
        parameters={
            "defaults": {"atr_period": 14},
            "instruments": {"USDJPY": {"atr_period": 300}},
        },
    )

    assert FailedSpikeReversalStrategy.bar_window(config) == 301


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (SessionEntryPolicy.PREFERRED, True),
        (SessionEntryPolicy.ALLOWED, True),
        (SessionEntryPolicy.SHADOW_ONLY, False),
        (SessionEntryPolicy.DISABLED, False),
    ],
)
def test_session_profile_entry_policy(policy, expected):
    profile = SessionProfile(sessions={"tokyo": policy})

    assert profile.entry_allowed("TOKYO") is expected
    assert profile.entry_allowed("london") is False


def test_session_profile_rejects_unknown_session_name():
    with pytest.raises(ValidationError):
        SessionProfile(sessions={"sydney": SessionEntryPolicy.ALLOWED})
