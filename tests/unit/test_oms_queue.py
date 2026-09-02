from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import (
    T0,
    FixedClock,
    at,
    make_command,
    make_intent,
    make_snapshot,
    make_tick,
    usdjpy_spec,
)
from trading.data.market import InMemoryMarketData
from trading.domain.order import CommandState, ExecutionSide
from trading.domain.position import BrokerPosition, PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel, RiskDecision
from trading.oms.queue import (
    DispatchOutcome,
    ExecutionQueue,
    QueuedCommand,
    QueuePriority,
    priority_for,
)
from trading.oms.rate_limit import RateLimitConfig, RateLimiter
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine


class FakeBroker:
    def __init__(
        self,
        closed: set[str] | None = None,
        quantity: Decimal = Decimal(1000),
    ) -> None:
        self._closed = closed or set()
        self._quantity = quantity

    def position(self, ticket: str) -> BrokerPosition | None:
        if ticket in self._closed:
            return None
        return BrokerPosition(
            broker_position_ticket=ticket,
            broker_position_identifier=ticket,
            symbol="USDJPY",
            direction=PositionDirection.SHORT,
            quantity=self._quantity,
            entry_price=Decimal("158.840"),
            stop_loss=Decimal("159.500"),
            observed_at=T0,
        )

    def net_exposure(self, symbol: str) -> Decimal:
        return Decimal(0)


class AdvancingFakeBroker(FakeBroker):
    def __init__(self, clock: FixedClock) -> None:
        super().__init__()
        self._clock = clock

    def position(self, ticket: str) -> BrokerPosition | None:
        self._clock.advance(milliseconds=500)
        return super().position(ticket)


class ApproveAll:
    def __init__(self, approved_quantity: Decimal | None = None) -> None:
        self.approved_quantity = approved_quantity
        self.calls: list[tuple[QueuedCommand, datetime]] = []

    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision:
        self.calls.append((entry, now))
        return RiskDecision(
            decision_id=uuid4(),
            intent_id=entry.intent.intent_id,
            approved=True,
            approved_quantity=self.approved_quantity,
            decided_at=now,
        )


class AdvancingApproveAll(ApproveAll):
    def __init__(self, clock: FixedClock) -> None:
        super().__init__()
        self._clock = clock

    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision:
        self._clock.advance(milliseconds=400)
        return super().revalidate(entry, now)


class RejectAll:
    def __init__(self) -> None:
        self.calls: list[tuple[QueuedCommand, datetime]] = []

    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision:
        self.calls.append((entry, now))
        return RiskDecision(
            decision_id=uuid4(),
            intent_id=entry.intent.intent_id,
            approved=False,
            reject_codes=["SPREAD_ACCEPTABLE"],
            decided_at=now,
        )


class RiskRevalidator:
    def __init__(self, clock: FixedClock) -> None:
        self.quote = make_tick("158.840", "158.844", time=clock.now())
        self._engine = RiskEngine(
            RiskConfig(
                trading_enabled=True,
                max_units_per_symbol={"USDJPY": 10000},
                absolute_max_spread_pips={"USDJPY": Decimal("2.0")},
            ),
            clock,
            MarketQuoteConversionService(InMemoryMarketData(), [usdjpy_spec()]),
        )

    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision:
        context = PreTradeContext(
            now=now,
            execution_enabled=True,
            broker_connected=True,
            account_reconciled=True,
            quote=self.quote,
            instrument=usdjpy_spec(),
            account=make_snapshot("1000000"),
            snapshots=[make_snapshot("1000000", observed_at=at(hours=-25))],
            symbol_open_positions_count=0,
            portfolio_open_positions_count=0,
            instrument_trading_enabled=True,
            symbol_exposure_units=Decimal(0),
            event_mode=EventRiskMode.NORMAL,
            kill_switch=KillSwitchLevel.NONE,
            unknown_commands=0,
            stop_distance_pips=Decimal(10),
            requested_quantity=entry.command.quantity,
        )
        return self._engine.evaluate(entry.intent, context)


