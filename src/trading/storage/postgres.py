"""PostgreSQL repositories (psycopg 3).

Command claiming uses FOR UPDATE SKIP LOCKED so multiple workers can process
the queue-like execution_commands table without lock contention.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

from trading.domain.account import AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.fill import Fill
from trading.domain.order import CommandState, ExecutionCommand


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


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

    def save_state(self, command: ExecutionCommand) -> None:
        self._conn.execute(
            """
            UPDATE execution_commands
            SET state = %(state)s,
                claimed_by = %(claimed_by)s,
                claimed_at = %(claimed_at)s,
                claim_expires_at = %(claim_expires_at)s,
                submitting_at = %(submitting_at)s,
                broker_request_started_at = %(broker_request_started_at)s,
                updated_at = now()
            WHERE id = %(id)s
            """,
            {
                "id": command.command_id,
                "state": command.state,
                "claimed_by": command.claimed_by,
                "claimed_at": command.claimed_at,
                "claim_expires_at": command.claim_expires_at,
                "submitting_at": command.submitting_at,
                "broker_request_started_at": command.broker_request_started_at,
            },
        )
        self._conn.commit()

    def claim_next(self, worker: str, lease_seconds: int, now: datetime) -> dict[str, Any] | None:
        with self._conn.transaction():
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
        return row

    def in_state(self, state: CommandState) -> Sequence[dict[str, Any]]:
        return self._conn.execute(
            "SELECT * FROM execution_commands WHERE state = %s ORDER BY created_at",
            (state,),
        ).fetchall()


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

    def since(self, t: datetime) -> Sequence[dict[str, Any]]:
        return self._conn.execute(
            "SELECT * FROM account_snapshots WHERE observed_at >= %s ORDER BY observed_at",
            (t,),
        ).fetchall()


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
                "payload": json.dumps(e.payload, default=str),
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
    ) -> Sequence[dict[str, Any]]:
        if event_type is None:
            return self._conn.execute(
                "SELECT * FROM events WHERE known_at <= %s ORDER BY known_at", (t,)
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM events WHERE known_at <= %s AND event_type = %s ORDER BY known_at",
            (t, event_type),
        ).fetchall()
