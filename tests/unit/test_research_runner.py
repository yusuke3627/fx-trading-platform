"""Research runner pieces: the replay axis rewrite and the change schedule."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import FakeEventRepository, FakeObservationRepository
from trading.backtest.research import broker_label_to_known, reconstructed
from trading.data.features import US2Y_VINTAGE_LOOKBACK, StoredFeatureSource
from trading.data.intervention.features import RECENCY_WINDOW_DAYS
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.market import Tick
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
END = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)


def tick(at: datetime, ingested_at: datetime) -> Tick:
    return Tick(
        symbol="USDJPY",
        bid=Decimal("157.000"),
        ask=Decimal("157.005"),
        time=at,
        received_at=ingested_at,
    )


def observation(known_at: datetime) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series=US_TREASURY_2Y_YIELD,
        observation_period=known_at.date().isoformat(),
        value=Decimal("3.50"),
        unit="percent",
        source="ALFRED",
        retrieved_at=known_at,
        known_at=known_at,
    )


def event(event_type: str, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source="TEST",
        payload={"action_date": known_at.date().isoformat()},
        retrieved_at=known_at,
        known_at=known_at,
    )


def make_source(observations=(), events=()) -> StoredFeatureSource:
    return StoredFeatureSource(
        FakeObservationRepository(observations),
        FakeEventRepository(events),
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
    )


def test_reconstructed_rewrites_known_time_from_the_broker_stamp():
    # An archived tick carries the backfill run's wall clock as received_at —
    # far in the tick's own future. The replay axis must come from the broker
    # stamp instead. August sits inside US DST, so the server runs at UTC+3.
    ingested = START + timedelta(days=400)
    ticks = [tick(START + timedelta(seconds=i), ingested) for i in range(3)]

    rewritten = reconstructed(ticks, timedelta(hours=7))

    assert [t.known_time for t in rewritten] == [
        START - timedelta(hours=3) + timedelta(seconds=i) for i in range(3)
    ]
    # The broker axis itself is untouched: bars keep folding event_time.
    assert [t.time for t in rewritten] == [t.time for t in ticks]
    assert rewritten[0].bid == ticks[0].bid


def test_reconstruction_follows_the_servers_dst_calendar():
    # The server's wall clock is New York's plus the anchor year-round, so
    # the same label maps 3h back in summer and only 2h back in winter.
    anchor = timedelta(hours=7)
    summer = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    assert broker_label_to_known(summer, anchor) == summer - timedelta(hours=3)
    assert broker_label_to_known(winter, anchor) == winter - timedelta(hours=2)

    # Across the 2025-11-02 fall-back: Friday close maps at -3h, the Sunday
    # session open at -2h, and the mapping stays monotonic through the gap.
    friday_close = datetime(2025, 10, 31, 20, 0, tzinfo=UTC)
    sunday_open = datetime(2025, 11, 3, 0, 30, tzinfo=UTC)
    assert broker_label_to_known(friday_close, anchor) == friday_close - timedelta(
        hours=3
    )
    assert broker_label_to_known(sunday_open, anchor) == sunday_open - timedelta(
        hours=2
    )


def test_period_coverage_rejects_the_shapes_that_would_report_plausibly():
    from trading.backtest.research import ensure_period_covered

    read_from = START - timedelta(days=10)
    first = tick(read_from + timedelta(minutes=30), START)
    last = tick(END - timedelta(minutes=30), START)
    ensure_period_covered((first, last), read_from, START, END)

    # A weekend at an edge is closure, not missing data: Friday's last quote
    # against a Monday-morning --to passes, because the gap holds almost no
    # open-market time.
    weekend_end = datetime(2026, 8, 17, 0, 15, tzinfo=UTC)  # Monday
    friday_last = tick(datetime(2026, 8, 14, 23, 45, tzinfo=UTC), START)
    ensure_period_covered((first, friday_last), read_from, START, weekend_end)

    with pytest.raises(SystemExit):
        ensure_period_covered(None, read_from, START, END)
    # Only lead-in ticks: nothing would be evaluated.
    with pytest.raises(SystemExit):
        ensure_period_covered((first, first), read_from, START, END)
    # History begins days into the requested warm-up: starved indicators.
    with pytest.raises(SystemExit):
        ensure_period_covered(
            (tick(START - timedelta(days=2), START), last), read_from, START, END
        )
    # A missing trading day at the tail of a WEEKDAY period is not excused
    # by any fixed calendar allowance.
    weekday_end = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)  # Wednesday
    with pytest.raises(SystemExit):
        ensure_period_covered(
            (first, tick(weekday_end - timedelta(days=1), START)),
            read_from,
            START,
            weekday_end,
        )


def test_covered_stream_judges_the_ticks_it_actually_delivers():
    from trading.backtest.data import TickDigest
    from trading.backtest.research import covered_reconstructed_stream

    read_from = START - timedelta(days=10)
    anchor = timedelta(hours=7)

    # Happy path: reconstruction and digest ride the pass; exhaustion passes
    # the coverage verdict.
    good = [
        tick(read_from + timedelta(minutes=30), START),
        tick(START + timedelta(hours=1), START),
        tick(END - timedelta(minutes=30), START),
    ]
    digest = TickDigest()
    delivered = list(
        covered_reconstructed_stream(iter(good), read_from, START, END, anchor, digest)
    )
    assert digest.count == 3
    assert [t.time for t in delivered] == [t.time for t in good]
    assert delivered[0].known_time != good[0].known_time

    # A head gap fails on the FIRST tick — before hours of replay are spent.
    late_head = [tick(START - timedelta(days=2), START)]
    stream = covered_reconstructed_stream(
        iter(late_head), read_from, START, END, anchor, TickDigest()
    )
    with pytest.raises(SystemExit):
        next(stream)

    # Only warm-up ticks fail at exhaustion, not silently.
    warmup_only = [tick(read_from + timedelta(minutes=30), START)]
    stream = covered_reconstructed_stream(
        iter(warmup_only), read_from, START, END, anchor, TickDigest()
    )
    with pytest.raises(SystemExit):
        list(stream)

    # An empty stream fails with the coverage message, not a bare engine error.
    with pytest.raises(SystemExit):
        list(
            covered_reconstructed_stream(
                iter([]), read_from, START, END, anchor, TickDigest()
            )
        )


def test_period_bounds_accept_only_utc_stamped_labels():
    # A +03:00 input meant as "the broker's summer midnight" would be
    # normalized to 21:00Z and silently read a different label range.
    import argparse

    from trading.backtest.research import broker_label

    assert broker_label("2026-08-18T00:00:00+00:00") == datetime(
        2026, 8, 18, tzinfo=UTC
    )
    assert broker_label("2026-08-18T00:00:00Z") == datetime(2026, 8, 18, tzinfo=UTC)
    with pytest.raises(argparse.ArgumentTypeError):
        broker_label("2026-08-18T00:00:00+03:00")
    with pytest.raises(argparse.ArgumentTypeError):
        broker_label("2026-08-18T00:00:00")


def test_a_label_inside_the_dst_transition_hour_is_refused():
    # New York switches at 02:00 Sunday — inside the FX weekend close, so no
    # correct dataset carries such a label; folding one onto either
    # occurrence could replay a price an hour early (look-ahead).
    anchor = timedelta(hours=7)
    repeated = datetime(2025, 11, 2, 8, 30, tzinfo=UTC)  # NY 01:30, twice
    skipped = datetime(2026, 3, 8, 9, 30, tzinfo=UTC)  # NY 02:30, never

    with pytest.raises(ValueError):
        broker_label_to_known(repeated, anchor)
    with pytest.raises(ValueError):
        broker_label_to_known(skipped, anchor)


def test_change_instants_mirror_the_snapshot_windows():
    inside_lookback = observation(START - US2Y_VINTAGE_LOOKBACK + timedelta(days=1))
    during = observation(START + timedelta(days=5))
    beyond_lookback = observation(START - US2Y_VINTAGE_LOOKBACK - timedelta(days=10))

    # The policy read is unbounded, so an old score's arrival is an instant
    # even when only its lookback expiry could fall inside the replay.
    old_policy = event("FED_POLICY_SHIFT_SCORE", START - timedelta(days=200))

    recent_intervention = event("INTERVENTION_REPORTED", START + timedelta(days=2))
    stale_intervention = event(
        "INTERVENTION_REPORTED",
        START - timedelta(days=RECENCY_WINDOW_DAYS + 5),
    )

    source = make_source(
        observations=[during, beyond_lookback, inside_lookback],
        events=[old_policy, recent_intervention, stale_intervention],
    )

    instants = source.change_instants(START, END)

    assert instants == sorted(
        [
            old_policy.known_at,
            inside_lookback.known_at,
            recent_intervention.known_at,
            during.known_at,
        ]
    )


def test_dataset_fingerprint_follows_content_not_row_identity():
    # The factories mint fresh UUIDs per row, so equality across two builds
    # proves the hash covers content, the way a re-ingested archive repeats it.
    rows = {
        "observations": [observation(START + timedelta(days=1))],
        "events": [event("FED_POLICY_SHIFT_SCORE", START - timedelta(days=30))],
    }
    first = make_source(**rows).dataset_fingerprint(START, END)

    assert make_source(**rows).dataset_fingerprint(START, END) == first


def test_dataset_fingerprint_changes_only_with_rows_a_replay_can_read():
    observations = [observation(START + timedelta(days=1))]
    base = make_source(observations=observations).dataset_fingerprint(START, END)

    grown = make_source(
        observations=observations,
        events=[event("INTERVENTION_REPORTED", START + timedelta(days=2))],
    ).dataset_fingerprint(START, END)
    out_of_window = make_source(
        observations=[
            *observations,
            observation(START - US2Y_VINTAGE_LOOKBACK - timedelta(days=10)),
        ],
    ).dataset_fingerprint(START, END)

    assert grown != base
    assert out_of_window == base


def test_frozen_source_ignores_rows_arriving_after_the_load():
    # The research run freezes the PIT rows once; a collector inserting on
    # the same database afterwards must change neither the snapshots nor the
    # fingerprint of the running replay.
    events = FakeEventRepository(
        [event("FED_POLICY_SHIFT_SCORE", START - timedelta(days=30))]
    )
    live = StoredFeatureSource(
        FakeObservationRepository(),
        events,
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
    )
    frozen = live.frozen(START, END)
    before = frozen.dataset_fingerprint(START, END)

    events.events.append(event("INTERVENTION_REPORTED", START + timedelta(days=1)))

    assert frozen.dataset_fingerprint(START, END) == before
    assert frozen.change_instants(START, END) != live.change_instants(START, END)
    assert live.dataset_fingerprint(START, END) != before


def test_every_registered_strategy_computes_a_positive_warmup():
    # The research runner sizes its lead-in read from this; a zero warmup
    # would start a real strategy against empty indicator windows.
    from trading.strategy.base import StrategyConfig
    from trading.strategy.registry import STRATEGIES

    for strategy_class in STRATEGIES.values():
        config = StrategyConfig(
            strategy_id=strategy_class.strategy_id, instruments=["USDJPY"]
        )
        assert strategy_class.warmup(config) > timedelta(0), strategy_class.strategy_id


def test_warmup_follows_the_evaluated_configuration():
    from trading.strategy.base import StrategyConfig
    from trading.strategy.registry import STRATEGIES

    swing = STRATEGIES["monetary_policy_convergence"]
    default = StrategyConfig(strategy_id=swing.strategy_id, instruments=["USDJPY"])
    # The trend gate's EMA(50) on 1d needs ~50 trading days of lead-in.
    assert swing.warmup(default) >= timedelta(days=70)

    intraday = STRATEGIES["post_event_failed_breakout"]
    base_config = StrategyConfig(strategy_id=intraday.strategy_id, instruments=["USDJPY"])
    widened = base_config.model_copy(
        update={"parameters": {"resistance_lookback": 500}}
    )
    assert intraday.warmup(widened) > intraday.warmup(base_config)
