"""DB claim と broker 送信の間に置く in-memory の優先度 queue（ADR-030）。

DB の claim は READY 行を created_at 順に 1 行ずつ取るだけで、複数の command を
見比べて順位を付けられない。そこで worker が claim した CLAIMED の command を
ここに載せ、送信順・送信間隔・送信直前の再確認をまとめて担う。

dispatch は 1 回で最大 1 件を処理し、次の順で判定する:

1. signal の失効（expires_at）→ EXPIRED。rate limit の有無に関わらず送らない
2. claim lease の失効 → 送らず捨てる（回収は recovery sweep の責務。同じ行を
   2 経路から送らないため、状態遷移もしない）
3. rate limit → 待ちの command は飛ばし、別の窓に属する command を先に見る
4. ticket 付き exit の fresh select → position が無ければ NOOP（CANCELLED）。
   Protection が先に決済した position へ裸の反対売買を送らない
5. pre-trade risk の再評価 → 不承認または数量縮小なら CANCELLED。REJECTED は
   CLAIMED から到達できず、作成時の risk 拒否と broker 拒否に予約する
6. 送信確定時刻で signal と claim lease を再検査し、処理中に失効した command は
   送らない

rate limit の窓は送信が確定した command だけが消費する。fresh select や
revalidation が例外を投げた command は queue から外れたままになり、lease 失効後に
sweep が READY へ戻す（壊れた entry が先頭で他の command を塞がない）。

失効・lease・rate limit は dispatch 開始時刻で判定する。ticket 付き exit の fresh select は
broker への往復を挟むため、revalidation はその直後に読み直した時刻で行う。送信候補の
確定後にもう一度時刻を読み、失効と lease を再検査したうえで、その時刻を rate limit と
SUBMITTING に記録する。呼び出し元の `save_state` と `order_send` までの遅延は queue から
観測できないため、この窓には含まれない。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from itertools import count
from typing import Protocol

from trading.backtest.clock import Clock
from trading.domain.intent import PositionIntent
from trading.domain.order import CommandState, ExecutionCommand
from trading.domain.position import PositionAction
from trading.domain.risk import RiskDecision
from trading.oms.claim import mark_submitting
from trading.oms.rate_limit import RateLimiter
from trading.oms.service import BrokerPositionReader
from trading.oms.state_machine import transition


class QueuePriority(IntEnum):
    EMERGENCY = 0
    CLOSE_REDUCE = 1
    PROTECTION_REPAIR = 2
    NEW_ENTRY = 3
    TELEMETRY = 4


def priority_for(
    command: ExecutionCommand, *, forced_risk_reduction: bool = False
) -> QueuePriority:
    if forced_risk_reduction:
        return QueuePriority.EMERGENCY
    if command.action in (PositionAction.REDUCE, PositionAction.CLOSE):
        return QueuePriority.CLOSE_REDUCE
    return QueuePriority.NEW_ENTRY


class Revalidator(Protocol):
    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision: ...


@dataclass(frozen=True)
class QueuedCommand:
    command: ExecutionCommand
    intent: PositionIntent
    priority: QueuePriority
    sequence: int
    arbitration_rank: int | None = None
    expires_at: datetime | None = None

    def sort_key(self) -> tuple[int, int, int, int]:
        # 同 priority では Arbitrator の rank 付きを rank 順で先に、rank 無しは
        # enqueue 順。rank と連番を同じ桁で比べないので、enqueue の順序を変えても
        # 並びは変わらない。
        if self.arbitration_rank is not None:
            return (int(self.priority), 0, self.arbitration_rank, self.sequence)
        return (int(self.priority), 1, self.sequence, self.sequence)


class DispatchOutcome(StrEnum):
    SEND = "SEND"
    EXPIRED = "EXPIRED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    REVALIDATION_REJECTED = "REVALIDATION_REJECTED"


@dataclass(frozen=True)
class Dispatch:
    """dispatch の結果。`command` は遷移後のコピー（SEND は SUBMITTING、EXPIRED /
    CANCELLED はその状態。LEASE_EXPIRED だけ無変更）で、呼び出し元が
    `save_state(expected_state=CLAIMED)` で永続化してから broker へ進む。
    `decision` は revalidation を実行した場合の RiskDecision（decision trail 用）。"""

    outcome: DispatchOutcome
    entry: QueuedCommand
    command: ExecutionCommand
    decision: RiskDecision | None = None


class ExecutionQueue:
    def __init__(
        self,
        *,
        clock: Clock,
        rate_limiter: RateLimiter,
        revalidator: Revalidator,
        broker: BrokerPositionReader,
    ) -> None:
        self._clock = clock
        self._limiter = rate_limiter
        self._revalidator = revalidator
        self._broker = broker
        self._entries: list[QueuedCommand] = []
        self._sequence = count()

    def enqueue(
        self,
        command: ExecutionCommand,
        intent: PositionIntent,
        *,
        priority: QueuePriority,
        arbitration_rank: int | None = None,
        expires_at: datetime | None = None,
    ) -> QueuedCommand:
        if command.state is not CommandState.CLAIMED:
            raise ValueError("execution queue accepts only CLAIMED commands")
        if any(entry.command.command_id == command.command_id for entry in self._entries):
            raise ValueError(f"command {command.command_id} is already queued")

        entry = QueuedCommand(
            command=command,
            intent=intent,
            priority=priority,
            sequence=next(self._sequence),
            arbitration_rank=arbitration_rank,
            expires_at=expires_at,
        )
        self._entries.append(entry)
        return entry

    def pending(self) -> tuple[QueuedCommand, ...]:
        return tuple(sorted(self._entries, key=QueuedCommand.sort_key))

    def __len__(self) -> int:
        return len(self._entries)

    def dispatch(self) -> Dispatch | None:
        """優先順で最初に送れる 1 件を処理する。None は空か全件 rate limit 待ち。"""
        # 失効・lease・rate limit は同じ dispatch 開始時刻で判定する。
        now = self._clock.now()
        for entry in sorted(self._entries, key=QueuedCommand.sort_key):
            command = entry.command
            if entry.expires_at is not None and now >= entry.expires_at:
                self._entries.remove(entry)
                expired = transition(command, CommandState.EXPIRED, now=now)
                return Dispatch(DispatchOutcome.EXPIRED, entry, expired)

            if command.claim_expires_at is not None and now >= command.claim_expires_at:
                self._entries.remove(entry)
                return Dispatch(DispatchOutcome.LEASE_EXPIRED, entry, command)

            market_entry = command.action in (
                PositionAction.OPEN,
                PositionAction.INCREASE,
            )
            if not self._limiter.allows(
                command.symbol, market_entry=market_entry, now=now
            ):
                continue

            self._entries.remove(entry)
            if command.broker_position_ticket is not None:
                position = self._broker.position(command.broker_position_ticket)
                if position is None:
                    cancelled = transition(command, CommandState.CANCELLED, now=now)
                    return Dispatch(DispatchOutcome.ALREADY_CLOSED, entry, cancelled)

                quantity = command.quantity
                if command.action is PositionAction.CLOSE:
                    quantity = position.quantity
                elif command.action is PositionAction.REDUCE:
                    quantity = min(command.quantity, position.quantity)
                if quantity != command.quantity:
                    command = command.model_copy(update={"quantity": quantity})
                    entry = replace(entry, command=command)

            revalidated_at = self._clock.now()
            decision = self._revalidator.revalidate(entry, revalidated_at)
            reduced = (
                decision.approved_quantity is not None
                and decision.approved_quantity < command.quantity
            )
            if not decision.approved or reduced:
                cancelled = transition(command, CommandState.CANCELLED, now=now)
                return Dispatch(
                    DispatchOutcome.REVALIDATION_REJECTED,
                    entry,
                    cancelled,
                    decision,
                )

            sent_at = self._clock.now()
            if entry.expires_at is not None and sent_at >= entry.expires_at:
                expired = transition(command, CommandState.EXPIRED, now=sent_at)
                return Dispatch(DispatchOutcome.EXPIRED, entry, expired, decision)
            if (
                command.claim_expires_at is not None
                and sent_at >= command.claim_expires_at
            ):
                return Dispatch(
                    DispatchOutcome.LEASE_EXPIRED,
                    entry,
                    command,
                    decision,
                )
            self._limiter.record(command.symbol, market_entry=market_entry, now=sent_at)
            return Dispatch(
                DispatchOutcome.SEND,
                entry,
                mark_submitting(command, sent_at),
                decision,
            )
        return None
