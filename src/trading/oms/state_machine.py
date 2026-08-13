"""OMS command state machine.

CLAIMED (a worker holds the row, no broker side effect yet) and SUBMITTING
(broker call started, side effect possible) are strictly separated:

- CLAIMED with an expired lease and no broker request may return to READY.
- SUBMITTING is NEVER reclaimed as READY; a crash there goes to UNKNOWN.
- UNKNOWN transitions only through reconciliation evidence.
"""
from __future__ import annotations

from datetime import datetime

from trading.domain.order import CommandState, ExecutionCommand

_ALLOWED: dict[CommandState, frozenset[CommandState]] = {
    CommandState.CREATED: frozenset(
        {CommandState.RISK_APPROVED, CommandState.REJECTED, CommandState.CANCELLED,
         CommandState.EXPIRED}
    ),
    CommandState.RISK_APPROVED: frozenset(
        {CommandState.READY, CommandState.CANCELLED, CommandState.EXPIRED}
    ),
    CommandState.READY: frozenset(
        {CommandState.CLAIMED, CommandState.CANCELLED, CommandState.EXPIRED}
    ),
    CommandState.CLAIMED: frozenset(
        {CommandState.SUBMITTING, CommandState.READY, CommandState.CANCELLED,
         CommandState.EXPIRED, CommandState.UNKNOWN}
    ),
    CommandState.SUBMITTING: frozenset(
        {CommandState.ACKNOWLEDGED, CommandState.PARTIAL_FILL, CommandState.FILLED,
         CommandState.REJECTED, CommandState.UNKNOWN}
    ),
    CommandState.ACKNOWLEDGED: frozenset(
        {CommandState.PARTIAL_FILL, CommandState.FILLED, CommandState.CANCELLED,
         CommandState.REJECTED, CommandState.EXPIRED, CommandState.UNKNOWN}
    ),
    CommandState.PARTIAL_FILL: frozenset(
        {CommandState.PARTIAL_FILL, CommandState.FILLED, CommandState.CANCELLED,
         CommandState.EXPIRED, CommandState.UNKNOWN}
    ),
    CommandState.FILLED: frozenset(),
    CommandState.REJECTED: frozenset(),
    CommandState.CANCELLED: frozenset(),
    CommandState.EXPIRED: frozenset(),
    CommandState.UNKNOWN: frozenset(
        {CommandState.FILLED, CommandState.PARTIAL_FILL, CommandState.CANCELLED,
         CommandState.REJECTED, CommandState.EXPIRED}
    ),
}


class InvalidTransition(RuntimeError):
    pass


def can_transition(current: CommandState, new: CommandState) -> bool:
    return new in _ALLOWED[current]


def transition(
    command: ExecutionCommand,
    new_state: CommandState,
    *,
    now: datetime,
    via_reconciliation: bool = False,
) -> ExecutionCommand:
    """Return a copy of the command in the new state, enforcing guards."""
    current = command.state

    if not can_transition(current, new_state):
        raise InvalidTransition(f"{current} -> {new_state} is not allowed")

    if current is CommandState.UNKNOWN and not via_reconciliation:
        raise InvalidTransition(
            "UNKNOWN commands are resolved only through reconciliation"
        )

    if current is CommandState.CLAIMED and new_state is CommandState.READY:
        lease_expired = (
            command.claim_expires_at is not None and now >= command.claim_expires_at
        )
        if not lease_expired:
            raise InvalidTransition("CLAIMED -> READY requires an expired lease")
        if command.broker_request_started_at is not None:
            raise InvalidTransition(
                "CLAIMED with a started broker request must go to UNKNOWN, not READY"
            )

    updates: dict = {"state": new_state}
    if new_state is CommandState.READY:
        updates.update(
            {"claimed_by": None, "claimed_at": None, "claim_expires_at": None}
        )
    return command.model_copy(update=updates)
