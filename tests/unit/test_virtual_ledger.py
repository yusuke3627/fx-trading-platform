from decimal import Decimal

from tests.support import T0, FixedClock, at
from trading.domain.order import ExecutionSide
from trading.domain.position import PositionDirection, VirtualPosition
from trading.portfolio.virtual_ledger import VirtualPositionLedger


def snapshot(
    quantity: str, as_of=T0, strategy_id="strategy_a", symbol="USDJPY"
) -> VirtualPosition:
    return VirtualPosition(
        strategy_id=strategy_id,
        symbol=symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal(quantity),
        as_of=as_of,
    )


def test_same_instant_snapshots_latest_insertion_wins():
    # Under a ReplayClock multiple snapshots can share one timestamp; the
    # newest insertion must be the current position, not the oldest.
    ledger = VirtualPositionLedger(FixedClock())
    ledger.record(snapshot("1000", as_of=T0))
    ledger.record(snapshot("2000", as_of=T0))
    current = ledger.position("strategy_a", "USDJPY")
    assert current is not None and current.quantity == Decimal(2000)


def test_increase_blends_cost_basis():
    clock = FixedClock()
    ledger = VirtualPositionLedger(clock)
    ledger.apply_fill("strategy_a", "USDJPY", ExecutionSide.BUY, Decimal(1000), Decimal(100))
    clock.advance(seconds=1)
    position = ledger.apply_fill(
        "strategy_a", "USDJPY", ExecutionSide.BUY, Decimal(1000), Decimal(102)
    )
    assert position.quantity == Decimal(2000)
    assert position.average_price == Decimal(101)


def test_reduce_keeps_cost_basis():
    clock = FixedClock()
    ledger = VirtualPositionLedger(clock)
    ledger.apply_fill("strategy_a", "USDJPY", ExecutionSide.BUY, Decimal(1000), Decimal(100))
    clock.advance(seconds=1)
    position = ledger.apply_fill(
        "strategy_a", "USDJPY", ExecutionSide.SELL, Decimal(500), Decimal(105)
    )
    assert position.quantity == Decimal(500)
    assert position.average_price == Decimal(100)


def test_flip_through_zero_restarts_cost_basis():
    clock = FixedClock()
    ledger = VirtualPositionLedger(clock)
    ledger.apply_fill("strategy_a", "USDJPY", ExecutionSide.BUY, Decimal(1000), Decimal(100))
    clock.advance(seconds=1)
    position = ledger.apply_fill(
        "strategy_a", "USDJPY", ExecutionSide.SELL, Decimal(3000), Decimal(103)
    )
    assert position.direction is PositionDirection.SHORT
    assert position.quantity == Decimal(2000)
    assert position.average_price == Decimal(103)


def test_full_close_clears_cost_basis():
    ledger = VirtualPositionLedger(FixedClock())
    ledger.apply_fill("strategy_a", "USDJPY", ExecutionSide.BUY, Decimal(1000), Decimal(100))
    position = ledger.apply_fill(
        "strategy_a", "USDJPY", ExecutionSide.SELL, Decimal(1000), Decimal(101)
    )
    assert position.quantity == Decimal(0)
    assert position.average_price is None


def test_net_exposure_sums_latest_per_strategy():
    ledger = VirtualPositionLedger(FixedClock())
    ledger.record(snapshot("2000", as_of=T0, strategy_id="strategy_a"))
    ledger.record(
        VirtualPosition(
            strategy_id="strategy_b",
            symbol="USDJPY",
            direction=PositionDirection.SHORT,
            quantity=Decimal(500),
            as_of=at(seconds=1),
        )
    )
    assert ledger.net_exposure("USDJPY") == Decimal(1500)


def test_open_positions_spans_symbols_and_drops_flat_books():
    ledger = VirtualPositionLedger(FixedClock())
    ledger.record(snapshot("1000", symbol="USDJPY"))
    ledger.record(snapshot("2000", symbol="EURUSD"))
    # 同じ (strategy, symbol) の新しい snapshot が最新として勝つ。
    ledger.record(snapshot("0", as_of=at(minutes=1), symbol="EURUSD"))
    ledger.record(snapshot("3000", symbol="GBPUSD", strategy_id="strategy_b"))

    held = {(p.strategy_id, p.symbol) for p in ledger.open_positions()}
    assert held == {("strategy_a", "USDJPY"), ("strategy_b", "GBPUSD")}


def test_older_snapshot_recorded_later_does_not_replace_the_current_position():
    ledger = VirtualPositionLedger(FixedClock())
    ledger.record(snapshot("2000", as_of=at(hours=2)))
    ledger.record(snapshot("1000", as_of=at(hours=1)))
    current = ledger.position("strategy_a", "USDJPY")
    assert current is not None and current.quantity == Decimal(2000)
