"""PostgreSQL repositories (psycopg 3).

Command claiming uses FOR UPDATE SKIP LOCKED so multiple workers can process
the queue-like execution_commands table without lock contention.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from trading.domain.account import AccountSnapshot
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope, ensure_json_native
from trading.domain.fill import Fill
from trading.domain.market import Bar, Tick
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


def _row_to_tick(row: dict[str, Any]) -> Tick:
    return Tick(
        symbol=row["symbol"],
        bid=row["bid"],
        ask=row["ask"],
        time=row["event_time"],
        received_at=row["received_at"],
    )


def _row_to_bar(row: dict[str, Any]) -> Bar:
    # end_at is not read back: Bar.close_time re-derives it from start and
    # timeframe. known_at is, because it sits on a different clock and no
    # other column can stand in for it (ADR-005).
    return Bar(
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        start=row["start_at"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        tick_volume=row["tick_volume"],
        known_at=row["known_at"],
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


class PostgresMarketTickRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_many(
        self, ticks: Sequence[Tick], *, source: str, ingestion_run: UUID
    ) -> int:
        if not ticks:
            return 0
        cursor = self._conn.cursor()
        cursor.executemany(
            """
            INSERT INTO market_ticks (
                symbol, bid, ask, event_time, received_at, source, ingestion_run
            ) VALUES (
                %(symbol)s, %(bid)s, %(ask)s, %(event_time)s, %(received_at)s,
                %(source)s, %(ingestion_run)s
            )
            ON CONFLICT (symbol, event_time, bid, ask) DO NOTHING
            """,
            [
                {
                    "symbol": t.symbol,
                    "bid": t.bid,
                    "ask": t.ask,
                    "event_time": t.time,
                    # Visibility follows reception, so what is stored is the
                    # tick's known time, not its broker timestamp.
                    "received_at": t.known_time,
                    "source": source,
                    "ingestion_run": ingestion_run,
                }
                for t in ticks
            ],
        )
        self._conn.commit()
        # Conflicting rows count zero, so this is what the batch actually added.
        return cursor.rowcount

    def known_before(
        self, symbol: str, t: datetime, since: datetime
    ) -> Sequence[Tick]:
        # Several quotes may share an event_time (the uniqueness key includes
        # bid/ask), so the identity column breaks the tie: without it the
        # order is planner-dependent, and which quote lands last decides a
        # bar's close and the marking price of a replay.
        rows = self._conn.execute(
            """
            SELECT symbol, bid, ask, event_time, received_at
            FROM market_ticks
            WHERE symbol = %s AND event_time >= %s AND received_at <= %s
            ORDER BY event_time, id
            """,
            (symbol, since, t),
        ).fetchall()
        return [_row_to_tick(r) for r in rows]


class PostgresMarketBarRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_many(self, bars: Sequence[Bar]) -> int:
        if not bars:
            return 0
        cursor = self._conn.cursor()
        cursor.executemany(
            """
            INSERT INTO market_bars (
                symbol, timeframe, start_at, end_at, known_at,
                open, high, low, close, tick_volume
            ) VALUES (
                %(symbol)s, %(timeframe)s, %(start_at)s, %(end_at)s, %(known_at)s,
                %(open)s, %(high)s, %(low)s, %(close)s, %(tick_volume)s
            )
            ON CONFLICT (symbol, timeframe, start_at) DO NOTHING
            """,
            [
                {
                    "symbol": b.symbol,
                    "timeframe": b.timeframe,
                    "start_at": b.start,
                    # end_at closes the candle on the broker's clock; known_at
                    # records when we saw it complete on ours (ADR-005).
                    "end_at": b.close_time,
                    "known_at": b.known_at,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "tick_volume": b.tick_volume,
                }
                for b in bars
            ],
        )
        self._conn.commit()
        # A settled bar is never rewritten: a re-run reports zero, not an
        # overwrite of history.
        return cursor.rowcount

    def known_before(
        self, symbol: str, timeframe: str, t: datetime, count: int
    ) -> Sequence[Bar]:
        rows = self._conn.execute(
            """
            SELECT symbol, timeframe, start_at, open, high, low, close, tick_volume
            FROM market_bars
            WHERE symbol = %s AND timeframe = %s AND known_at <= %s
            ORDER BY start_at DESC
            LIMIT %s
            """,
            (symbol, timeframe, t, count),
        ).fetchall()
        return [_row_to_bar(r) for r in reversed(rows)]


def _row_to_observation(row: dict[str, Any]) -> EconomicObservation:
    return EconomicObservation(
        observation_id=row["id"],
        series=row["series"],
        observation_period=row["observation_period"],
        value=row["value"],
        unit=row["unit"],
        source=row["source"],
        source_uri=row["source_uri"],
        payload_hash=row["payload_hash"],
        published_at=row["published_at"],
        retrieved_at=row["retrieved_at"],
        known_at=row["known_at"],
    )


class PostgresMacroObservationRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_many(self, observations: Sequence[EconomicObservation]) -> int:
        if not observations:
            return 0
        cursor = self._conn.cursor()
        # A row is a vintage only if its value differs from the vintage
        # immediately preceding it (known_at order). Forward collectors stamp
        # known_at = retrieved_at, so without this check every scheduled
        # re-collection would append a full copy of unchanged values as fake
        # revisions. Comparing against the latest vintage BEFORE the
        # candidate's known_at (not the latest overall) keeps an ALFRED
        # backfill insertable after later forward rows already exist.
        cursor.executemany(
            """
            INSERT INTO macro_observations (
                id, series, observation_period, value, unit, source,
                source_uri, payload_hash, published_at, retrieved_at, known_at
            )
            SELECT
                %(id)s, %(series)s, %(observation_period)s, %(value)s, %(unit)s,
                %(source)s, %(source_uri)s, %(payload_hash)s, %(published_at)s,
                %(retrieved_at)s, %(known_at)s
            WHERE (
                SELECT m.value FROM macro_observations m
                WHERE m.series = %(series)s
                  AND m.observation_period = %(observation_period)s
                  AND m.known_at < %(known_at)s
                ORDER BY m.known_at DESC, m.id DESC
                LIMIT 1
            ) IS DISTINCT FROM %(value)s
            ON CONFLICT (series, observation_period, known_at) DO NOTHING
            """,
            [
                {
                    "id": o.observation_id,
                    "series": o.series,
                    "observation_period": o.observation_period,
                    "value": o.value,
                    "unit": o.unit,
                    "source": o.source,
                    "source_uri": o.source_uri,
                    "payload_hash": o.payload_hash,
                    "published_at": o.published_at,
                    "retrieved_at": o.retrieved_at,
                    "known_at": o.known_at,
                }
                for o in observations
            ],
        )
        self._conn.commit()
        # Unchanged values and already-stored vintages count zero, so this is
        # the number of genuinely new vintages the batch added.
        return cursor.rowcount

    def known_before(self, series: str, t: datetime) -> Sequence[EconomicObservation]:
        # id breaks ties when two vintages share a known_at (UUIDs order
        # arbitrarily but stably), keeping replay order planner-independent.
        rows = self._conn.execute(
            """
            SELECT id, series, observation_period, value, unit, source,
                   source_uri, payload_hash, published_at, retrieved_at, known_at
            FROM macro_observations
            WHERE series = %s AND known_at <= %s
            ORDER BY known_at, id
            """,
            (series, t),
        ).fetchall()
        return [_row_to_observation(r) for r in rows]


class PostgresEventRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert(self, e: EventEnvelope) -> None:
        # Construction-time validation is not enough: frozen models do not
        # deep-freeze nested containers, so re-verify right before the JSONB
        # adaptation to keep the round trip type-exact.
        ensure_json_native(e.payload)
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

    def insert_new(self, e: EventEnvelope) -> bool:
        ensure_json_native(e.payload)
        cursor = self._conn.execute(
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
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": e.event_id,
                "event_type": e.event_type,
                "source": e.source,
                "source_uri": e.source_uri,
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
        return cursor.rowcount == 1

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
