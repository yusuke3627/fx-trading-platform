"""Invariant tests derived from SYSTEM_SPEC: strategies (and the LLM boundary)
must be structurally unable to reach the broker, and position/order direction
vocabularies stay separate."""
from dataclasses import fields
from pathlib import Path

from trading.domain.order import ExecutionSide, execution_side
from trading.domain.position import PositionAction, PositionDirection
from trading.strategy.base import StrategyContext

SRC = Path(__file__).resolve().parents[2] / "src" / "trading"

# Anything that would let a strategy or the LLM layer reach the broker, the
# OMS write path, the database, or the wall clock.
FORBIDDEN_SUBSTRINGS = (
    "MetaTrader5",
    "trading.execution",
    "trading.oms",
    "trading.storage",
    "psycopg",
    "datetime.now(",
)

SCANNED_DIRS = ("strategy", "intelligence")


def scan(root: Path) -> list[str]:
    violations = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in text:
                violations.append(f"{path.relative_to(SRC)}: {needle}")
    return violations


def test_strategy_and_intelligence_never_touch_broker_or_clock():
    violations = []
    for directory in SCANNED_DIRS:
        violations.extend(scan(SRC / directory))
    assert violations == [], violations


def test_strategy_context_exposes_no_execution_surface():
    names = {f.name for f in fields(StrategyContext)}
    assert names == {
        "clock",
        "market",
        "indicators",
        "features",
        "regime",
        "currency_states",
        "currency_regime",
        "portfolio",
        "config",
    }


def test_closing_short_is_buy_not_a_buy_signal():
    assert (
        execution_side(PositionDirection.SHORT, PositionAction.CLOSE)
        is ExecutionSide.BUY
    )
    assert (
        execution_side(PositionDirection.SHORT, PositionAction.REDUCE)
        is ExecutionSide.BUY
    )
    assert (
        execution_side(PositionDirection.SHORT, PositionAction.OPEN)
        is ExecutionSide.SELL
    )
    assert (
        execution_side(PositionDirection.LONG, PositionAction.CLOSE)
        is ExecutionSide.SELL
    )
    assert (
        execution_side(PositionDirection.LONG, PositionAction.INCREASE)
        is ExecutionSide.BUY
    )


def test_one_strategy_file_one_canonical_id():
    from trading.strategy.intraday.post_event_failed_breakout import (
        PostEventFailedBreakoutStrategy,
    )
    from trading.strategy.scalp.failed_spike_reversal import (
        FailedSpikeReversalStrategy,
    )
    from trading.strategy.swing.monetary_policy_convergence import (
        MonetaryPolicyConvergenceStrategy,
    )

    ids = {
        FailedSpikeReversalStrategy.strategy_id,
        PostEventFailedBreakoutStrategy.strategy_id,
        MonetaryPolicyConvergenceStrategy.strategy_id,
    }
    assert ids == {
        "failed_spike_reversal",
        "post_event_failed_breakout",
        "monetary_policy_convergence",
    }


def test_no_timeframe_named_strategy_files():
    strategy_dir = SRC / "strategy"
    timeframe_names = {"1m.py", "5m.py", "15m.py", "30m.py", "1h.py", "4h.py", "1d.py"}
    offenders = [p for p in strategy_dir.rglob("*.py") if p.name in timeframe_names]
    assert offenders == []
