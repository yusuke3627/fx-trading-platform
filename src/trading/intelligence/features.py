"""Feature store and canonical feature names.

A missing feature returns None; strategies treat missing data as "no trade",
never as zero.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

# Canonical feature names (strategy inputs). A name states what is measured,
# not what the reader wishes were measured: there is no "fed_expected_path" —
# what exists is a meeting-statement score and a Treasury yield series, and
# the constants say so. (docs/research/2026-08-15, principle 6.)
DISTANCE_FROM_VWAP = "distance_from_vwap"
ATR_NORMALIZED_BREAKOUT = "atr_normalized_breakout"
SHORT_TERM_MOMENTUM = "short_term_momentum"
RATE_DIFFERENTIAL_CHANGE = "rate_differential_change"
INTERVENTION_RISK = "intervention_risk"

# Named honestly but not yet produced: a US release-surprise series needs a
# consensus source this platform does not have. Readers treat it as any other
# missing feature.
US_DATA_SURPRISE = "us_data_surprise"

# Policy proxies (data/policy). US2Y is a policy PROXY, not a Fed-expectation
# measure; rate_differential_change stays unused until a JP2Y series exists.
US2Y_LEVEL = "us2y_level"
US2Y_CHANGE_1D = "us2y_change_1d"
US2Y_CHANGE_5D = "us2y_change_5d"
US2Y_ZSCORE_20D = "us2y_zscore_20d"
BOJ_POLICY_SHIFT_SCORE = "boj_policy_shift_score"
FED_POLICY_SHIFT_SCORE = "fed_policy_shift_score"


class FeatureStore(Protocol):
    def get(self, name: str, symbol: str | None = None) -> float | None: ...


class InMemoryFeatureStore:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str | None], float] = {}

    def set(self, name: str, value: float, symbol: str | None = None) -> None:
        self._values[(name, symbol)] = value

    def replace(self, values: Mapping[str, float]) -> None:
        """Swap the whole store for `values` (global, symbol-less features).

        Strategies hold this object by reference, so mutation is how a refresh
        reaches them. Swapping rather than setting is what lets a feature go
        missing again: inputs that disappeared must read as None, not as the
        value they had when they were last computable.
        """
        self._values = {(name, None): value for name, value in values.items()}

    def get(self, name: str, symbol: str | None = None) -> float | None:
        if (name, symbol) in self._values:
            return self._values[(name, symbol)]
        return self._values.get((name, None))

    def values(self) -> dict[str, float]:
        """Every global feature currently set, for reporting what the gates
        can see. Symbol-scoped entries are left out: a report of them needs a
        symbol to be meaningful, and nothing sets them today."""
        return {name: value for (name, symbol), value in self._values.items() if symbol is None}
