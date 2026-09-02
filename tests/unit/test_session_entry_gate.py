"""session profile が strategy の entry 経路を gate する（ADR-028）。

DST 境界をまたぐ session 判定は tests/unit/test_session.py が担う。ここでは
冬時間の固定 instant だけを使い、policy × StrategyStatus の表と gate の位置を固定する。
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.support import FixedClock, make_event
from trading.strategy.base import Strategy, StrategyConfig, StrategyHorizon, StrategyStatus
from trading.strategy.intraday.post_event_failed_breakout import (
    PostEventFailedBreakoutStrategy,
)
from trading.strategy.scalp.failed_spike_reversal import FailedSpikeReversalStrategy
from trading.strategy.sessions import SessionEntryPolicy, SessionProfile
from trading.strategy.swing.monetary_policy_convergence import (
    MonetaryPolicyConvergenceStrategy,
)

TOKYO_ONLY = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
TOKYO_LONDON_OVERLAP = datetime(2026, 1, 15, 8, 30, tzinfo=UTC)
LONDON_ONLY = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
LONDON_NY_OVERLAP = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)
OFF_SESSION = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)

USDJPY_CORE = SessionProfile(
    sessions={"tokyo": "ALLOWED", "london": "ALLOWED", "new_york": "PREFERRED"}
)
LONDON_NY_MAJOR = SessionProfile(
    sessions={"tokyo": "DISABLED", "london": "PREFERRED", "new_york": "PREFERRED"}
)
SHADOW_EVERYWHERE = {"tokyo": "SHADOW_ONLY", "london": "SHADOW_ONLY", "new_york": "SHADOW_ONLY"}
CLOSED_EVERYWHERE = {"tokyo": "DISABLED", "london": "DISABLED", "new_york": "DISABLED"}


def test_policy_at_reads_the_open_session():
    assert USDJPY_CORE.policy_at(TOKYO_ONLY) is SessionEntryPolicy.ALLOWED
    assert USDJPY_CORE.policy_at(LONDON_NY_OVERLAP) is SessionEntryPolicy.PREFERRED
    assert USDJPY_CORE.policy_at(OFF_SESSION) is None


def test_overlapping_sessions_take_the_most_permissive_policy():
    assert LONDON_NY_MAJOR.policy_at(TOKYO_LONDON_OVERLAP) is SessionEntryPolicy.PREFERRED


def test_session_missing_from_the_profile_yields_no_policy():
    tokyo_only = SessionProfile(sessions={"tokyo": "PREFERRED"})

    assert tokyo_only.policy_at(LONDON_ONLY) is None


@pytest.mark.parametrize(
    ("policy", "live", "expected"),
    [
        (SessionEntryPolicy.PREFERRED, False, True),
        (SessionEntryPolicy.PREFERRED, True, True),
        (SessionEntryPolicy.ALLOWED, False, True),
        (SessionEntryPolicy.ALLOWED, True, True),
        (SessionEntryPolicy.SHADOW_ONLY, False, True),
        (SessionEntryPolicy.SHADOW_ONLY, True, False),
        (SessionEntryPolicy.DISABLED, False, False),
        (SessionEntryPolicy.DISABLED, True, False),
    ],
)
def test_permits_entry_by_policy_and_liveness(policy, live, expected):
    profile = SessionProfile(sessions={"tokyo": policy})

    assert profile.permits_entry(TOKYO_ONLY, live=live) is expected


@pytest.mark.parametrize("live", [False, True])
def test_no_open_session_permits_nothing(live):
    everywhere = SessionProfile(
        sessions={"tokyo": "PREFERRED", "london": "PREFERRED", "new_york": "PREFERRED"}
    )

    assert everywhere.permits_entry(OFF_SESSION, live=live) is False


class GateProbe(Strategy):
    strategy_id = "gate_probe"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.INTRADAY

    async def on_event(self, event, context):
        return []


def ctx_for(strategy_id, instant, *, status, profile=None):
    if profile is None:
        session_profiles, parameters = {}, {}
    else:
        session_profiles = {"probe": profile}
        parameters = {"instruments": {"USDJPY": {"session_profile": "probe"}}}
    config = StrategyConfig(
        strategy_id=strategy_id,
        status=status,
        instruments=["USDJPY"],
        session_profiles=session_profiles,
        parameters=parameters,
    )
    return SimpleNamespace(clock=FixedClock(instant), config=config)


def test_instrument_without_profile_is_never_gated():
    ctx = ctx_for("gate_probe", OFF_SESSION, status=StrategyStatus.MICRO_LIVE)

    assert GateProbe()._session_permits_entry(ctx, "USDJPY") is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (StrategyStatus.RESEARCH_ONLY, True),
        (StrategyStatus.BACKTEST_ELIGIBLE, True),
        (StrategyStatus.SHADOW, True),
        (StrategyStatus.MICRO_LIVE, False),
        (StrategyStatus.LIMITED_LIVE, False),
        (StrategyStatus.PRODUCTION, False),
    ],
)
def test_shadow_only_session_evaluates_only_below_live(status, expected):
    ctx = ctx_for("gate_probe", TOKYO_ONLY, status=status, profile=SHADOW_EVERYWHERE)

    assert GateProbe()._session_permits_entry(ctx, "USDJPY") is expected


def test_disabled_session_is_closed_even_in_shadow():
    ctx = ctx_for(
        "gate_probe", TOKYO_ONLY, status=StrategyStatus.SHADOW, profile=CLOSED_EVERYWHERE
    )

    assert GateProbe()._session_permits_entry(ctx, "USDJPY") is False


STRATEGIES = [
    FailedSpikeReversalStrategy,
    PostEventFailedBreakoutStrategy,
    MonetaryPolicyConvergenceStrategy,
]


def recording(strategy):
    calls = []
    strategy._evaluate = lambda symbol, ctx: calls.append((symbol, ctx))
    return calls


@pytest.mark.parametrize("strategy_class", STRATEGIES)
async def test_closed_session_stops_before_evaluation(strategy_class):
    strategy = strategy_class()
    calls = recording(strategy)
    ctx = ctx_for(
        strategy_class.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=CLOSED_EVERYWHERE,
    )

    assert await strategy.on_event(make_event(), ctx) == []
    assert calls == []


@pytest.mark.parametrize("strategy_class", STRATEGIES)
async def test_instrument_without_profile_reaches_evaluation(strategy_class):
    strategy = strategy_class()
    calls = recording(strategy)
    ctx = ctx_for(strategy_class.strategy_id, TOKYO_ONLY, status=StrategyStatus.SHADOW)

    assert await strategy.on_event(make_event(), ctx) == []
    assert calls == [("USDJPY", ctx)]