def make_queue(
    *,
    clock: FixedClock | None = None,
    limiter: RateLimiter | None = None,
    revalidator: ApproveAll | RejectAll | RiskRevalidator | None = None,
    broker: FakeBroker | None = None,
) -> ExecutionQueue:
    return ExecutionQueue(
        clock=clock or FixedClock(),
        rate_limiter=limiter or RateLimiter(RateLimitConfig()),
        revalidator=revalidator or ApproveAll(),
        broker=broker or FakeBroker(),
    )


def enqueue(
    queue: ExecutionQueue,
    *,
    action: PositionAction = PositionAction.OPEN,
    symbol: str = "USDJPY",
    ticket: str | None = None,
    priority: QueuePriority | None = None,
    rank: int | None = None,
    quantity: str = "1000",
    expires_at: datetime | None = None,
    claim_expires_at: datetime | None = None,
) -> QueuedCommand:
    direction = PositionDirection.SHORT
    side = (
        ExecutionSide.BUY
        if action in (PositionAction.REDUCE, PositionAction.CLOSE)
        else ExecutionSide.SELL
    )
    command = make_command(
        state=CommandState.CLAIMED,
        side=side,
        action=action,
        direction=direction,
        symbol=symbol,
        quantity=quantity,
        broker_position_ticket=ticket,
        claim_expires_at=claim_expires_at,
    )
    intent = make_intent(action=action, direction=direction, symbol=symbol)
    return queue.enqueue(
        command,
        intent,
        priority=priority if priority is not None else priority_for(command),
        arbitration_rank=rank,
        expires_at=expires_at,
    )


def test_close_and_protection_repair_are_prioritized_over_new_entries():
    queue = make_queue()
    enqueue(queue, symbol="EURUSD")
    enqueue(queue, symbol="GBPUSD")
    close = enqueue(queue, action=PositionAction.CLOSE, ticket="1001")
    repair = enqueue(queue, priority=QueuePriority.PROTECTION_REPAIR)

    assert queue.pending()[:2] == (close, repair)
    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.entry is close


def test_emergency_is_prioritized_over_close():
    queue = make_queue()
    close = enqueue(queue, action=PositionAction.CLOSE, ticket="1001")
    emergency_command = make_command(
        state=CommandState.CLAIMED,
        side=ExecutionSide.BUY,
        action=PositionAction.CLOSE,
        broker_position_ticket="1002",
    )
    emergency = queue.enqueue(
        emergency_command,
        make_intent(action=PositionAction.CLOSE),
        priority=priority_for(emergency_command, forced_risk_reduction=True),
    )

    assert queue.pending()[:2] == (emergency, close)
    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.entry is emergency


@pytest.mark.parametrize(
    ("action", "forced_risk_reduction", "expected"),
    [
        (PositionAction.OPEN, False, QueuePriority.NEW_ENTRY),
        (PositionAction.INCREASE, False, QueuePriority.NEW_ENTRY),
        (PositionAction.REDUCE, False, QueuePriority.CLOSE_REDUCE),
        (PositionAction.CLOSE, False, QueuePriority.CLOSE_REDUCE),
        (PositionAction.REDUCE, True, QueuePriority.EMERGENCY),
    ],
)
def test_priority_is_derived_from_action_and_forced_reduction(
    action, forced_risk_reduction, expected
):
    command = make_command(state=CommandState.CLAIMED, action=action)

    assert (
        priority_for(command, forced_risk_reduction=forced_risk_reduction)
        is expected
    )


