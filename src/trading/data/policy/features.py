"""Policy-proxy feature computation (point-in-time).

Pure functions over already-visible data: the caller fetches with
`known_before(..., t)` so everything here is automatically PIT-safe. Missing
input yields None — never zero — matching the feature-store doctrine that
missing data means "no trade", not "neutral".

float is acceptable here (indicator-style computation, not money).
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope

# Trailing observations (business days) in the z-score window.
ZSCORE_WINDOW = 20


def yield_series(observations: Sequence[EconomicObservation]) -> list[float]:
    """Latest visible vintage per day, in day order.

    `known_before` returns every visible vintage ordered by known_at, so for
    a revised day the later vintage overwrites the earlier one here.
    """
    by_day: dict[str, float] = {}
    for o in observations:
        by_day[o.observation_period] = float(o.value)
    return [value for _, value in sorted(by_day.items())]


def us2y_features(observations: Sequence[EconomicObservation]) -> dict[str, float | None]:
    """US2Y_LEVEL / CHANGE_1D / CHANGE_5D / ZSCORE_20D.

    Offsets are in published observations (business days), which is what a
    daily Treasury series naturally provides. ZSCORE_20D is the latest level
    against the trailing ZSCORE_WINDOW levels (including the latest); a flat
    window has no scale, so its z-score is None.
    """
    series = yield_series(observations)

    def change(offset: int) -> float | None:
        if len(series) <= offset:
            return None
        return series[-1] - series[-1 - offset]

    zscore: float | None = None
    if len(series) >= ZSCORE_WINDOW:
        window = series[-ZSCORE_WINDOW:]
        mean = sum(window) / len(window)
        variance = sum((v - mean) ** 2 for v in window) / len(window)
        if variance > 0:
            zscore = (series[-1] - mean) / math.sqrt(variance)

    return {
        "us2y_level": series[-1] if series else None,
        "us2y_change_1d": change(1),
        "us2y_change_5d": change(5),
        "us2y_zscore_20d": zscore,
    }


def latest_policy_score(events: Sequence[EventEnvelope]) -> float | None:
    """The most recent visible policy-shift score, or None if no meeting is
    visible yet. Caller filters by event_type (BOJ vs FED)."""
    latest: EventEnvelope | None = None
    for event in events:
        if latest is None or event.known_at > latest.known_at:
            latest = event
    if latest is None:
        return None
    return float(latest.payload["score"])
