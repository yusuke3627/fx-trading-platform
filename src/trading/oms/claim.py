"""Claim protocol for execution commands.

Workers claim READY rows with FOR UPDATE SKIP LOCKED (queue-like table
processing without lock contention). Recovery rules:

- CLAIMED + lease expired + broker request never started  -> READY
- CLAIMED or SUBMITTING + broker request started           -> UNKNOWN
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


def recovery_state(command: ExecutionCommand, now: datetime) -> CommandState | None:
    """State a stale command should recover to; None when no recovery applies."""
    if command.state is CommandState.CLAIMED:
        if command.broker_request_started_at is not None:
            return CommandState.UNKNOWN
        if command.claim_expires_at is not None and now >= command.claim_expires_at:
            return CommandState.READY
        return None
    if command.state is CommandState.SUBMITTING:
        return CommandState.UNKNOWN
    return None
