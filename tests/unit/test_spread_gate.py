from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.strategy.base import StrategyConfig
from trading.strategy.spread_gate import SpreadGate


def test_primary_gate_passes_at_boundary_and_rejects_above_it():
    gate = SpreadGate(max_spread_to_atr=0.5, absolute_max_spread_pips=None)

    assert gate.allows(
        spread=Decimal("0.005"), atr=0.01, pip_size=Decimal("0.01")
    )
    assert not gate.allows(
        spread=Decimal("0.0051"), atr=0.01, pip_size=Decimal("0.01")
    )


def test_absolute_ceiling_passes_at_boundary_and_rejects_above_it():
    gate = SpreadGate(max_spread_to_atr=None, absolute_max_spread_pips=Decimal("1.5"))

    assert gate.allows(
        spread=Decimal("0.015"), atr=0.01, pip_size=Decimal("0.01")
    )
    assert not gate.allows(
        spread=Decimal("0.0151"), atr=0.01, pip_size=Decimal("0.01")
    )


def test_primary_and_absolute_ceiling_are_combined_with_and():
    gate = SpreadGate(max_spread_to_atr=0.5, absolute_max_spread_pips=Decimal("1.0"))

    assert not gate.allows(
        spread=Decimal("0.015"), atr=0.04, pip_size=Decimal("0.01")
    )


def test_unconfigured_gate_allows_quote():
    gate = SpreadGate(max_spread_to_atr=None, absolute_max_spread_pips=None)

    assert gate.allows(
        spread=Decimal(1), atr=0.01, pip_size=Decimal("0.01")
    )


def test_primary_gate_rejects_non_positive_atr():
    gate = SpreadGate(max_spread_to_atr=0.5, absolute_max_spread_pips=None)

    assert not gate.allows(
        spread=Decimal("0.001"), atr=0, pip_size=Decimal("0.01")
    )


def test_gate_is_built_from_resolved_strategy_parameters():
    config = StrategyConfig(
        strategy_id="probe",
        parameters={
            "defaults": {"spread_gate": {"max_spread_to_atr": "0.4"}},
            "instruments": {"USDJPY": {"absolute_max_spread_pips": "1.5"}},
        },
    )

    gate = SpreadGate.from_params(config.params_for("USDJPY"))

    assert gate.max_spread_to_atr == 0.4
    assert gate.absolute_max_spread_pips == Decimal("1.5")


@pytest.mark.parametrize(
    "parameters",
    [
        # 文字列: 読み込みは通るが最初の市場イベントで .get() が落ちる形
        {"defaults": {"spread_gate": "disabled"}},
        # bool は float へ 1.0 に黙って変換されるので明示的に弾く
        {"defaults": {"spread_gate": {"max_spread_to_atr": True}}},
        # bool の ceiling は Decimal("True") で実行時に落ちる形
        {"instruments": {"USDJPY": {"absolute_max_spread_pips": True}}},
        {"instruments": {"USDJPY": {"absolute_max_spread_pips": 0}}},
        # 打ち間違いのキーは黙って無視されず起動時に止まる
        {"defaults": {"spread_gate": {"max_spred_to_atr": "0.4"}}},
    ],
    ids=["non-mapping", "bool-ratio", "bool-ceiling", "zero-ceiling", "unknown-key"],
)
def test_invalid_spread_gate_settings_are_rejected_at_load(parameters):
    with pytest.raises(ValidationError):
        StrategyConfig(strategy_id="probe", parameters=parameters)


def test_string_thresholds_from_yaml_are_settled_at_load():
    config = StrategyConfig(
        strategy_id="probe",
        parameters={
            "defaults": {"spread_gate": {"max_spread_to_atr": "0.5"}},
            "instruments": {"USDJPY": {"absolute_max_spread_pips": "1.5"}},
        },
    )

    gate = SpreadGate.from_params(config.params_for("USDJPY"))

    assert gate.absolute_max_spread_pips == Decimal("1.5")
