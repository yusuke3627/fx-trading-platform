"""Regime service: market-environment labels composed from features."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from enum import StrEnum
from typing import Protocol

from trading.intelligence import features as f
from trading.intelligence.features import FeatureStore


class RegimeLabel(StrEnum):
    USD_POLICY_HAWKISH = "USD_POLICY_HAWKISH"
    JPY_POLICY_HAWKISH = "JPY_POLICY_HAWKISH"
    RISK_OFF = "RISK_OFF"
    VOLATILITY_HIGH = "VOLATILITY_HIGH"
    INTERVENTION_RISK_HIGH = "INTERVENTION_RISK_HIGH"


class RegimeService(Protocol):
    def active(self) -> AbstractSet[str]: ...


RegimeRule = Callable[[FeatureStore], bool]


def default_rules(thresholds: dict[str, float] | None = None) -> dict[RegimeLabel, RegimeRule]:
    t = {
        "usd_hawkish_min": 0.0,
        "jpy_hawkish_min": 0.0,
        "intervention_risk_high": 0.6,
        **(thresholds or {}),
    }

    def _gt(name: str, threshold: float) -> RegimeRule:
        def rule(store: FeatureStore) -> bool:
            value = store.get(name)
            return value is not None and value > threshold

        return rule

    return {
        RegimeLabel.USD_POLICY_HAWKISH: _gt(f.FED_EXPECTED_PATH_CHANGE, t["usd_hawkish_min"]),
        RegimeLabel.JPY_POLICY_HAWKISH: _gt(f.BOJ_EXPECTED_PATH_CHANGE, t["jpy_hawkish_min"]),
        RegimeLabel.INTERVENTION_RISK_HIGH: _gt(f.INTERVENTION_RISK, t["intervention_risk_high"]),
    }


class RuleBasedRegimeService:
    def __init__(
        self,
        store: FeatureStore,
        rules: dict[RegimeLabel, RegimeRule] | None = None,
    ) -> None:
        self._store = store
        self._rules = rules if rules is not None else default_rules()

    def active(self) -> frozenset[str]:
        return frozenset(label for label, rule in self._rules.items() if rule(self._store))
