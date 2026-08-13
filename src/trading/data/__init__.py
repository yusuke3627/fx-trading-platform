"""Data collection layer.

Collectors turn external sources into point-in-time EventEnvelopes. Every
event records source, source_uri, retrieved_at, published_at, known_at,
payload_hash and raw_uri; the source registry is a view over events, not a
separate store.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from trading.domain.event import EventEnvelope


class Collector(Protocol):
    source: str

    def collect(self) -> Iterable[EventEnvelope]: ...
