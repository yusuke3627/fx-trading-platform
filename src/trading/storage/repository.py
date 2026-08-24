"""Repository protocols.

Storage interfaces used by OMS, risk and reconciliation. The PostgreSQL
implementation lives in storage/postgres.py; strategies never see these.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from trading.domain.account import AccountSnapshot
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.fill import Fill
from trading.domain.market import Bar, Tick
from trading.domain.order import CommandState, ExecutionCommand


class StaleCommandStateError(RuntimeError):
    """The command's DB state no longer matches what the caller held; the
    caller must re-read and go through reconciliation instead of writing."""


class CommandRepository(Protocol):
    def insert(self, command: ExecutionCommand) -> None: ...

    def get(self, command_id: str) -> ExecutionCommand | None: ...

    # Compare-and-set: raises StaleCommandStateError when the row is no
    # longer in expected_state (e.g. a timeout sweep moved it to UNKNOWN
    # while a slow worker was still holding the old object).
    def save_state(
        self, command: ExecutionCommand, expected_state: CommandState
    ) -> None: ...

    # `now` is injected (never read from a wall clock inside the repository)
    # so claim leases stay deterministic under replay.
    def claim_next(
        self, worker: str, lease_seconds: int, now: datetime
    ) -> ExecutionCommand | None: ...

    def in_state(self, state: CommandState) -> Sequence[ExecutionCommand]: ...


class FillRepository(Protocol):
    def insert(self, fill: Fill) -> None: ...

    def has_deal(self, broker_deal_id: str) -> bool: ...


class AccountSnapshotRepository(Protocol):
    # Every call names the account. The high-water mark and the day baseline
    # are read back out of this series, so a terminal switched from the demo
    # login to a live one against the same database would otherwise inherit
    # the other account's peak equity — an unrelated number that reads as a
    # drawdown.
    def insert(self, account_id: str, snapshot: AccountSnapshot) -> None: ...

    # Visibility is capped at `t`, the same as for ticks and bars: a row
    # observed after it was not knowable then. A live evaluation freezes its
    # clock for the length of a cycle, so a snapshot the collector writes
    # midway through one must not appear inside it.
    def known_before(
        self, account_id: str, t: datetime, since: datetime
    ) -> Sequence[AccountSnapshot]: ...

    def latest_known_before(
        self, account_id: str, t: datetime
    ) -> AccountSnapshot | None: ...


class EventRepository(Protocol):
    def insert(self, event: EventEnvelope) -> None: ...

    # Insert unless an event with the same id already exists; returns whether
    # a row was added. For ingests whose event ids are deterministic
    # (policy meeting scores), re-running is a no-op instead of an error.
    def insert_new(self, event: EventEnvelope) -> bool: ...

    def known_before(
        self, t: datetime, event_type: str | None = None
    ) -> Sequence[EventEnvelope]: ...


class MarketTickRepository(Protocol):
    # Returns the number of ticks actually stored. Re-running an ingestion
    # over an already-collected range is a normal way to fill a gap, and it
    # has to be able to report what it filled rather than what it sent.
    def insert_many(
        self, ticks: Sequence[Tick], *, source: str, ingestion_run: UUID
    ) -> int: ...

    # `since` is mandatory: the tick table grows without bound, so a
    # visibility query is a window over the series, never a scan of a
    # symbol's whole history.
    def known_before(
        self, symbol: str, t: datetime, since: datetime
    ) -> Sequence[Tick]: ...

    # The newest quote visible at `t`, by broker time. Unlike known_before this
    # takes no window, because the newest quote may be arbitrarily old — a
    # weekend, a market holiday, a collector that has been down — and a window
    # would answer "no price" for a market that simply is not quoting.
    def latest_known_before(self, symbol: str, t: datetime) -> Tick | None: ...


class MarketBarRepository(Protocol):
    def insert_many(self, bars: Sequence[Bar]) -> int: ...

    # Mirrors MarketDataService.bars(): the most recent `count` bars visible
    # at `t`, oldest first.
    def known_before(
        self, symbol: str, timeframe: str, t: datetime, count: int
    ) -> Sequence[Bar]: ...


class MacroObservationRepository(Protocol):
    # Returns the number of rows actually stored: re-collecting a period that
    # is already ingested is the normal way to pick up revisions, and only
    # genuinely new vintages count. A row whose value equals the vintage
    # immediately preceding its known_at is not a revision and is skipped —
    # scheduled forward re-collection must not grow the chain.
    def insert_many(self, observations: Sequence[EconomicObservation]) -> int: ...

    # All vintages of a series visible at `t`, oldest first. The caller
    # derives first print vs revision from the known_at order within one
    # (series, observation_period).
    def known_before(self, series: str, t: datetime) -> Sequence[EconomicObservation]: ...


class IncidentRepository(Protocol):
    def insert(self, severity: str, kind: str, detail: dict, occurred_at: datetime) -> None: ...
