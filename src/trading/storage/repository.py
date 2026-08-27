"""Repository protocols.

Storage interfaces used by OMS, risk and reconciliation. The PostgreSQL
implementation lives in storage/postgres.py; strategies never see these.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from trading.domain.account import AccountSnapshot
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.fill import Fill
from trading.domain.intent import PositionIntent
from trading.domain.market import Bar, Tick
from trading.domain.order import CommandState, ExecutionCommand
from trading.domain.risk import RiskDecision
from trading.domain.signal import StrategySignal
from trading.domain.swap import SwapSnapshot


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

    # `since` (a known_at lower bound) is optional, unlike the tick and
    # observation windows: some readers genuinely need the newest event
    # however old it is (the latest policy score), and events are sparse
    # enough per type that an unbounded read stays small.
    def known_before(
        self,
        t: datetime,
        event_type: str | None = None,
        since: datetime | None = None,
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

    # The first row known_before would return. Answers "is there a quote at or
    # after this broker time" without reading the series it would have to scan
    # to find out.
    def earliest_known_after(
        self, symbol: str, t: datetime, since: datetime
    ) -> Tick | None: ...

    # Every quote with event_time in [start, end), oldest first. Unlike the
    # visibility reads this does not filter on received_at: a research replay
    # reads a recorded period in full and reconstructs each tick's known time
    # from its broker timestamp (ADR-014), so reception time is not a filter.
    def between(
        self, symbol: str, start: datetime, end: datetime
    ) -> Sequence[Tick]: ...

    # between() as a stream: same rows and order, delivered without holding
    # the period in memory — a months-long research replay reads tens of
    # millions of rows, which no host here materializes twice.
    def stream_between(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[Tick]: ...

    # The first and last row between() would return, without the rows in
    # between: the research runner's period-coverage checks need exactly the
    # edges.
    def bounds_between(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[Tick, Tick] | None: ...


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

    # Vintages of a series visible at `t` that became known after `since`,
    # oldest first. `since` is mandatory for the same reason the tick window
    # is: the vintage chain grows without bound, and a reader polling this
    # every few seconds must not re-model the whole history each pass. The
    # caller derives first print vs revision from the known_at order within
    # one (series, observation_period).
    def known_before(
        self, series: str, t: datetime, since: datetime
    ) -> Sequence[EconomicObservation]: ...


class SwapSnapshotRepository(Protocol):
    def insert(self, snapshot: SwapSnapshot) -> None: ...

    # Snapshots visible at `t`, oldest first — the backtest loads the whole
    # visible series once and does its latest-known lookups in memory.
    def known_before(self, symbol: str, t: datetime) -> Sequence[SwapSnapshot]: ...

    def latest_known_before(self, symbol: str, t: datetime) -> SwapSnapshot | None: ...


class DecisionRepository(Protocol):
    # One decision trail: what a strategy saw, what Portfolio made of it, and
    # how Risk graded that. The three tables are chained by foreign key
    # (signal <- intent <- decision), so they are written together or not at
    # all — a half-written trail cannot be read back as either outcome.
    #
    # Every call names the account: an intent is sized from its equity and a
    # decision is graded against its loss history, so the same trail means
    # something different depending on which account it was made for.
    #
    # A signal that produced several intents is recorded once per intent and
    # must stay one row, so writing it again is a no-op rather than an error.
    def record(
        self,
        account_id: str,
        signal: StrategySignal,
        intent: PositionIntent,
        decision: RiskDecision,
    ) -> None: ...

    # A signal that produced no intent at all — sizing landed below one volume
    # step, or the stop distance was not usable. It still happened, and a
    # strategy whose signals never become intents is exactly what a shadow run
    # is there to reveal.
    def record_signal(self, account_id: str, signal: StrategySignal) -> None: ...

    # The most recent trails, newest first: what a run decided, read back whole.
    def recent(
        self, account_id: str, limit: int
    ) -> Sequence[tuple[StrategySignal, PositionIntent, RiskDecision]]: ...


class IncidentRepository(Protocol):
    def insert(self, severity: str, kind: str, detail: dict, occurred_at: datetime) -> None: ...
