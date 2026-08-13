"""Fundamental engine: point-in-time macro events -> features."""
from __future__ import annotations

from typing import Callable

from trading.domain.event import EventEnvelope
from trading.intelligence.features import InMemoryFeatureStore

EventHandler = Callable[[EventEnvelope, InMemoryFeatureStore], None]


class FundamentalEngine:
    """Dispatches stored events to registered feature updaters.

    Handlers must only use event fields with known_at discipline; the engine
    itself never looks at wall-clock time.
    """

    def __init__(self, features: InMemoryFeatureStore) -> None:
        self.features = features
        self._handlers: dict[str, list[EventHandler]] = {}

    def register(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def on_event(self, event: EventEnvelope) -> None:
        for handler in self._handlers.get(event.event_type, []):
            handler(event, self.features)
