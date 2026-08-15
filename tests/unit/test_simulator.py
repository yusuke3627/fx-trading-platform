from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

from tests.support import T0, at, make_command, make_tick, usdjpy_spec
from trading.backtest.costs import STRESS_SCENARIOS, CostModel
from trading.backtest.simulator import ExecutionSimulator, SimulatedPosition
from trading.domain.account import AccountMode
from trading.domain.fill import FillOrigin, ProtectionReason
from trading.domain.order import ExecutionSide
from trading.domain.position import PositionAction, PositionDirection


def deterministic_costs() -> CostModel:
    return CostModel(latency_ms=0.0, slippage_sigma_pips=0.0)


def test_cost_model_has_no_fixed_spread():
    names = {f.name for f in fields(CostModel)}
    assert "spread" not in names and "fixed_spread" not in names
    assert "spread_multiplier" in names  # spread only ever scales observed bid/ask


def test_buy_fills_at_observed_ask_without_slippage():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    ticks = [make_tick("158.840", "158.844")]
    result = sim.submit(
        make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG), ticks
    )
    assert not result.rejected
    assert result.fill is not None
    assert result.fill.price == Decimal("158.844")
    assert result.fill.origin is FillOrigin.COMMAND


def test_stress_scenario_widens_effective_spread():
    stressed = ExecutionSimulator(
        STRESS_SCENARIOS["spread_x10"], usdjpy_spec(), seed=1
    )
    normal_price = Decimal("158.844")
    ticks = [make_tick("158.840", "158.844", time=T0 + timedelta(seconds=i)) for i in range(5)]
    result = stressed.submit(
        make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG), ticks
    )
    if result.fill is not None:  # reject burst may trigger under stress
        assert result.fill.price > normal_price


def test_order_never_fills_on_ticks_before_its_creation():
    # Pre-command history in the tick window must not produce a fill in the
    # past (command.created_at is the earliest possible submit time).
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    stale_ticks = [make_tick("158.840", "158.844", time=at(minutes=-5))]
    result = sim.submit(
        make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG),
        stale_ticks,
    )
    assert result.rejected and result.fill is None


def test_fill_time_is_at_or_after_command_creation():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    ticks = [
        make_tick("158.800", "158.804", time=at(minutes=-5)),
        make_tick("158.840", "158.844", time=at(seconds=1)),
    ]
    result = sim.submit(
        make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG), ticks
    )
    assert result.fill is not None
    assert result.fill.broker_time == at(seconds=1)
    assert result.fill.price == Decimal("158.844")


def test_no_fill_when_no_tick_after_latency_window():
    costs = CostModel(latency_ms=10_000, slippage_sigma_pips=0.0)
    sim = ExecutionSimulator(costs, usdjpy_spec(), seed=1)
    result = sim.submit(
        make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG),
        [make_tick("158.840", "158.844")],
    )
    assert result.rejected and result.fill is None


def open_long(
    sim: ExecutionSimulator, quantity: str = "1000", stop_loss: str | None = None
):
    result = sim.submit(
        make_command(
            side=ExecutionSide.BUY,
            direction=PositionDirection.LONG,
            action=PositionAction.OPEN,
            quantity=quantity,
            stop_loss=stop_loss,
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.position is not None
    return result.position


def test_protection_fill_on_stop_loss_cross():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = open_long(sim, stop_loss="158.80")

    no_trigger = sim.check_protection(position, make_tick("158.850", "158.854"))
    assert no_trigger is None

    fill = sim.check_protection(position, make_tick("158.790", "158.794"))
    assert fill is not None
    assert fill.origin is FillOrigin.PROTECTION
    assert fill.protection_reason is ProtectionReason.STOP_LOSS
    assert fill.side is ExecutionSide.SELL
    assert fill.broker_position_ticket == position.position_id


def test_protection_never_fires_twice():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = open_long(sim, stop_loss="158.80")
    trigger_tick = make_tick("158.790", "158.794")
    assert sim.check_protection(position, trigger_tick) is not None
    # The position left the book with the first fire.
    assert sim.check_protection(position, trigger_tick) is None


def test_protection_uses_latest_book_quantity_after_reduce():
    # The caller may still hold the object from OPEN; after a partial REDUCE
    # the protection must close the remaining quantity, not the stale one.
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    opened = open_long(sim, quantity="2000", stop_loss="158.80")
    sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.REDUCE,
            quantity="1000",
            broker_position_ticket=opened.position_id,
        ),
        [make_tick("158.840", "158.844")],
    )

    fill = sim.check_protection(opened, make_tick("158.790", "158.794"))
    assert fill is not None
    assert fill.quantity == Decimal(1000)


def test_protection_ignores_unregistered_position():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    ghost = SimulatedPosition(
        position_id="simpos-ghost",
        symbol="USDJPY",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        entry_price=Decimal("158.90"),
        stop_loss=Decimal("158.80"),
        take_profit=None,
    )
    assert sim.check_protection(ghost, make_tick("158.790", "158.794")) is None


