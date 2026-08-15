"""PostgreSQL repositories (psycopg 3).

Command claiming uses FOR UPDATE SKIP LOCKED so multiple workers can process
the queue-like execution_commands table without lock contention.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from trading.domain.account import AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.fill import Fill
from trading.domain.order import CommandState, ExecutionCommand
from trading.storage.repository import StaleCommandStateError


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


def _row_to_command(row: dict[str, Any]) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=row["id"],
        intent_id=row["intent_id"],
        idempotency_key=row["idempotency_key"],
        symbol=row["symbol"],
        side=row["side"],
        action=row["action"],
        direction=row["direction"],
        quantity=row["quantity"],
        stop_loss_price=row["stop_loss_price"],
        take_profit_price=row["take_profit_price"],
        broker_position_ticket=row["broker_position_ticket"],
        state=row["state"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        submitting_at=row["submitting_at"],
        broker_request_started_at=row["broker_request_started_at"],
        created_at=row["created_at"],
    )


def _row_to_snapshot(row: dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        observed_at=row["observed_at"],
        balance=row["balance"],
        equity=row["equity"],
        margin=row["margin"],
        free_margin=row["free_margin"],
        margin_level=row["margin_level"],
        unrealized_pnl=row["unrealized_pnl"],
        realized_pnl_day=row["realized_pnl_day"],
        high_water_mark=row["high_water_mark"],
        drawdown_from_hwm=row["drawdown_from_hwm"],
        broker_connected=row["broker_connected"],
    )


def _row_to_event(row: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=row["id"],
        event_type=row["event_type"],
        source=row["source"],
        source_uri=row["source_uri"],
        payload=row["payload"],
        payload_hash=row["payload_hash"],
        raw_uri=row["raw_uri"],
        effective_at=row["effective_at"],
        published_at=row["published_at"],
        retrieved_at=row["retrieved_at"],
        known_at=row["known_at"],
        processed_at=row["processed_at"],
        superseded_at=row["superseded_at"],
    )


class PostgresCommandRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert(self, command: ExecutionCommand) -> None:
        self._conn.execute(
            """
            INSERT INTO execution_commands (
                id, intent_id, idempotency_key, symbol, side, action, direction,
                quantity, stop_loss_price, take_profit_price,
                broker_position_ticket, state, created_at
            ) VALUES (
                %(id)s, %(intent_id)s, %(idempotency_key)s, %(symbol)s, %(side)s,
                %(action)s, %(direction)s, %(quantity)s, %(sl)s, %(tp)s,
                %(ticket)s, %(state)s, %(created_at)s
            )
            """,
            {
                "id": command.command_id,
                "intent_id": command.intent_id,
                "idempotency_key": command.idempotency_key,
                "symbol": command.symbol,
                "side": command.side,
                "action": command.action,
                "direction": command.direction,
                "quantity": command.quantity,
                "sl": command.stop_loss_price,
                "tp": command.take_profit_price,
                "ticket": command.broker_position_ticket,
                "state": command.state,
                "created_at": command.created_at,
            },
        )
        self._conn.commit()

    def save_state(
        self, command: ExecutionCommand, expected_state: CommandState
    ) -> None:
        """Compare-and-set on the current DB state.

        An unconditional UPDATE would let a slow worker holding a stale
        SUBMITTING object overwrite UNKNOWN (or rewind a terminal state),
        resolving UNKNOWN without reconciliation.
        """
        cursor = self._conn.execute(
            """
            UPDATE execution_commands
            SET state = %(state)s,
                claimed_by = %(claimed_by)s,
                claimed_at = %(claimed_at)s,
                claim_expires_at = %(claim_expires_at)s,
                submitting_at = %(submitting_at)s,
                broker_request_started_at = %(broker_request_started_at)s,
                updated_at = now()
            WHERE id = %(id)s AND state = %(expected_state)s
            """,
            {
                "id": command.command_id,
                "state": command.state,
                "expected_state": expected_state,
                "claimed_by": command.claimed_by,
                "claimed_at": command.claimed_at,
                "claim_expires_at": command.claim_expires_at,
                "submitting_at": command.submitting_at,
                "broker_request_started_at": command.broker_request_started_at,
            },
        )
        self._conn.commit()
        if cursor.rowcount != 1:
            raise StaleCommandStateError(
                f"command {command.command_id} is no longer {expected_state}; "
                "re-read and reconcile instead of writing"
            )

    def get(self, command_id: str) -> ExecutionCommand | None:
        row = self._conn.execute(
            "SELECT * FROM execution_commands WHERE id = %s", (command_id,)
        ).fetchone()
        return _row_to_command(row) if row else None

    def claim_next(
        self, worker: str, lease_seconds: int, now: datetime
    ) -> ExecutionCommand | None:
        # Single-statement claim + explicit commit. A `transaction()` block
        # would degrade to a savepoint if a prior SELECT on this connection
        # opened an implicit transaction, leaving the CLAIMED update
        # uncommitted — a crash after the broker call would then roll the
        # claim back to READY and let another worker re-send the order.
        row = self._conn.execute(
            """
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
            RETURNING *
            """,
            {
                "worker": worker,
                "now": now,
                "expires": now + timedelta(seconds=lease_seconds),
            },
        ).fetchone()
        self._conn.commit()
        return _row_to_command(row) if row else None

    def in_state(self, state: CommandState) -> Sequence[ExecutionCommand]:
        rows = self._conn.execute(
            "SELECT * FROM execution_commands WHERE state = %s ORDER BY created_at",
            (state,),
        ).fetchall()
        return [_row_to_command(r) for r in rows]


class PostgresFillRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert(self, fill: Fill) -> None:
        self._conn.execute(
            """
            INSERT INTO fills (
                id, broker_deal_id, broker_order_id, broker_position_ticket,
                broker_position_identifier, execution_command_id, origin,
                protection_reason, side, quantity, price, broker_time, received_at
            ) VALUES (
                %(id)s, %(deal)s, %(order)s, %(ticket)s, %(identifier)s,
                %(command)s, %(origin)s, %(reason)s, %(side)s, %(quantity)s,
                %(price)s, %(broker_time)s, %(received_at)s
            )
            ON CONFLICT (broker_deal_id) DO NOTHING
            """,
            {
                "id": fill.fill_id,
                "deal": fill.broker_deal_id,
                "order": fill.broker_order_id,
                "ticket": fill.broker_position_ticket,
                "identifier": fill.broker_position_identifier,
                "command": fill.execution_command_id,
                "origin": fill.origin,
                "reason": fill.protection_reason,
                "side": fill.side,
                "quantity": fill.quantity,
                "price": fill.price,
                "broker_time": fill.broker_time,
                "received_at": fill.received_at,
            },
        )
        self._conn.commit()

    def has_deal(self, broker_deal_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM fills WHERE broker_deal_id = %s", (broker_deal_id,)
        ).fetchone()
        return row is not None


class PostgresAccountSnapshotRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert(self, s: AccountSnapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO account_snapshots (
                observed_at, balance, equity, margin, free_margin, margin_level,
                unrealized_pnl, realized_pnl_day, high_water_mark,
                drawdown_from_hwm, broker_connected
            ) VALUES (
                %(observed_at)s, %(balance)s, %(equity)s, %(margin)s,
                %(free_margin)s, %(margin_level)s, %(unrealized_pnl)s,
                %(realized_pnl_day)s, %(hwm)s, %(dd)s, %(connected)s
            )
            """,
            {
                "observed_at": s.observed_at,
                "balance": s.balance,
                "equity": s.equity,
                "margin": s.margin,
                "free_margin": s.free_margin,
                "margin_level": s.margin_level,
                "unrealized_pnl": s.unrealized_pnl,
                "realized_pnl_day": s.realized_pnl_day,
                "hwm": s.high_water_mark,
                "dd": s.drawdown_from_hwm,
                "connected": s.broker_connected,
            },
        )
        self._conn.commit()

    def since(self, t: datetime) -> Sequence[AccountSnapshot]:
        rows = self._conn.execute(
            "SELECT * FROM account_snapshots WHERE observed_at >= %s ORDER BY observed_at",
            (t,),
        ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def latest(self) -> AccountSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM account_snapshots ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        return _row_to_snapshot(row) if row else None


class PostgresEventRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert(self, e: EventEnvelope) -> None:
        self._conn.execute(
            """
            INSERT INTO events (
                id, event_type, source, source_uri, payload, payload_hash,
                raw_uri, effective_at, published_at, retrieved_at, known_at,
                processed_at, superseded_at
            ) VALUES (
                %(id)s, %(event_type)s, %(source)s, %(source_uri)s, %(payload)s,
                %(payload_hash)s, %(raw_uri)s, %(effective_at)s, %(published_at)s,
                %(retrieved_at)s, %(known_at)s, %(processed_at)s, %(superseded_at)s
            )
            """,
            {
                "id": e.event_id,
                "event_type": e.event_type,
                "source": e.source,
                "source_uri": e.source_uri,
                # Jsonb adapts the dict for the JSONB column (a plain str
                # would bind as text and fail the type check); EventEnvelope
                # already guarantees JSON-native payloads, so the round-trip
                # preserves types exactly.
                "payload": Jsonb(e.payload),
                "payload_hash": e.payload_hash,
                "raw_uri": e.raw_uri,
                "effective_at": e.effective_at,
                "published_at": e.published_at,
                "retrieved_at": e.retrieved_at,
                "known_at": e.known_at,
                "processed_at": e.processed_at,
                "superseded_at": e.superseded_at,
            },
        )
        self._conn.commit()

    def known_before(
        self, t: datetime, event_type: str | None = None
    ) -> Sequence[EventEnvelope]:
        if event_type is None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE known_at <= %s ORDER BY known_at", (t,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE known_at <= %s AND event_type = %s ORDER BY known_at",
                (t, event_type),
            ).fetchall()
        return [_row_to_event(r) for r in rows]
