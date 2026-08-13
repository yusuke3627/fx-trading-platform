"""Worker-crash and UNKNOWN-command recovery scenarios."""
from decimal import Decimal

import pytest

from trading.domain.order import CommandState
from trading.oms.claim import (
    mark_broker_request_started,
    mark_claimed,
    mark_submitting,
    recovery_state,
)
from trading.oms.reconciliation import resolve_unknown
from trading.oms.state_machine import InvalidTransition, transition

from tests.support import at, make_command


def test_crash_before_broker_request_reclaims_to_ready():
    command = mark_claimed(
        make_command(state=CommandState.READY), "worker-1", at(), lease_seconds=60
    )
    # Worker dies; lease expires; no broker side effect exists.
    assert recovery_state(command, at(minutes=2)) is CommandState.READY


def test_unexpired_claim_is_not_reclaimed():
    command = mark_claimed(
        make_command(state=CommandState.READY), "worker-1", at(), lease_seconds=60
    )
    assert recovery_state(command, at(seconds=30)) is None


def test_crash_after_broker_request_goes_to_unknown():
    command = mark_claimed(
        make_command(state=CommandState.READY), "worker-1", at(), lease_seconds=60
    )
    command = mark_broker_request_started(command, at(seconds=1))
    assert recovery_state(command, at(minutes=2)) is CommandState.UNKNOWN


def test_crash_during_submitting_goes_to_unknown_never_ready():
    command = mark_claimed(
        make_command(state=CommandState.READY), "worker-1", at(), lease_seconds=60
    )
    command = mark_submitting(command, at(seconds=1))
    command = mark_broker_request_started(command, at(seconds=1))

    assert recovery_state(command, at(minutes=5)) is CommandState.UNKNOWN
    with pytest.raises(InvalidTransition):
        transition(command, CommandState.READY, now=at(minutes=5))


def test_unknown_resolution_from_broker_history():
    def unknown_command():
        return make_command(state=CommandState.UNKNOWN, quantity="1000")

    filled = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        filled_quantity=Decimal("1000"),
    )
    assert filled.state is CommandState.FILLED

    partial = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        filled_quantity=Decimal("400"),
    )
    assert partial.state is CommandState.PARTIAL_FILL

    cancelled = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        filled_quantity=Decimal("0"),
    )
    assert cancelled.state is CommandState.CANCELLED

    no_side_effect = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=False,
        filled_quantity=Decimal("0"),
    )
    assert no_side_effect.state is CommandState.REJECTED
