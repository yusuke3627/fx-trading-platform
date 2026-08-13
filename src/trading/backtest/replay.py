"""Replay engine.

Historical ticks and events are delivered in known_at order while advancing a
ReplayClock. Nothing with known_at in the future of the replay clock is ever
visible: a July CPI value revised in September must not be seen by an August
backtest.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading.backtest.clock import Clock, ReplayClock
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick


class LookaheadError(RuntimeError):
    pass


def visible(event: EventEnvelope, clock: Clock) -> bool:
    return event.known_at <= clock.now()


def assert_visible(event: EventEnvelope, clock: Clock) -> None:
    if not visible(event, clock):
        raise LookaheadError(
            f"event {event.event_id} known_at={event.known_at} is in the future "
            f"of replay clock {clock.now()}"
        )


@dataclass(frozen=True)
class ReplayItem:
    at: datetime
    payload: EventEnvelope | Tick | Bar


def replay_time(item: EventEnvelope | Tick | Bar) -> datetime:
    """When an item becomes known: events at known_at, ticks at their time,
    bars at close time — delivering a bar at its start would hand the strategy
    the close price before it exists (systematic look-ahead)."""
    if isinstance(item, EventEnvelope):
        return item.known_at
    if isinstance(item, Tick):
        return item.time
    return item.close_time


class ReplayEngine:
    def __init__(self, clock: ReplayClock) -> None:
        self.clock = clock

    def run(
        self,
        items: Iterable[EventEnvelope | Tick | Bar],
        handler: Callable[[EventEnvelope | Tick | Bar], Any],
    ) -> int:
        """Deliver items in time order, advancing the clock first so a handler
        can never observe data before its known_at.

        Items sharing a timestamp keep their input order (stable sort), so a
        replay is deterministic for identical input sequences.
        """
        ordered = sorted(
            (ReplayItem(replay_time(i), i) for i in items), key=lambda r: r.at
        )
        delivered = 0
        for entry in ordered:
            self.clock.advance_to(entry.at)
            if isinstance(entry.payload, EventEnvelope):
                assert_visible(entry.payload, self.clock)
            handler(entry.payload)
            delivered += 1
        return delivered
