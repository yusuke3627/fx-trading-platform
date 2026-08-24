"""A live evaluation reads one instant, the way replay always did."""
from tests.support import FixedClock, at
from trading.live.clock import CycleClock


def test_time_holds_still_inside_a_cycle():
    source = FixedClock(at(minutes=1))
    clock = CycleClock(source)
    clock.begin_cycle()

    source.advance(minutes=5)

    assert clock.now() == at(minutes=1)


def test_the_next_cycle_picks_up_the_new_time():
    source = FixedClock(at(minutes=1))
    clock = CycleClock(source)
    clock.begin_cycle()
    source.advance(minutes=5)

    assert clock.begin_cycle() == at(minutes=6)
    assert clock.now() == at(minutes=6)


def test_outside_a_cycle_the_source_answers():
    source = FixedClock(at(minutes=1))
    clock = CycleClock(source)

    source.advance(minutes=5)

    assert clock.now() == at(minutes=6)
