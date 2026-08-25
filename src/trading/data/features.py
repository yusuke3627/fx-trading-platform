"""Feature store refresh from the point-in-time series.

The bridge the feature pipeline was missing: policy, intervention and macro
each compute features as pure functions over already-visible rows, and the
strategies read a FeatureStore — but nothing connected the two, so every run
evaluated against an empty store.

Features are derived values, so nothing here persists: the store is
recomputed from the PIT repositories before each evaluation, and a process
restart loses only what the next refresh rebuilds. A feature whose inputs are
missing is absent from the store — None to the reader, never zero — which is
the doctrine the compute functions already follow.

Visibility is the caller's clock. Every read is `known_before(now)`, so the
same refresh against the same rows gives a backtest exactly what live saw.
"""
from __future__ import annotations

from datetime import datetime

from trading.data.intervention.features import KIND_TO_STATUS, intervention_risk_inputs
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.data.policy.features import latest_policy_score, us2y_features
from trading.data.policy.scoring import EVENT_TYPES
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig, intervention_risk_score
from trading.storage.repository import EventRepository, MacroObservationRepository


class StoredFeatureSource:
    """Keeps a FeatureStore current from the stored PIT series.

    Holds the store it feeds: refresh() swaps the store's whole contents, so a
    feature whose inputs disappeared goes absent instead of surviving as a
    stale value from an earlier cycle.
    """

    def __init__(
        self,
        observations: MacroObservationRepository,
        events: EventRepository,
        intervention: InterventionRiskConfig,
        store: InMemoryFeatureStore,
    ) -> None:
        self._observations = observations
        self._events = events
        self._intervention = intervention
        self._store = store

    def refresh(self, now: datetime) -> None:
        self._store.replace(self.snapshot(now))

    def snapshot(self, now: datetime) -> dict[str, float]:
        """Every feature computable from the series visible at `now`."""
        values: dict[str, float | None] = {}

        values.update(us2y_features(self._observations.known_before(US_TREASURY_2Y_YIELD, now)))

        values[f.BOJ_POLICY_SHIFT_SCORE] = latest_policy_score(
            self._events.known_before(now, EVENT_TYPES["BOJ"])
        )
        values[f.FED_POLICY_SHIFT_SCORE] = latest_policy_score(
            self._events.known_before(now, EVENT_TYPES["FED"])
        )

        # Event-derived inputs only; the price-derived ones join once someone
        # computes them. No recent intervention yields no inputs at all, and
        # that is absence of evidence, not evidence of calm — the feature goes
        # missing rather than scoring 0.
        intervention_events = [
            event
            for kind in KIND_TO_STATUS
            for event in self._events.known_before(now, kind)
        ]
        inputs = intervention_risk_inputs(intervention_events, now)
        if inputs:
            values[f.INTERVENTION_RISK] = intervention_risk_score(inputs, self._intervention)

        return {name: value for name, value in values.items() if value is not None}
