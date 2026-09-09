from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.replay.test_vertical_slice import build_engine
from tests.support import usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.engine import _RunState
from trading.backtest.simulator import ExecutionSimulator
from trading.data.market.dukascopy import known_to_broker_label


@pytest.mark.parametrize("month", [1, 8])
def test_snapshot_daily_pnl_resets_at_jst_midnight_and_preserves_balance(month):
    engine = build_engine(CostModel())
    simulator = ExecutionSimulator(CostModel(), usdjpy_spec(), seed=1)
    state = _RunState(initial_equity=Decimal(10000))
    midnight = datetime(2026, month, 12, 15, tzinfo=UTC)
    before = midnight - timedelta(seconds=1)
    before_label = known_to_broker_label(before, timedelta(hours=7))
    midnight_label = known_to_broker_label(midnight, timedelta(hours=7))
    engine._record_realized(state, Decimal(100), before_label)
    assert engine._snapshot(state, simulator, before).realized_pnl_day == Decimal(100)
    assert engine._snapshot(state, simulator, midnight).realized_pnl_day == 0
    # 翌日に到着した前日の損益訂正は累計残高だけを変える。
    engine._record_realized(state, Decimal(-20), before_label)
    engine._record_realized(state, Decimal(-5), midnight_label)
    snapshot = engine._snapshot(state, simulator, midnight)
    assert snapshot.realized_pnl_day == Decimal(-5)
    assert snapshot.balance == Decimal(10075)


def test_future_broker_label_is_not_counted_before_live_history_window():
    engine = build_engine(CostModel())
    state = _RunState(initial_equity=Decimal(10000))
    now = datetime(2026, 8, 12, 16, tzinfo=UTC)
    later = now + timedelta(hours=1)
    engine._record_realized(
        state, Decimal(25), known_to_broker_label(later, timedelta(hours=7))
    )
    assert engine._realized_pnl_day(state, now) == 0
    assert engine._realized_pnl_day(state, later) == Decimal(25)
