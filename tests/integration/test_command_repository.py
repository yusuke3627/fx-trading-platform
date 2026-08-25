"""PostgreSQL-backed claim protocol for execution commands.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a throwaway idempotency-key prefix and removes its own rows.

The claim is what keeps two workers off one order, and it is written as a
single UPDATE with FOR UPDATE SKIP LOCKED. Neither the locking nor the
compare-and-set can be observed against an in-memory fake — both are database
behaviour, and this is the layer that would send an order twice if they were
wrong.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.support import make_command
from trading.domain.order import CommandState
from trading.storage.repository import StaleCommandStateError

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 13, tzinfo=UTC)


@pytest.fixture
def workers():
    """Hands out repositories on separate connections, as separate worker
    processes would have."""
    from trading.storage.postgres import PostgresCommandRepository, connect

    prefix = f"itest-{uuid4().hex[:12]}"
    connections = []

    # claim_next() takes the oldest READY row in the whole table, so these
    # tests cannot be scoped by a key prefix the way the others are — a
    # leftover from an interrupted run would be claimed instead of the row the
    # test wrote. Integration tests run against a database of their own
    # (tests/integration/README.md), and nothing in the application writes this
    # table yet, so it is emptied up front.
    setup = connect(DSN)
    setup.execute("DELETE FROM execution_commands")
    setup.commit()
    setup.close()

    def worker():
        conn = connect(DSN)
        connections.append(conn)
        return PostgresCommandRepository(conn), conn

    yield worker, prefix

    # Release first: a test that left a row locked would otherwise block the
    # delete until this connection went away, which is only after the timeout.
    for conn in connections:
        conn.rollback()
        conn.close()
    cleanup = connect(DSN)
    cleanup.execute(
        "DELETE FROM execution_commands WHERE idempotency_key LIKE %s", (f"{prefix}%",)
    )
    cleanup.commit()
    cleanup.close()


def command(prefix: str, seq: int, state: CommandState = CommandState.READY):
    # intent_id references position_intents and these tests are about the claim
    # protocol rather than the decision trail, so the commands stand alone.
    # created_at orders the queue, so each one gets a distinct instant.
    return make_command(state=state).model_copy(
        update={
            "intent_id": None,
            "idempotency_key": f"{prefix}-{seq}",
            "created_at": T0 + timedelta(seconds=seq),
        }
    )


def test_a_command_round_trips_through_the_database(workers):
    worker, prefix = workers
    repo, _ = worker()
    written = command(prefix, 1, state=CommandState.CREATED)

    repo.insert(written)

    assert repo.get(str(written.command_id)) == written


def test_claiming_takes_the_oldest_ready_command(workers):
    worker, prefix = workers
    repo, _ = worker()
    repo.insert(command(prefix, 2))
    oldest = command(prefix, 1)
    repo.insert(oldest)

    claimed = repo.claim_next("worker-a", 30, T0)

    assert claimed is not None
    assert claimed.command_id == oldest.command_id
    assert claimed.state is CommandState.CLAIMED
    assert claimed.claimed_by == "worker-a"
    assert claimed.claim_expires_at == T0 + timedelta(seconds=30)


def test_a_row_another_worker_holds_is_skipped_not_waited_on(workers):
    # This is what FOR UPDATE SKIP LOCKED buys. Waiting on the locked row
    # instead would block until the other worker committed and then hand back
    # the same command — the same order, sent twice.
    worker, prefix = workers
    holder, holder_conn = worker()
    other, _ = worker()
    first, second = command(prefix, 1), command(prefix, 2)
    holder.insert(first)
    holder.insert(second)
    holder_conn.execute(
        "SELECT id FROM execution_commands WHERE id = %s FOR UPDATE",
        (first.command_id,),
    )

    claimed = other.claim_next("worker-b", 30, T0)

    assert claimed is not None
    assert claimed.command_id == second.command_id


def test_claiming_an_empty_queue_returns_nothing(workers):
    worker, prefix = workers
    repo, _ = worker()
    repo.insert(command(prefix, 1, state=CommandState.CREATED))

    assert repo.claim_next("worker-a", 30, T0) is None


def test_a_state_that_moved_underneath_the_caller_is_refused(workers):
    # A timeout sweep can move a command to UNKNOWN while a slow worker still
    # holds the old object. Writing from that object would take it back to a
    # state reconciliation has already moved on from.
    worker, prefix = workers
    repo, _ = worker()
    written = command(prefix, 1)
    repo.insert(written)
    repo.save_state(written.model_copy(update={"state": CommandState.UNKNOWN}), CommandState.READY)

    with pytest.raises(StaleCommandStateError):
        repo.save_state(
            written.model_copy(update={"state": CommandState.SUBMITTING}),
            CommandState.READY,
        )


def test_commands_can_be_listed_by_state(workers):
    worker, prefix = workers
    repo, _ = worker()
    ready = command(prefix, 1)
    repo.insert(ready)
    repo.insert(command(prefix, 2, state=CommandState.CREATED))

    listed = repo.in_state(CommandState.READY)

    assert [c.command_id for c in listed] == [ready.command_id]
