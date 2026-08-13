"""Claim protocol for execution commands.

Workers claim READY rows with FOR UPDATE SKIP LOCKED (queue-like table
processing without lock contention). Recovery rules (all staleness-gated so a
runtime sweep can never seize a healthy in-flight command):

- CLAIMED + lease expired + broker request never started  -> READY
- CLAIMED + lease expired + broker request started        -> UNKNOWN
- SUBMITTING + submitting_at older than the timeout       -> UNKNOWN
"""
from __future__ import annotations

from datetime import datetime, timedelta

from trading.domain.order import CommandState, ExecutionCommand
from trading.oms.state_machine import transition

CLAIM_SQL = """
UPDATE execution_commands
SET state = 'CLAIMED',
    claimed_by = %(worker)s,
    claimed_at = %(now)s,
    claim_expires_at = %(expires)s
WHERE id = (
    SELECT id
    FROM execution_commands
    WHERE state = 'READY'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id;
"""


def mark_claimed(
    command: ExecutionCommand, worker: str, now: datetime, lease_seconds: int
) -> ExecutionCommand:
    claimed = transition(command, CommandState.CLAIMED, now=now)
    return claimed.model_copy(
        update={
            "claimed_by": worker,
            "claimed_at": now,
            "claim_expires_at": now + timedelta(seconds=lease_seconds),
        }
    )


def mark_submitting(command: ExecutionCommand, now: datetime) -> ExecutionCommand:
    submitting = transition(command, CommandState.SUBMITTING, now=now)
    return submitting.model_copy(update={"submitting_at": now})


def mark_broker_request_started(
    command: ExecutionCommand, now: datetime
) -> ExecutionCommand:
    """Recorded immediately before the broker API call: from this instant a
    broker side effect may exist and blind retry is forbidden."""
    return command.model_copy(update={"broker_request_started_at": now})


DEFAULT_SUBMITTING_TIMEOUT_SECONDS = 60


def recovery_state(
    command: ExecutionCommand,
    now: datetime,
    *,
    submitting_timeout_seconds: int = DEFAULT_SUBMITTING_TIMEOUT_SECONDS,
) -> CommandState | None:
    """State a stale command should recover to; None when no recovery applies.

    A command is only considered stale after its lease expiry (CLAIMED) or the
    submitting timeout (SUBMITTING): declaring a live submission UNKNOWN would
    falsely trip halt_on_unknown_order and freeze new risk.
    """
    if command.state is CommandState.CLAIMED:
        lease_expired = (
            command.claim_expires_at is not None and now >= command.claim_expires_at
        )
        if not lease_expired:
            return None
        if command.broker_request_started_at is not None:
            return CommandState.UNKNOWN
        return CommandState.READY
    if command.state is CommandState.SUBMITTING:
        started = command.submitting_at or command.broker_request_started_at
        if started is None:
            # No timestamp to measure staleness against: startup recovery
            # after a crash, where no worker can still be in flight.
            return CommandState.UNKNOWN
        if now >= started + timedelta(seconds=submitting_timeout_seconds):
            return CommandState.UNKNOWN
        return None
    return None
