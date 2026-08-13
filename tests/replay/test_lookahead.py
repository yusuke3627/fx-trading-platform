"""Replay determinism and look-ahead prohibition."""
import pytest

from tests.support import T0, at, make_bar, make_event, make_tick
from trading.backtest.clock import ClockRegressionError, ReplayClock
from trading.backtest.replay import LookaheadError, ReplayEngine, assert_visible, visible


def test_replay_clock_never_regresses():
    clock = ReplayClock(T0)
    clock.advance_to(at(minutes=5))
    with pytest.raises(ClockRegressionError):
        clock.advance_to(at(minutes=4))


def test_future_known_at_is_invisible():
    # A September revision must not be visible to an August backtest.
    clock = ReplayClock(at(days=2))
    revision = make_event(known_at=at(days=19), event_type="macro.cpi_revision")
    assert visible(revision, clock) is False
    with pytest.raises(LookaheadError):
        assert_visible(revision, clock)


def test_replay_delivers_in_known_at_order_with_clock_advanced():
    clock = ReplayClock(T0)
    engine = ReplayEngine(clock)

    items = [
        make_event(known_at=at(minutes=30), event_type="macro.late"),
        make_tick("158.840", "158.844", time=at(minutes=10)),
        make_event(known_at=at(minutes=20), event_type="macro.early"),
    ]

    seen: list[tuple[str, str]] = []

    def handler(item):
        label = getattr(item, "event_type", "tick")
        seen.append((label, clock.now().isoformat()))

    delivered = engine.run(items, handler)
    assert delivered == 3
    assert [label for label, _ in seen] == ["tick", "macro.early", "macro.late"]
    # The clock is already at the item's time when the handler observes it.
    assert seen[0][1] == at(minutes=10).isoformat()
    assert seen[2][1] == at(minutes=30).isoformat()


def test_bar_is_delivered_at_close_time_not_start():
    # A 1h bar's close does not exist at the bar's start; delivering it there
    # would leak one hour of the future into every bar-driven backtest.
    clock = ReplayClock(T0)
    engine = ReplayEngine(clock)
    bar = make_bar("158.80", "158.90", "158.70", "158.85", start=T0, timeframe="1h")

    delivered_at: list[str] = []
    engine.run([bar], lambda item: delivered_at.append(clock.now().isoformat()))
    assert delivered_at == [at(hours=1).isoformat()]


def test_replay_is_deterministic_across_runs():
    def run_once() -> list[str]:
        engine = ReplayEngine(ReplayClock(T0))
        items = [
            make_tick("158.840", "158.844", time=at(minutes=3)),
            make_event(known_at=at(minutes=1)),
            make_event(known_at=at(minutes=2)),
        ]
        order: list[str] = []
        engine.run(items, lambda i: order.append(getattr(i, "event_type", "tick")))
        return order

    assert run_once() == run_once()