def test_close_removes_position_from_book():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = open_long(sim)
    result = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.CLOSE,
            broker_position_ticket=position.position_id,
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.fill is not None
    assert result.fill.broker_position_ticket == position.position_id
    assert result.position is None
    assert sim.position(position.position_id) is None


def test_netting_exit_without_ticket_applies_to_symbol_book():
    sim = ExecutionSimulator(
        deterministic_costs(), usdjpy_spec(), seed=1, account_mode=AccountMode.NETTING
    )
    open_long(sim, quantity="2000")
    reduced = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.REDUCE,
            quantity="1000",
        ),
        [make_tick("158.840", "158.844")],
    )
    assert reduced.fill is not None and reduced.fill.quantity == Decimal(1000)
    assert reduced.position is not None and reduced.position.quantity == Decimal(1000)

    closed = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.CLOSE,
            quantity="1000",
        ),
        [make_tick("158.840", "158.844")],
    )
    assert closed.fill is not None
    assert closed.position is None


def test_netting_open_labelled_buy_offsets_existing_short():
    # A reducing delta from OMS arrives as BUY with action=OPEN (the intent's
    # label). Netting must offset the short book, not add a coexisting LONG.
    sim = ExecutionSimulator(
        deterministic_costs(), usdjpy_spec(), seed=1, account_mode=AccountMode.NETTING
    )
    short = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.SHORT,
            action=PositionAction.OPEN,
            quantity="80000",
        ),
        [make_tick("158.840", "158.844")],
    ).position
    assert short is not None

    result = sim.submit(
        make_command(
            side=ExecutionSide.BUY,
            direction=PositionDirection.LONG,
            action=PositionAction.OPEN,
            quantity="20000",
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.fill is not None and result.fill.quantity == Decimal(20000)
    remaining = sim.position(short.position_id)
    assert remaining is not None
    assert remaining.direction is PositionDirection.SHORT
    assert remaining.quantity == Decimal(60000)
    # No LONG was created: the returned position is the reduced SHORT.
    assert result.position is not None
    assert result.position.direction is PositionDirection.SHORT


def test_netting_same_direction_fills_merge_into_single_position():
    # A netting account holds one position per symbol: quantity adds up,
    # entry price is volume-weighted, protection follows the latest order.
    sim = ExecutionSimulator(
        deterministic_costs(), usdjpy_spec(), seed=1, account_mode=AccountMode.NETTING
    )
    first = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.SHORT,
            action=PositionAction.OPEN,
            quantity="1000",
            stop_loss="159.50",
        ),
        [make_tick("158.840", "158.844")],
    ).position
    assert first is not None

    second = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.SHORT,
            action=PositionAction.INCREASE,
            quantity="3000",
            stop_loss="159.30",
        ),
        [make_tick("158.640", "158.644")],
    ).position
    assert second is not None
    assert second.position_id == first.position_id
    assert second.quantity == Decimal(4000)
    # (158.840 x 1000 + 158.640 x 3000) / 4000
    assert second.entry_price == Decimal("158.690")
    assert second.stop_loss == Decimal("159.30")
    assert sim.position(first.position_id).quantity == Decimal(4000)


def test_netting_exit_with_empty_book_does_not_execute():
    sim = ExecutionSimulator(
        deterministic_costs(), usdjpy_spec(), seed=1, account_mode=AccountMode.NETTING
    )
    result = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.CLOSE,
            quantity="1000",
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.rejected and result.fill is None


def test_exit_against_missing_position_does_not_execute():
    # A queued CLOSE whose position was already closed (e.g. by protection)
    # must not fill — a real broker rejects the unknown ticket.
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    result = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.CLOSE,
            broker_position_ticket="simpos-ghost",
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.rejected and result.fill is None


def test_exit_after_protection_close_does_not_execute():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = open_long(sim, stop_loss="158.80")
    fired = sim.check_protection(position, make_tick("158.790", "158.794"))
    assert fired is not None

    result = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.CLOSE,
            broker_position_ticket=position.position_id,
        ),
        [make_tick("158.790", "158.794")],
    )
    assert result.rejected and result.fill is None


def test_exit_cannot_fill_more_than_held():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = open_long(sim, quantity="1000")
    result = sim.submit(
        make_command(
            side=ExecutionSide.SELL,
            direction=PositionDirection.LONG,
            action=PositionAction.REDUCE,
            quantity="50000",
            broker_position_ticket=position.position_id,
        ),
        [make_tick("158.840", "158.844")],
    )
    assert result.fill is not None
    assert result.fill.quantity == Decimal(1000)
    assert result.position is None


def test_same_seed_reproduces_fill_price():
    costs = CostModel(latency_ms=0.0, slippage_sigma_pips=0.8)
    ticks = [make_tick("158.840", "158.844")]
    command = make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG)
    price_a = ExecutionSimulator(costs, usdjpy_spec(), seed=42).submit(command, ticks)
    price_b = ExecutionSimulator(costs, usdjpy_spec(), seed=42).submit(command, ticks)
    assert price_a.fill is not None and price_b.fill is not None
    assert price_a.fill.price == price_b.fill.price