def test_ranked_entries_dispatch_deterministically_regardless_of_enqueue_order():
    ranked = [("USDJPY", 3), ("EURUSD", 1), ("GBPUSD", 4), ("GBPJPY", 2)]
    queues: list[tuple[ExecutionQueue, FixedClock]] = []
    for order in (ranked, list(reversed(ranked))):
        clock = FixedClock()
        queue = make_queue(clock=clock)
        for symbol, rank in order:
            enqueue(queue, symbol=symbol, rank=rank)
        queues.append((queue, clock))

    dispatched_orders: list[list[str]] = []
    for queue, clock in queues:
        symbols = []
        for _ in range(4):
            dispatched = queue.dispatch()
            assert dispatched is not None
            symbols.append(dispatched.command.symbol)
            clock.advance(seconds=1)
        dispatched_orders.append(symbols)

    assert dispatched_orders == [
        ["EURUSD", "GBPJPY", "USDJPY", "GBPUSD"],
        ["EURUSD", "GBPJPY", "USDJPY", "GBPUSD"],
    ]


def test_equal_rank_and_unranked_entries_use_enqueue_order_as_tie_breaker():
    queue = make_queue()
    unranked_first = enqueue(queue, symbol="USDJPY")
    ranked = enqueue(queue, symbol="EURUSD", rank=4)
    ranked_same = enqueue(queue, symbol="GBPJPY", rank=4)
    unranked_second = enqueue(queue, symbol="GBPUSD")

    assert queue.pending() == (
        ranked,
        ranked_same,
        unranked_first,
        unranked_second,
    )


def test_four_market_entries_wait_one_second_and_keep_arbitration_rank():
    clock = FixedClock()
    queue = make_queue(clock=clock)
    for symbol, rank in (
        ("GBPUSD", 3),
        ("USDJPY", 1),
        ("GBPJPY", 4),
        ("EURUSD", 2),
    ):
        enqueue(queue, symbol=symbol, rank=rank)

    symbols = []
    for index in range(4):
        dispatched = queue.dispatch()
        assert dispatched is not None
        assert dispatched.outcome is DispatchOutcome.SEND
        symbols.append(dispatched.command.symbol)
        if index < 3:
            assert queue.dispatch() is None
            clock.advance(seconds=1)

    assert symbols == ["USDJPY", "EURUSD", "GBPUSD", "GBPJPY"]


def test_per_symbol_limit_skips_blocked_exit_for_another_symbol():
    clock = FixedClock()
    queue = make_queue(clock=clock)
    for ticket in range(6):
        enqueue(queue, action=PositionAction.CLOSE, ticket=str(ticket))
    enqueue(
        queue,
        action=PositionAction.CLOSE,
        symbol="EURUSD",
        ticket="eur-1",
    )

    dispatched_symbols = []
    for _ in range(6):
        dispatched = queue.dispatch()
        assert dispatched is not None
        dispatched_symbols.append(dispatched.command.symbol)
    assert dispatched_symbols == ["USDJPY"] * 5 + ["EURUSD"]
    assert queue.dispatch() is None

    clock.advance(seconds=1)
    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.command.symbol == "USDJPY"


def test_expired_entry_is_removed_without_consuming_limit_or_revalidation():
    clock = FixedClock()
    revalidator = ApproveAll()
    queue = make_queue(clock=clock, revalidator=revalidator)
    expired = enqueue(queue, expires_at=at(seconds=2), rank=1)
    enqueue(queue, symbol="EURUSD", rank=2)
    clock.advance(seconds=3)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.EXPIRED
    assert dispatched.entry is expired
    assert dispatched.command.state is CommandState.EXPIRED
    assert len(queue) == 1
    assert revalidator.calls == []

    following = queue.dispatch()
    assert following is not None
    assert following.outcome is DispatchOutcome.SEND


def test_revalidation_rejection_cancels_without_consuming_limit():
    limiter = RateLimiter(RateLimitConfig())
    revalidator = RejectAll()
    queue = make_queue(limiter=limiter, revalidator=revalidator)
    enqueue(queue)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.REVALIDATION_REJECTED
    assert dispatched.command.state is CommandState.CANCELLED
    assert dispatched.decision is not None
    assert dispatched.decision.reject_codes == ["SPREAD_ACCEPTABLE"]
    assert len(queue) == 0
    assert limiter.allows("EURUSD", market_entry=True, now=T0)


