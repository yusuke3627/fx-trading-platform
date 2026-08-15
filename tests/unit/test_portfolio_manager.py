from decimal import Decimal
from uuid import uuid4

from tests.support import T0, FixedClock
from trading.domain.position import PositionAction, PositionDirection, VirtualPosition
from trading.domain.signal import StrategySignal
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger


def make_signal(
    direction: PositionDirection = PositionDirection.SHORT,
    stop_pips: str = "10",
) -> StrategySignal:
    return StrategySignal(
        signal_id=uuid4(),
        strategy_id="test_strategy",
        strategy_version="0.0.1",
        symbol="USDJPY",
        desired_direction=direction,
        conviction=0.5,
        expected_horizon_seconds=3600,
        stop_distance_pips=Decimal(stop_pips),
        reason_codes=["TEST"],
        generated_at=T0,
    )


def sizing() -> SizingInput:
    return SizingInput(
        equity=Decimal(1_000_000),
        max_risk_per_trade_pct=Decimal("0.05"),
        pip_size=Decimal("0.01"),
        volume_step=Decimal(1000),
        entry_price=Decimal("158.840"),
    )


def manager_with(*positions: VirtualPosition) -> PortfolioManager:
    ledger = VirtualPositionLedger(FixedClock())
    for p in positions:
        ledger.record(p)
    return PortfolioManager(ledger, FixedClock())


def held(direction: PositionDirection) -> VirtualPosition:
    return VirtualPosition(
        strategy_id="test_strategy",
        symbol="USDJPY",
        direction=direction,
        quantity=Decimal(1000),
        as_of=T0,
    )


def test_open_intent_sized_by_risk_budget():
    # 1,000,000 x 0.05% = 500 budget; 10 pips x 0.01 = 0.1 loss/unit -> 5,000.
    intents = manager_with().intents_from_signal(make_signal(), sizing())
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action is PositionAction.OPEN
    assert intent.target_quantity == Decimal(5000)
    # SHORT stop sits above the entry price.
    assert intent.protection is not None
    assert intent.protection.stop_loss_price == Decimal("158.940")


def test_long_stop_sits_below_entry():
    intents = manager_with().intents_from_signal(
        make_signal(direction=PositionDirection.LONG), sizing()
    )
    assert intents[0].protection.stop_loss_price == Decimal("158.740")


def test_same_direction_becomes_increase():
    manager = manager_with(held(PositionDirection.SHORT))
    intents = manager.intents_from_signal(make_signal(), sizing())
    assert len(intents) == 1
    assert intents[0].action is PositionAction.INCREASE


def test_direction_flip_closes_then_opens():
    # The reversal must survive: CLOSE what exists, then OPEN the desired
    # side. Collapsing it into a bare exit would lose the deduped signal.
    manager = manager_with(held(PositionDirection.LONG))
    intents = manager.intents_from_signal(make_signal(), sizing())
    assert [i.action for i in intents] == [PositionAction.CLOSE, PositionAction.OPEN]

    close, reopen = intents
    # The close carries the held direction: CLOSE LONG (via SELL).
    assert close.direction is PositionDirection.LONG
    assert close.target_quantity == Decimal(0)
    assert close.protection is None

    assert reopen.direction is PositionDirection.SHORT
    assert reopen.target_quantity == Decimal(5000)
    assert reopen.protection is not None


def test_no_intents_without_stop_distance():
    assert manager_with().intents_from_signal(make_signal(stop_pips="0"), sizing()) == []
