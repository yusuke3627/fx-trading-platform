import pytest

from tests.support import at, make_command
from trading.domain.order import CommandState
from trading.oms.state_machine import InvalidTransition, can_transition, transition


def test_happy_path_transitions():
    command = make_command(state=CommandState.CREATED)
    path = [
        CommandState.RISK_APPROVED,
        CommandState.READY,
        CommandState.CLAIMED,
        CommandState.SUBMITTING,
        CommandState.ACKNOWLEDGED,
        CommandState.PARTIAL_FILL,
        CommandState.FILLED,
    ]
    for state in path:
        command = transition(command, state, now=at(minutes=1))
    assert command.state is CommandState.FILLED


def test_submitting_is_never_reclaimed_as_ready():
    assert not can_transition(CommandState.SUBMITTING, CommandState.READY)
    command = make_command(state=CommandState.SUBMITTING)
    with pytest.raises(InvalidTransition):
        transition(command, CommandState.READY, now=at(minutes=1))


def test_unknown_requires_reconciliation_evidence():
    command = make_command(state=CommandState.UNKNOWN)
    with pytest.raises(InvalidTransition):
        transition(command, CommandState.FILLED, now=at(minutes=1))
    resolved = transition(
        command, CommandState.FILLED, now=at(minutes=1), via_reconciliation=True
    )
    assert resolved.state is CommandState.FILLED


def test_unknown_is_never_resubmitted():
    assert not can_transition(CommandState.UNKNOWN, CommandState.SUBMITTING)
    assert not can_transition(CommandState.UNKNOWN, CommandState.READY)


def test_claimed_to_ready_requires_expired_lease():
    command = make_command(
        state=CommandState.CLAIMED, claim_expires_at=at(minutes=10)
    )
    with pytest.raises(InvalidTransition):
        transition(command, CommandState.READY, now=at(minutes=5))

    recovered = transition(command, CommandState.READY, now=at(minutes=11))
    assert recovered.state is CommandState.READY
    assert recovered.claimed_by is None
    assert recovered.claim_expires_at is None


def test_claimed_with_broker_request_cannot_return_to_ready():
    command = make_command(
        state=CommandState.CLAIMED,
        claim_expires_at=at(minutes=10),
        broker_request_started_at=at(minutes=1),
    )
    with pytest.raises(InvalidTransition):
        transition(command, CommandState.READY, now=at(minutes=11))
    unknown = transition(command, CommandState.UNKNOWN, now=at(minutes=11))
    assert unknown.state is CommandState.UNKNOWN


def test_terminal_states_have_no_exits():
    for terminal in (
        CommandState.FILLED,
        CommandState.REJECTED,
        CommandState.CANCELLED,
        CommandState.EXPIRED,
    ):
        for target in CommandState:
            assert not can_transition(terminal, target)