def test_approved_entry_is_marked_submitting_at_dispatch_time():
    clock = FixedClock()
    revalidator = ApproveAll()
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue)
    clock.advance(seconds=3)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.SEND
    assert dispatched.command.state is CommandState.SUBMITTING
    assert dispatched.command.submitting_at == at(seconds=3)
    assert revalidator.calls[0][1] == at(seconds=3)


def test_market_entry_window_starts_after_revalidation_finishes():
    clock = FixedClock()
    revalidator = AdvancingApproveAll(clock)
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue, symbol="USDJPY", rank=1)
    enqueue(queue, symbol="EURUSD", rank=2)

    first = queue.dispatch()
    assert first is not None
    assert first.command.submitting_at == at(milliseconds=400)

    clock.advance(milliseconds=600)
    assert clock.now() == at(seconds=1)
    assert queue.dispatch() is None

    clock.advance(milliseconds=400)
    second = queue.dispatch()
    assert second is not None
    assert second.outcome is DispatchOutcome.SEND


def test_entry_expiring_during_revalidation_is_not_sent_or_rate_limited():
    clock = FixedClock()
    revalidator = AdvancingApproveAll(clock)
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue, rank=1, expires_at=at(milliseconds=200))
    enqueue(queue, rank=2)

    expired = queue.dispatch()
    assert expired is not None
    assert expired.outcome is DispatchOutcome.EXPIRED
    assert expired.command.state is CommandState.EXPIRED
    assert expired.decision is not None

    following = queue.dispatch()
    assert following is not None
    assert following.outcome is DispatchOutcome.SEND


def test_claim_lease_expiring_during_revalidation_is_not_sent_or_rate_limited():
    clock = FixedClock()
    revalidator = AdvancingApproveAll(clock)
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue, rank=1, claim_expires_at=at(milliseconds=200))
    enqueue(queue, rank=2)

    lease_expired = queue.dispatch()
    assert lease_expired is not None
    assert lease_expired.outcome is DispatchOutcome.LEASE_EXPIRED
    assert lease_expired.command.state is CommandState.CLAIMED
    assert lease_expired.decision is not None

    following = queue.dispatch()
    assert following is not None
    assert following.outcome is DispatchOutcome.SEND


def test_current_wide_spread_is_rejected_at_dispatch_time():
    clock = FixedClock()
    revalidator = RiskRevalidator(clock)
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue)
    revalidator.quote = make_tick("158.840", "158.900", time=clock.now())

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.REVALIDATION_REJECTED
    assert dispatched.decision is not None
    assert "SPREAD_ACCEPTABLE" in dispatched.decision.reject_codes


def test_current_acceptable_spread_is_sent_after_risk_revalidation():
    clock = FixedClock()
    revalidator = RiskRevalidator(clock)
    queue = make_queue(clock=clock, revalidator=revalidator)
    enqueue(queue)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.SEND
    assert dispatched.decision is not None
    assert dispatched.decision.approved


def test_reduced_revalidation_quantity_cancels_original_command():
    revalidator = ApproveAll(approved_quantity=Decimal(500))
    queue = make_queue(revalidator=revalidator)
    enqueue(queue)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.REVALIDATION_REJECTED
    assert dispatched.command.state is CommandState.CANCELLED
    assert dispatched.decision is not None
    assert dispatched.decision.approved


@pytest.mark.parametrize("position_exists", [False, True])
def test_ticket_exit_is_freshly_selected_before_send(position_exists):
    ticket = "1001"
    broker = FakeBroker(closed=set() if position_exists else {ticket})
    revalidator = ApproveAll()
    queue = make_queue(broker=broker, revalidator=revalidator)
    enqueue(queue, action=PositionAction.CLOSE, ticket=ticket)

    dispatched = queue.dispatch()
    assert dispatched is not None
    if position_exists:
        assert dispatched.outcome is DispatchOutcome.SEND
        assert dispatched.command.state is CommandState.SUBMITTING
        assert len(revalidator.calls) == 1
    else:
        assert dispatched.outcome is DispatchOutcome.ALREADY_CLOSED
        assert dispatched.command.state is CommandState.CANCELLED
        assert revalidator.calls == []


