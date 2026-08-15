"""Worker-crash and UNKNOWN-command recovery scenarios."""
from decimal import Decimal

import pytest

from tests.support import at, make_command
from trading.domain.order import CommandState
from trading.oms.claim import (
    mark_broker_request_started,
    mark_claimed,
    mark_submitting,
    recovery_state,
)
from trading.oms.reconciliation import resolve_unknown
from trading.oms.state_machine import InvalidTransition, transition


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


def test_inflight_submitting_within_timeout_is_left_alone():
    # A live worker mid-submission must never be seized by a recovery sweep:
    # a false UNKNOWN would trip halt_on_unknown_order and freeze new risk.
    command = mark_claimed(
        make_command(state=CommandState.READY), "worker-1", at(), lease_seconds=60
    )
    command = mark_submitting(command, at(seconds=1))
    assert recovery_state(command, at(seconds=30)) is None


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
        broker_order_live=False,
        filled_quantity=Decimal(1000),
    )
    assert filled is not None and filled.state is CommandState.FILLED

    partial = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        broker_order_live=False,
        filled_quantity=Decimal(400),
    )
    assert partial is not None and partial.state is CommandState.PARTIAL_FILL

    cancelled = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        broker_order_live=False,
        filled_quantity=Decimal(0),
    )
    assert cancelled is not None and cancelled.state is CommandState.CANCELLED

    no_side_effect = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=False,
        broker_order_live=False,
        filled_quantity=Decimal(0),
    )
    assert no_side_effect is not None and no_side_effect.state is CommandState.REJECTED


def test_live_broker_order_keeps_unknown_unresolved():
    # An order still working on the book can fill after reconciliation ran;
    # calling it CANCELLED would re-enable new risk while the fill is pending.
    def unknown_command():
        return make_command(state=CommandState.UNKNOWN, quantity="1000")

    still_open = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        broker_order_live=True,
        filled_quantity=Decimal(0),
    )
    assert still_open is None

    partially_filled_live = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        broker_order_live=True,
        filled_quantity=Decimal(400),
    )
    assert partially_filled_live is None

    # A fully observed fill is terminal evidence regardless of order status.
    fully_filled = resolve_unknown(
        unknown_command(),
        now=at(minutes=1),
        broker_order_found=True,
        broker_order_live=True,
        filled_quantity=Decimal(1000),
    )
    assert fully_filled is not None and fully_filled.state is CommandState.FILLED
