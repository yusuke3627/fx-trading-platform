from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

from tests.support import T0, make_command, make_tick, usdjpy_spec
from trading.backtest.costs import STRESS_SCENARIOS, CostModel
from trading.backtest.simulator import ExecutionSimulator, SimulatedPosition
from trading.domain.fill import FillOrigin, ProtectionReason
from trading.domain.order import ExecutionSide
from trading.domain.position import PositionDirection


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


def test_protection_fill_on_stop_loss_cross():
    sim = ExecutionSimulator(deterministic_costs(), usdjpy_spec(), seed=1)
    position = SimulatedPosition(
        position_id="simpos-1",
        symbol="USDJPY",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        entry_price=Decimal("158.90"),
        stop_loss=Decimal("158.80"),
        take_profit=None,
    )
    no_trigger = sim.check_protection(position, make_tick("158.850", "158.854"))
    assert no_trigger is None

    fill = sim.check_protection(position, make_tick("158.790", "158.794"))
    assert fill is not None
    assert fill.origin is FillOrigin.PROTECTION
    assert fill.protection_reason is ProtectionReason.STOP_LOSS
    assert fill.side is ExecutionSide.SELL
    assert fill.broker_position_ticket == "simpos-1"


def test_same_seed_reproduces_fill_price():
    costs = CostModel(latency_ms=0.0, slippage_sigma_pips=0.8)
    ticks = [make_tick("158.840", "158.844")]
    command = make_command(side=ExecutionSide.BUY, direction=PositionDirection.LONG)
    price_a = ExecutionSimulator(costs, usdjpy_spec(), seed=42).submit(command, ticks)
    price_b = ExecutionSimulator(costs, usdjpy_spec(), seed=42).submit(command, ticks)
    assert price_a.fill is not None and price_b.fill is not None
    assert price_a.fill.price == price_b.fill.price
