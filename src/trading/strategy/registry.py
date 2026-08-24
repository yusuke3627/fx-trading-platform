"""Strategy lookup by id.

Configuration names a strategy by id; a runner needs the class behind that
name. The mapping is written out rather than discovered by scanning modules —
a strategy joining a live run because something happened to import it is not
a thing that should be possible.
"""
from __future__ import annotations

from trading.strategy.base import Strategy
from trading.strategy.intraday.post_event_failed_breakout import (
    PostEventFailedBreakoutStrategy,
)
from trading.strategy.scalp.failed_spike_reversal import FailedSpikeReversalStrategy
from trading.strategy.swing.monetary_policy_convergence import (
    MonetaryPolicyConvergenceStrategy,
)

STRATEGIES: dict[str, type[Strategy]] = {
    cls.strategy_id: cls
    for cls in (
        FailedSpikeReversalStrategy,
        PostEventFailedBreakoutStrategy,
        MonetaryPolicyConvergenceStrategy,
    )
}
