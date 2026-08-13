from decimal import Decimal

import pytest

from tests.support import T0, FixedClock, make_command, make_intent
from trading.backtest.clock import SystemClock
from trading.domain.account import AccountMode
from trading.domain.order import ExecutionSide
from trading.domain.position import (
    BrokerPosition,
    PositionAction,
    PositionDirection,
)
from trading.oms.service import (
    ExitPreparation,
    NakedExitError,
    OMSService,
    execution_delta,
)
from trading.portfolio.manager import PortfolioManager
from trading.portfolio.virtual_ledger import VirtualPositionLedger


class FakeBroker:
    def __init__(
        self,
        positions: dict[str, BrokerPosition] | None = None,
        net: Decimal = Decimal(0),
    ) -> None:
        self._positions = positions or {}
        self._net = net

    def position(self, ticket: str) -> BrokerPosition | None:
        return self._positions.get(ticket)

    def net_exposure(self, symbol: str) -> Decimal:
        return self._net


def short_position(ticket: str = "1001", quantity: str = "20000") -> BrokerPosition:
    return BrokerPosition(
        broker_position_ticket=ticket,
        broker_position_identifier=ticket,
        symbol="USDJPY",
        direction=PositionDirection.SHORT,
        quantity=Decimal(quantity),
        entry_price=Decimal("158.840"),
        stop_loss=Decimal("159.500"),
        take_profit=None,
        observed_at=T0,
    )


def test_desired_net_exposure_aggregates_conflicting_strategies():
    # Swing -100k, Intraday -30k, Scalp +20k -> broker target -110k (allowed
    # conflict; the portfolio layer owns aggregation).
    manager = PortfolioManager(VirtualPositionLedger(FixedClock()), FixedClock())
    manager.set_target("swing", "USDJPY", Decimal(-100000))
    manager.set_target("intraday", "USDJPY", Decimal(-30000))
    manager.set_target("scalp", "USDJPY", Decimal(20000))
    assert manager.desired_net_exposure("USDJPY") == Decimal(-110000)


def test_execution_delta_is_difference_only():
    assert execution_delta(Decimal(-110000), Decimal(-80000)) == Decimal(-30000)


def test_netting_command_orders_the_delta_not_raw_quantity():
    oms = OMSService(
        account_mode=AccountMode.NETTING,
        broker=FakeBroker(net=Decimal(-80000)),
        clock=SystemClock(),
    )
    command = oms.command_for_netting(
        symbol="USDJPY",
        desired_net=Decimal(-110000),
        intent=make_intent(direction=PositionDirection.SHORT),
        volume_step=Decimal(1000),
    )
    assert command is not None
    assert command.side is ExecutionSide.SELL
    assert command.quantity == Decimal(30000)


def test_netting_noop_when_delta_below_step():
    oms = OMSService(
        account_mode=AccountMode.NETTING,
        broker=FakeBroker(net=Decimal(-80000)),
        clock=SystemClock(),
    )
    command = oms.command_for_netting(
        symbol="USDJPY",
        desired_net=Decimal(-80500),
        intent=make_intent(),
        volume_step=Decimal(1000),
    )
    assert command is None


def test_netting_exit_after_protection_close_is_noop_not_reversal():
    # Plan captured current=-1000 -> desired 0. Broker-side SL then closed the
    # position (fresh net = 0). The command must be a NOOP, not a fresh order
    # built from the stale value.
    oms = OMSService(
        account_mode=AccountMode.NETTING,
        broker=FakeBroker(net=Decimal(0)),
        clock=SystemClock(),
    )
    command = oms.command_for_netting(
        symbol="USDJPY",
        desired_net=Decimal(0),
        intent=make_intent(action=PositionAction.CLOSE),
        volume_step=Decimal(1000),
    )
    assert command is None


def test_hedging_exit_references_ticket_and_maps_short_close_to_buy():
    broker = FakeBroker({"1001": short_position()})
    oms = OMSService(
        account_mode=AccountMode.HEDGING, broker=broker, clock=SystemClock()
    )
    command = oms.command_for_hedging_exit(
        intent=make_intent(action=PositionAction.CLOSE, direction=PositionDirection.SHORT),
        ticket="1001",
    )
    assert command is not None
    assert command.broker_position_ticket == "1001"
    # Closing a SHORT is a BUY order, not a BUY signal.
    assert command.side is ExecutionSide.BUY
    assert command.quantity == Decimal(20000)


def test_exit_after_protection_close_is_noop_not_reversal():
    oms = OMSService(
        account_mode=AccountMode.HEDGING, broker=FakeBroker(), clock=SystemClock()
    )
    plan = oms.prepare_exit("1001")
    assert plan.result is ExitPreparation.ALREADY_CLOSED

    command = oms.command_for_hedging_exit(
        intent=make_intent(action=PositionAction.CLOSE, direction=PositionDirection.SHORT),
        ticket="1001",
    )
    assert command is None  # never a naked opposite market order


def test_exit_cannot_exceed_existing_position():
    broker = FakeBroker({"1001": short_position(quantity="5000")})
    oms = OMSService(
        account_mode=AccountMode.HEDGING, broker=broker, clock=SystemClock()
    )
    command = oms.command_for_hedging_exit(
        intent=make_intent(action=PositionAction.REDUCE, direction=PositionDirection.SHORT),
        ticket="1001",
        quantity=Decimal(50000),
    )
    assert command is not None
    assert command.quantity == Decimal(5000)


def test_idempotency_key_distinguishes_follow_up_commands():
    # A re-delta after a partial fill is a legitimate second command from the
    # same intent; only identical (intent, sequence) pairs share a key.
    oms = OMSService(
        account_mode=AccountMode.NETTING,
        broker=FakeBroker(net=Decimal(-80000)),
        clock=SystemClock(),
    )
    intent = make_intent(direction=PositionDirection.SHORT)

    def command(sequence: int):
        return oms.command_for_netting(
            symbol="USDJPY",
            desired_net=Decimal(-110000),
            intent=intent,
            volume_step=Decimal(1000),
            sequence=sequence,
        )

    first, retry, follow_up = command(0), command(0), command(1)
    assert first is not None and retry is not None and follow_up is not None
    assert first.idempotency_key == retry.idempotency_key
    assert first.idempotency_key != follow_up.idempotency_key


def test_naked_exit_command_is_rejected_on_hedging():
    oms = OMSService(
        account_mode=AccountMode.HEDGING, broker=FakeBroker(), clock=SystemClock()
    )
    naked = make_command(action=PositionAction.CLOSE, broker_position_ticket=None)
    with pytest.raises(NakedExitError):
        oms.validate_command(naked)
