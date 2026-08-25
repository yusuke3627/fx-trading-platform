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

import json
from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta
from hashlib import sha256

from trading.data.intervention.features import (
    KIND_TO_STATUS,
    RECENCY_WINDOW_DAYS,
    intervention_risk_inputs,
)
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.data.policy.features import latest_policy_score, us2y_features
from trading.data.policy.scoring import EVENT_TYPES, SCORING_VERSION
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig, intervention_risk_score
from trading.storage.repository import EventRepository, MacroObservationRepository

# How far back the US2Y vintage read reaches, on the known_at axis. This
# bounds the query, it does not define the feature: us2y_features needs 20
# business days of observations (its z-score window) and each day's vintage
# becomes known the following morning, so 28 calendar days would already
# cover it. 90 keeps revisions of those days in view and leaves the margin a
# collection outage would consume, while still reading ~65 rows instead of
# the whole chain every cycle.
US2Y_VINTAGE_LOOKBACK = timedelta(days=90)


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

    @property
    def store(self) -> InMemoryFeatureStore:
        """The store refresh() feeds — the one a StrategyContext must hold for
        strategies to see what was refreshed."""
        return self._store

    def refresh(self, now: datetime) -> None:
        self._store.replace(self.snapshot(now))

    def snapshot(self, now: datetime) -> dict[str, float]:
        """Every feature computable from the series visible at `now`."""
        values: dict[str, float | None] = {}

        values.update(
            us2y_features(
                self._observations.known_before(
                    US_TREASURY_2Y_YIELD, now, now - US2Y_VINTAGE_LOOKBACK
                )
            )
        )

        values[f.BOJ_POLICY_SHIFT_SCORE] = self._policy_score(now, EVENT_TYPES["BOJ"])
        values[f.FED_POLICY_SHIFT_SCORE] = self._policy_score(now, EVENT_TYPES["FED"])

        # Event-derived inputs only; the price-derived ones join once someone
        # computes them. No recent intervention yields no inputs at all, and
        # that is absence of evidence, not evidence of calm — the feature goes
        # missing rather than scoring 0.
        #
        # The known_at bound cannot drop a relevant event: nothing is known
        # before it happens, so known_at >= the action date's midnight. The
        # extra day bridges the arithmetic mismatch — the recency check counts
        # whole DATES and keeps an action exactly RECENCY_WINDOW_DAYS old,
        # whose midnight lies up to a day before the same span measured back
        # from this instant.
        recency = now - timedelta(days=RECENCY_WINDOW_DAYS + 1)
        intervention_events = [
            event
            for kind in KIND_TO_STATUS
            for event in self._events.known_before(now, kind, since=recency)
        ]
        inputs = intervention_risk_inputs(intervention_events, now)
        if inputs:
            values[f.INTERVENTION_RISK] = intervention_risk_score(inputs, self._intervention)

        return {name: value for name, value in values.items() if value is not None}

    def change_instants(self, start: datetime, end: datetime) -> list[datetime]:
        """Every known_at at which a row snapshot() reads arrives, for replays
        of [start, end] — the schedule a ReplayFeatureTimeline steps on.

        Each read mirrors the corresponding snapshot() bound taken at
        now=start, so a row that no snapshot in the range can see is not an
        instant. Rows already known at `start` are still included: their
        arrival is folded into the timeline's opening refresh, but their
        lookback EXPIRY can fall inside the replay, and the timeline derives
        expiries from these instants.
        """
        observations, events = self._replay_rows(start, end)
        return sorted(
            [row.known_at for row in observations]
            + [event.known_at for event in events]
        )

    def dataset_fingerprint(self, start: datetime, end: datetime) -> str:
        """Content hash of every stored row a replay of [start, end] can read.

        Ticks alone do not identify a research dataset: the macro, policy and
        intervention rows decide what the strategy gates saw, and a
        re-collected vintage or a re-scored meeting changes results under the
        same commit, config and ticks. Row identities (UUIDs) are excluded so
        the same content re-ingested hashes the same.
        """
        observations, events = self._replay_rows(start, end)
        lines = [
            f"obs|{row.series}|{row.observation_period}|{row.value}"
            f"|{row.known_at.isoformat()}"
            for row in observations
        ] + [
            f"event|{event.event_type}|{event.known_at.isoformat()}"
            f"|{json.dumps(event.payload, sort_keys=True, default=str)}"
            for event in events
        ]
        digest = sha256()
        for line in sorted(lines):
            digest.update(line.encode())
            digest.update(b"\n")
        return digest.hexdigest()

    def _replay_rows(self, start: datetime, end: datetime):
        """The rows any snapshot inside [start, end] can read, window by
        window: US2Y inside its lookback, policy unbounded, intervention
        inside the recency bound taken at `start` (a superset of every later
        instant's bound)."""
        observations = self._observations.known_before(
            US_TREASURY_2Y_YIELD, end, start - US2Y_VINTAGE_LOOKBACK
        )
        events: list = []
        for event_type in (EVENT_TYPES["BOJ"], EVENT_TYPES["FED"]):
            events.extend(self._events.known_before(end, event_type))
        recency = start - timedelta(days=RECENCY_WINDOW_DAYS + 1)
        for kind in KIND_TO_STATUS:
            events.extend(self._events.known_before(end, kind, since=recency))
        return observations, events

    def _policy_score(self, now: datetime, event_type: str) -> float | None:
        # A re-tuned scoring algorithm re-ingests past meetings as NEW events
        # (scoring.py versions them instead of rewriting history), so the same
        # meeting can sit in the store under several versions with one
        # known_at. The reader has to pick, and it picks the version this
        # build computes — features and scorer then agree by construction.
        return latest_policy_score(
            [
                event
                for event in self._events.known_before(now, event_type)
                if event.payload.get("scoring_version") == SCORING_VERSION
            ]
        )