def test_ticket_exit_is_revalidated_after_fresh_position_lookup():
    clock = FixedClock()
    revalidator = ApproveAll()
    queue = make_queue(
        clock=clock,
        broker=AdvancingFakeBroker(clock),
        revalidator=revalidator,
    )
    enqueue(queue, action=PositionAction.CLOSE, ticket="1001")

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.SEND
    assert revalidator.calls[0][1] == at(milliseconds=500)
    assert dispatched.command.submitting_at == at(milliseconds=500)


def test_close_uses_fresh_position_quantity_after_queue_wait():
    revalidator = ApproveAll()
    queue = make_queue(
        broker=FakeBroker(quantity=Decimal(400)),
        revalidator=revalidator,
    )
    queued = enqueue(
        queue,
        action=PositionAction.CLOSE,
        ticket="1001",
        quantity="1000",
    )

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.command.quantity == Decimal(400)
    assert dispatched.entry.command.quantity == Decimal(400)
    assert dispatched.entry is not queued
    assert revalidator.calls[0][0] is dispatched.entry


def test_reduce_is_capped_at_fresh_position_quantity():
    revalidator = ApproveAll()
    queue = make_queue(
        broker=FakeBroker(quantity=Decimal(400)),
        revalidator=revalidator,
    )
    enqueue(
        queue,
        action=PositionAction.REDUCE,
        ticket="1001",
        quantity="1000",
    )

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.command.quantity == Decimal(400)
    assert dispatched.entry.command.quantity == Decimal(400)
    assert revalidator.calls[0][0] is dispatched.entry


def test_reduce_keeps_command_quantity_when_position_is_larger():
    revalidator = ApproveAll()
    queue = make_queue(
        broker=FakeBroker(quantity=Decimal(1000)),
        revalidator=revalidator,
    )
    queued = enqueue(
        queue,
        action=PositionAction.REDUCE,
        ticket="1001",
        quantity="500",
    )

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.command.quantity == Decimal(500)
    assert dispatched.entry is queued
    assert revalidator.calls[0][0] is queued


def test_expired_claim_lease_is_removed_without_state_transition():
    clock = FixedClock()
    revalidator = ApproveAll()
    queue = make_queue(clock=clock, revalidator=revalidator)
    entry = enqueue(queue, claim_expires_at=at(seconds=30))
    clock.advance(seconds=31)

    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.LEASE_EXPIRED
    assert dispatched.command is entry.command
    assert dispatched.command.state is CommandState.CLAIMED
    assert len(queue) == 0
    assert revalidator.calls == []


def test_enqueue_rejects_non_claimed_and_duplicate_commands():
    queue = make_queue()
    ready = make_command(state=CommandState.READY)
    with pytest.raises(ValueError, match="only CLAIMED"):
        queue.enqueue(
            ready,
            make_intent(),
            priority=QueuePriority.NEW_ENTRY,
        )

    entry = enqueue(queue)
    with pytest.raises(ValueError, match="already queued"):
        queue.enqueue(
            entry.command,
            entry.intent,
            priority=QueuePriority.NEW_ENTRY,
        )


def test_emergency_still_obeys_per_symbol_rate_limit():
    clock = FixedClock()
    queue = make_queue(clock=clock)
    for ticket in range(6):
        enqueue(
            queue,
            action=PositionAction.CLOSE,
            ticket=str(ticket),
            priority=QueuePriority.EMERGENCY,
        )

    for _ in range(5):
        dispatched = queue.dispatch()
        assert dispatched is not None
        assert dispatched.outcome is DispatchOutcome.SEND
    assert queue.dispatch() is None

    clock.advance(seconds=1)
    dispatched = queue.dispatch()
    assert dispatched is not None
    assert dispatched.outcome is DispatchOutcome.SEND
