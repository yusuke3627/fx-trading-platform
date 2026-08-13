"""Feature store and canonical feature names.

A missing feature returns None; strategies treat missing data as "no trade",
never as zero.
"""
from __future__ import annotations

from typing import Protocol

# Canonical feature names (strategy inputs).
DISTANCE_FROM_VWAP = "distance_from_vwap"
ATR_NORMALIZED_BREAKOUT = "atr_normalized_breakout"
SHORT_TERM_MOMENTUM = "short_term_momentum"
RATE_DIFFERENTIAL_CHANGE = "rate_differential_change"
INTERVENTION_RISK = "intervention_risk"

US_RATE_EXPECTATION_CHANGE = "us_rate_expectation_change"
US_DATA_SURPRISE = "us_data_surprise"
US2Y_CHANGE = "us2y_change"
FED_EXPECTED_PATH_CHANGE = "fed_expected_path_change"
BOJ_EXPECTED_PATH_CHANGE = "boj_expected_path_change"
BOJ_HAWKISH_SHIFT = "boj_hawkish_shift"


class FeatureStore(Protocol):
    def get(self, name: str, symbol: str | None = None) -> float | None: ...


class InMemoryFeatureStore:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str | None], float] = {}

    def set(self, name: str, value: float, symbol: str | None = None) -> None:
        self._values[(name, symbol)] = value

    def get(self, name: str, symbol: str | None = None) -> float | None:
        if (name, symbol) in self._values:
            return self._values[(name, symbol)]
        return self._values.get((name, None))
