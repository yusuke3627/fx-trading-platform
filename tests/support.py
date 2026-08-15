"""Shared test factories. All names/values are fictional test data."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.domain.account import AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent, ProtectionSpec
from trading.domain.market import Bar, Tick
from trading.domain.order import CommandState, ExecutionCommand, ExecutionSide
from trading.domain.position import PositionAction, PositionDirection

T0 = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)


def at(**kwargs) -> datetime:
    return T0 + timedelta(**kwargs)


class FixedClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def usdjpy_spec(**overrides) -> InstrumentSpec:
    values = {
        "symbol": "USDJPY",
        "digits": 3,
        "pip_size": Decimal("0.01"),
        "contract_size": Decimal(1000),
        "volume_min": Decimal(1000),
        "volume_step": Decimal(1000),
        "volume_max": Decimal(100000),
        "stop_level_points": 0,
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def make_tick(
    bid: str,
    ask: str,
    time: datetime = T0,
    symbol: str = "USDJPY",
    received_at: datetime | None = None,
) -> Tick:
    return Tick(
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        time=time,
        received_at=received_at,
    )


def make_bar(
    open_: str,
    high: str,
    low: str,
    close: str,
    start: datetime = T0,
    symbol: str = "USDJPY",
    timeframe: str = "1m",
    tick_volume: int = 0,
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=tick_volume,
    )


def make_snapshot(
    equity: str,
    observed_at: datetime = T0,
    high_water_mark: str | None = None,
    broker_connected: bool = True,
    margin: str = "0",
    margin_level: str | None = None,
) -> AccountSnapshot:
    eq = Decimal(equity)
    hwm = Decimal(high_water_mark) if high_water_mark is not None else eq
    return AccountSnapshot(
        observed_at=observed_at,
        balance=eq,
        equity=eq,
        margin=Decimal(margin),
        free_margin=eq,
        margin_level=Decimal(margin_level) if margin_level is not None else None,
        unrealized_pnl=Decimal(0),
        realized_pnl_day=Decimal(0),
        high_water_mark=hwm,
        drawdown_from_hwm=max(hwm - eq, Decimal(0)),
        broker_connected=broker_connected,
    )


def make_intent(
    action: PositionAction = PositionAction.OPEN,
    direction: PositionDirection = PositionDirection.SHORT,
    symbol: str = "USDJPY",
    protected: bool = True,
    target_quantity: str | None = "1000",
) -> PositionIntent:
    protection = None
    if protected:
        protection = ProtectionSpec(
            stop_loss_price=Decimal("159.50"),
            take_profit_price=None,
            maximum_unprotected_seconds=30,
            source="STRATEGY",
        )
    return PositionIntent(
        intent_id=uuid4(),
        strategy_id="test_strategy",
        strategy_version="0.0.1",
        symbol=symbol,
        action=action,
        direction=direction,
        target_quantity=Decimal(target_quantity) if target_quantity else None,
        delta_quantity=None,
        protection=protection,
        reason_codes=["TEST"],
        generated_at=T0,
    )


def make_command(
    state: CommandState = CommandState.CREATED,
    side: ExecutionSide = ExecutionSide.SELL,
    action: PositionAction = PositionAction.OPEN,
    direction: PositionDirection = PositionDirection.SHORT,
    quantity: str = "1000",
    claim_expires_at: datetime | None = None,
    broker_request_started_at: datetime | None = None,
    broker_position_ticket: str | None = None,
    stop_loss: str | None = None,
) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=uuid4(),
        intent_id=uuid4(),
        idempotency_key=f"test-{uuid4().hex[:8]}",
        symbol="USDJPY",
        side=side,
        action=action,
        direction=direction,
        quantity=Decimal(quantity),
        stop_loss_price=Decimal(stop_loss) if stop_loss else None,
        state=state,
        claim_expires_at=claim_expires_at,
        broker_request_started_at=broker_request_started_at,
        broker_position_ticket=broker_position_ticket,
        created_at=T0,
    )


def make_event(
    known_at: datetime = T0,
    event_type: str = "market.tick",
    source: str = "test",
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source=source,
        retrieved_at=known_at,
        known_at=known_at,
    )
