"""Provider protocols for optional / paid data sources.

Buying a data source later must never require rewriting strategy code: a new
provider implements one of these protocols and is wired in configuration.
Paid providers pass the Data Upgrade Gate (ablation, expected incremental
value > 2x data cost) before adoption.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol

from trading.domain.event import EventEnvelope


class EconomicExpectationProvider(Protocol):
    def expectations(self, as_of: datetime) -> Iterable[EventEnvelope]: ...


class NewsProvider(Protocol):
    def news(self, since: datetime) -> Iterable[EventEnvelope]: ...


class JapanMarketProvider(Protocol):
    """J-Quants free tier: historical feature provider for JP-market vs USD/JPY
    research, not a live signal source (data is delayed)."""

    def observations(self, since: datetime) -> Iterable[EventEnvelope]: ...


class PolicyExpectationProvider(Protocol):
    """Initially disabled / manual research snapshots; a future CME FedWatch
    implementation slots in here."""

    def policy_path(self, as_of: datetime) -> Iterable[EventEnvelope]: ...


class DisabledProvider:
    """Explicitly-off provider: yields nothing, never errors."""

    def expectations(self, as_of: datetime) -> Iterable[EventEnvelope]:
        return ()

    def news(self, since: datetime) -> Iterable[EventEnvelope]:
        return ()

    def observations(self, since: datetime) -> Iterable[EventEnvelope]:
        return ()

    def policy_path(self, as_of: datetime) -> Iterable[EventEnvelope]:
        return ()
