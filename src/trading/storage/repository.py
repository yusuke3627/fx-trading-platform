"""Repository protocols.

Storage interfaces used by OMS, risk and reconciliation. The PostgreSQL
implementation lives in storage/postgres.py; strategies never see these.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from trading.domain.account import AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.fill import Fill
from trading.domain.order import CommandState, ExecutionCommand


class CommandRepository(Protocol):
    def insert(self, command: ExecutionCommand) -> None: ...

    def get(self, command_id: str) -> ExecutionCommand | None: ...

    def save_state(self, command: ExecutionCommand) -> None: ...

    def claim_next(self, worker: str, lease_seconds: int) -> ExecutionCommand | None: ...

    def in_state(self, state: CommandState) -> Sequence[ExecutionCommand]: ...


class FillRepository(Protocol):
    def insert(self, fill: Fill) -> None: ...

    def has_deal(self, broker_deal_id: str) -> bool: ...


class AccountSnapshotRepository(Protocol):
    def insert(self, snapshot: AccountSnapshot) -> None: ...

    def since(self, t: datetime) -> Sequence[AccountSnapshot]: ...

    def latest(self) -> AccountSnapshot | None: ...


class EventRepository(Protocol):
    def insert(self, event: EventEnvelope) -> None: ...

    def known_before(
        self, t: datetime, event_type: str | None = None
    ) -> Sequence[EventEnvelope]: ...


class IncidentRepository(Protocol):
    def insert(self, severity: str, kind: str, detail: dict, occurred_at: datetime) -> None: ...