class ReplayFeatureTimeline:
    """Steps a StoredFeatureSource along a replay clock.

    Live refreshes on a poll; a replay delivering a million ticks cannot ask
    the repositories a million times, and does not need to — between changes
    of the underlying rows the snapshot is constant. The caller supplies the
    instants at which rows become known (it has them: a replay's dataset is
    loaded up front), and advance() recomputes only when the clock crosses
    one.

    Two classes of change have no new row behind them. A row EXPIRES when the
    US2Y lookback window slides past its known_at — visible only while a
    series has stopped updating, but in exactly that regime live would drop
    the value mid-day and a replay must not keep it until midnight. Every
    change instant therefore schedules its own expiry alongside it; the
    surplus instants this creates for rows other bounds govern cost one cheap
    refresh each. And intervention risk decays on DATE arithmetic, so the
    snapshot moves at UTC midnights while an intervention is inside its
    recency window; advance() also refreshes on the first tick of each new
    date. (The intervention query window's own slide needs no instant: its
    date-based cutoff has always zeroed an event's contribution by the
    midnight before the timestamp window drops the row.)
    """

    def __init__(self, source: StoredFeatureSource, changes: Sequence[datetime]) -> None:
        self._source = source
        self._changes = sorted(
            [*changes, *(instant + US2Y_VINTAGE_LOOKBACK for instant in changes)]
        )
        self._position = 0
        self._refreshed_at: datetime | None = None

    @property
    def store(self) -> InMemoryFeatureStore:
        return self._source.store

    def reset(self, start: datetime) -> None:
        """Position the timeline at the replay's opening instant.

        Rows already known at `start` belong in the very first evaluation —
        a backtest of August must see July's meeting scores from its first
        tick — so this is a real refresh, not just a pointer rewind.
        """
        self._position = bisect_right(self._changes, start)
        self._refresh(start)

    def advance(self, now: datetime) -> None:
        crossed = False
        while self._position < len(self._changes) and self._changes[self._position] <= now:
            self._position += 1
            crossed = True
        if crossed or self._refreshed_at is None or now.date() != self._refreshed_at.date():
            self._refresh(now)

    def _refresh(self, now: datetime) -> None:
        self._source.refresh(now)
        self._refreshed_at = now
