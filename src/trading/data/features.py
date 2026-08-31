"""Feature store and currency state refresh from the point-in-time series.

The bridge the feature pipeline was missing: policy, intervention and macro
each compute features as pure functions over already-visible rows, and the
strategies read a FeatureStore — but nothing connected the two, so every run
evaluated against an empty store.

strategy が見る PIT スナップショットはここが唯一の作り手で、feature store
（数値の特徴量）と通貨 state（CurrencyState）の両方を供給する。分けないのは
どちらも同じ行を読み、同じ時刻で入れ替わらなければならないため — 別々に
refresh すると、feature は新しい会合を見ているのに通貨 state はまだ見て
いない、という不整合が replay に出る。frozen() / change_instants() /
dataset_fingerprint() も 1 つの行の集合から答える（ADR-022）。

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

from trading.data.factor_series import (
    MacroFactorSeries,
    PolicyScoreFactorSeries,
)
from trading.data.intervention.features import (
    KIND_TO_STATUS,
    RECENCY_WINDOW_DAYS,
    intervention_risk_inputs,
)
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.data.policy.features import latest_policy_score, us2y_features
from trading.data.policy.scoring import EVENT_TYPES, SCORING_VERSION
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.money import Currency
from trading.intelligence import features as f
from trading.intelligence.currency import (
    ChainedFactorSeries,
    CurrencyScoreConfig,
    CurrencyState,
    CurrencyStateService,
    CurrencyStateStore,
)
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
    """Keeps a FeatureStore and a CurrencyStateStore current from the PIT series.

    Holds the stores it feeds: refresh() swaps their whole contents, so an
    input that disappeared goes absent instead of surviving as a stale value
    from an earlier cycle.
    """

    def __init__(
        self,
        observations: MacroObservationRepository,
        events: EventRepository,
        intervention: InterventionRiskConfig,
        store: InMemoryFeatureStore,
        currency: CurrencyScoreConfig | None = None,
        currency_states: CurrencyStateStore | None = None,
    ) -> None:
        self._observations = observations
        self._events = events
        self._intervention = intervention
        self._store = store
        # store は設定を内側で持つ。呼び出し側が別々に組むと、正規化の窓と
        # 読み出し幅が食い違っても静かに通ってしまう。
        self._currency_config = currency or CurrencyScoreConfig()
        self._currency_states = currency_states or CurrencyStateStore(self._currency_config)
        self._macro_factors = MacroFactorSeries(
            observations, self._currency_config.normalization
        )
        self._currency = CurrencyStateService(
            ChainedFactorSeries(
                self._macro_factors, PolicyScoreFactorSeries(events)
            ),
            self._currency_config,
        )

    @property
    def store(self) -> InMemoryFeatureStore:
        """The store refresh() feeds — the one a StrategyContext must hold for
        strategies to see what was refreshed."""
        return self._store

    @property
    def currency_states(self) -> CurrencyStateStore:
        """The store refresh() feeds with per-currency state — the one a
        StrategyContext must hold for strategies to see it."""
        return self._currency_states

    def refresh(self, now: datetime) -> None:
        self._store.replace(self.snapshot(now))
        self._currency_states.replace(self.currency_snapshot(now))

    def currency_snapshot(self, now: datetime) -> dict[Currency, CurrencyState]:
        """`now` 時点で観測が 1 つでもある通貨の state。

        1 つも観測が無い通貨は directional_score も confidence も 0 になる
        が、それは「方向感が無い」ではなく「何も見えていない」。store へ
        入れると PairState が射影できてしまうので落とす。

        freshness ではなく観測の有無で判定する。公表間隔の長い factor は
        次の公表を待つ間 freshness が 0 になるが（#89）、値そのものは
        依然として最新の事実であり、方向感は語れる。
        """
        states: dict[Currency, CurrencyState] = {}
        for currency in Currency:
            state = self._currency.state(currency, now)
            if any(score is not None for score in state.factor_scores.values()):
                states[currency] = state
        return states

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

    def frozen(self, start: datetime, end: datetime) -> StoredFeatureSource:
        """A source answering every read of a [start, end] replay from one
        consistent load of the stored rows.

        The reads behind change_instants(), each snapshot() and
        dataset_fingerprint() are separate queries on the live connection; a
        collector inserting history between them could put rows in the
        fingerprint the replay never saw, or surface a row at an instant the
        change schedule does not contain. Freezing pins all three to the same
        rows.
        """
        observations, events = self._replay_rows(start, end)
        return StoredFeatureSource(
            _FrozenObservations(observations),
            _FrozenEvents(events),
            self._intervention,
            self._store,
            self._currency_config,
            # 同じ store を引き継ぐ: strategy が参照で持っているのはこの
            # インスタンスで、凍結した側が別の器を作ると refresh が届かない。
            self._currency_states,
        )

    def _replay_rows(self, start: datetime, end: datetime):
        """The rows any snapshot inside [start, end] can read, window by
        window: each macro series inside the widest lookback that reads it,
        policy unbounded, intervention inside the recency bound taken at
        `start` (a superset of every later instant's bound)."""
        observations: list[EconomicObservation] = []
        for series, lookback in self.observation_windows().items():
            observations.extend(
                self._observations.known_before(series, end, start - lookback)
            )
        events: list = []
        for event_type in (EVENT_TYPES["BOJ"], EVENT_TYPES["FED"]):
            events.extend(self._events.known_before(end, event_type))
        recency = start - timedelta(days=RECENCY_WINDOW_DAYS + 1)
        for kind in KIND_TO_STATUS:
            events.extend(self._events.known_before(end, kind, since=recency))
        return observations, events

    def observation_windows(self) -> dict[str, timedelta]:
        """系列ごとの読み出し幅。同じ系列を 2 つの用途が読むときは広い方。

        US2Y は feature（20 営業日の z）と RATES factor（正規化の窓）の
        両方が読む。狭い方で凍結すると、片方の読み手だけが replay で
        欠測する。
        """
        windows = dict(self._macro_factors.read_windows())
        windows[US_TREASURY_2Y_YIELD] = max(
            windows.get(US_TREASURY_2Y_YIELD, timedelta(0)), US2Y_VINTAGE_LOOKBACK
        )
        return windows

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


class _FrozenObservations:
    """One load of observation rows, answering known_before the way the
    stored repository does: series match, since-exclusive known_at window."""

    def __init__(self, observations: Sequence[EconomicObservation]) -> None:
        self._observations = list(observations)

    def known_before(
        self, series: str, t: datetime, since: datetime
    ) -> list[EconomicObservation]:
        return [
            o
            for o in self._observations
            if o.series == series and since < o.known_at <= t
        ]


class _FrozenEvents:
    """One load of event rows, mirroring the stored repository's filters."""

    def __init__(self, events: Sequence[EventEnvelope]) -> None:
        self._events = list(events)

    def known_before(
        self,
        t: datetime,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[EventEnvelope]:
        return [
            e
            for e in self._events
            if e.known_at <= t
            and (event_type is None or e.event_type == event_type)
            and (since is None or e.known_at > since)
        ]


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

    @property
    def currency_states(self) -> CurrencyStateStore:
        return self._source.currency_states

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
