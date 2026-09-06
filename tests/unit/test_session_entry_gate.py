"""session profile が strategy の entry と決済専用 signal を gate する（ADR-028 / ADR-031）。

DST 境界をまたぐ session 判定は tests/unit/test_session.py が担う。ここでは
冬時間の固定 instant だけを使い、policy × StrategyStatus の表と gate の位置を固定する。
"""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import FixedClock, at, make_bar, make_event, usdjpy_spec
from trading.domain.position import PositionDirection, VirtualPosition
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.strategy.base import (
    SESSION_CLOSED_EXIT_ONLY,
    Strategy,
    StrategyConfig,
    StrategyHorizon,
    StrategyStatus,
)
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


def ctx_for(strategy_id, instant, *, status, profile=None, held: VirtualPosition | None = None):
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
    portfolio = SimpleNamespace(position=lambda _strategy_id, _symbol: held)
    return SimpleNamespace(clock=FixedClock(instant), config=config, portfolio=portfolio)


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
async def test_closed_session_without_a_position_stops_before_evaluation(strategy_class):
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
async def test_closed_session_with_a_position_reaches_evaluation(strategy_class):
    strategy = strategy_class()
    calls = recording(strategy)
    position = VirtualPosition(
        strategy_id=strategy_class.strategy_id,
        symbol="USDJPY",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        as_of=TOKYO_ONLY,
    )
    ctx = ctx_for(
        strategy_class.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=CLOSED_EVERYWHERE,
        held=position,
    )

    assert await strategy.on_event(make_event(), ctx) == []
    assert calls == [("USDJPY", ctx)]


@pytest.mark.parametrize("strategy_class", STRATEGIES)
async def test_instrument_without_profile_reaches_evaluation(strategy_class):
    strategy = strategy_class()
    calls = recording(strategy)
    ctx = ctx_for(strategy_class.strategy_id, TOKYO_ONLY, status=StrategyStatus.SHADOW)

    assert await strategy.on_event(make_event(), ctx) == []
    assert calls == [("USDJPY", ctx)]


def setup_signal(probe, ctx, setup_id):
    return probe._setup_signal(
        ctx,
        symbol="USDJPY",
        direction=PositionDirection.SHORT,
        setup_id=setup_id,
        conviction=0.5,
        stop_distance_pips=Decimal(10),
        expected_horizon_seconds=60,
        reason_codes=["PROBE"],
    )


def test_open_session_setup_becomes_an_entry_signal_once():
    probe = GateProbe()
    ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=USDJPY_CORE,
    )

    signal = setup_signal(probe, ctx, TOKYO_ONLY)

    assert signal is not None
    assert signal.exit_only is False
    assert signal.desired_direction is PositionDirection.SHORT
    assert setup_signal(probe, ctx, TOKYO_ONLY) is None


def test_closed_session_setup_without_a_position_is_not_remembered():
    probe = GateProbe()
    closed_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=CLOSED_EVERYWHERE,
    )
    open_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=USDJPY_CORE,
    )

    assert setup_signal(probe, closed_ctx, TOKYO_ONLY) is None
    assert setup_signal(probe, open_ctx, TOKYO_ONLY) is not None


def test_closed_session_reversal_setup_becomes_an_exit_only_signal():
    probe = GateProbe()
    position = VirtualPosition(
        strategy_id=probe.strategy_id,
        symbol="USDJPY",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        as_of=TOKYO_ONLY,
    )
    closed_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=CLOSED_EVERYWHERE,
        held=position,
    )
    open_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=USDJPY_CORE,
        held=position,
    )

    signal = setup_signal(probe, closed_ctx, TOKYO_ONLY)

    assert signal is not None
    assert signal.exit_only is True
    assert signal.desired_direction is PositionDirection.SHORT
    assert "PROBE" in signal.reason_codes
    assert SESSION_CLOSED_EXIT_ONLY in signal.reason_codes
    assert setup_signal(probe, closed_ctx, TOKYO_ONLY) is None
    reopened_signal = setup_signal(probe, open_ctx, TOKYO_ONLY)
    assert reopened_signal is not None
    assert reopened_signal.exit_only is False


def test_closed_session_same_direction_setup_is_dropped():
    probe = GateProbe()
    position = VirtualPosition(
        strategy_id=probe.strategy_id,
        symbol="USDJPY",
        direction=PositionDirection.SHORT,
        quantity=Decimal(1000),
        as_of=TOKYO_ONLY,
    )
    closed_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=CLOSED_EVERYWHERE,
        held=position,
    )
    open_ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=USDJPY_CORE,
        held=position,
    )

    assert setup_signal(probe, closed_ctx, TOKYO_ONLY) is None
    signal = setup_signal(probe, open_ctx, TOKYO_ONLY)
    assert signal is not None
    assert signal.exit_only is False


def test_open_session_ignores_the_held_position():
    probe = GateProbe()
    position = VirtualPosition(
        strategy_id=probe.strategy_id,
        symbol="USDJPY",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        as_of=TOKYO_ONLY,
    )
    ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=USDJPY_CORE,
        held=position,
    )

    signal = setup_signal(probe, ctx, TOKYO_ONLY)

    assert signal is not None
    assert signal.exit_only is False


@pytest.mark.parametrize(
    "profile,held_direction,direction,expected",
    [
        # gate が開いていれば保有は entry の邪魔をしない。
        (USDJPY_CORE, PositionDirection.SHORT, PositionDirection.SHORT, True),
        (USDJPY_CORE, PositionDirection.SHORT, PositionDirection.LONG, True),
        # 閉鎖中は保有と逆向き（決済になる向き）だけ。
        (CLOSED_EVERYWHERE, PositionDirection.SHORT, PositionDirection.SHORT, False),
        (CLOSED_EVERYWHERE, PositionDirection.SHORT, PositionDirection.LONG, True),
        # 閉鎖中で保有が無ければどの向きも signal にならない。
        (CLOSED_EVERYWHERE, None, PositionDirection.SHORT, False),
        (CLOSED_EVERYWHERE, None, PositionDirection.LONG, False),
        # profile を参照しない instrument は gate されない。
        (None, None, PositionDirection.SHORT, True),
    ],
)
def test_session_permits_setup_by_gate_and_held_direction(
    profile, held_direction, direction, expected
):
    probe = GateProbe()
    held = (
        None
        if held_direction is None
        else VirtualPosition(
            strategy_id=probe.strategy_id,
            symbol="USDJPY",
            direction=held_direction,
            quantity=Decimal(1000),
            as_of=TOKYO_ONLY,
        )
    )
    ctx = ctx_for(
        probe.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=profile,
        held=held,
    )

    assert probe._session_permits_setup(ctx, "USDJPY", direction) is expected


def intraday_ctx(*, profile, held=None):
    """両方向の failed breakout が同じバーで成立し、両方の macro gate が開く context。

    `_evaluate` は setup を上から順に見るので、先に来る SHORT 分岐が後続の LONG 分岐を
    塞がないことを、この 1 つの盤面で確かめられる。
    """
    setup_bars = [
        make_bar(
            "150.00", "150.50", "149.50", "150.00", start=at(minutes=15 * i), timeframe="15m"
        )
        for i in range(21)
    ]
    entry_bars = [
        make_bar(
            "150.00", "150.10", "149.90", "150.00", start=at(minutes=5 * i), timeframe="5m"
        )
        for i in range(3)
    ]
    # resistance(150.50) を上抜けて戻り、同時に support(149.50) を下抜けて戻る外側バー。
    entry_bars.append(
        make_bar("150.00", "150.80", "149.20", "150.00", start=at(minutes=15), timeframe="5m")
    )
    entry_bars.append(
        make_bar("150.00", "150.10", "149.90", "150.00", start=at(minutes=20), timeframe="5m")
    )

    features = InMemoryFeatureStore()
    features.set(f.US_DATA_SURPRISE, -0.5)
    features.set(f.US2Y_CHANGE_5D, 0.10)
    features.set(f.US2Y_CHANGE_1D, 0.04)
    features.set(f.INTERVENTION_RISK, 0.1)

    ctx = ctx_for(
        PostEventFailedBreakoutStrategy.strategy_id,
        TOKYO_ONLY,
        status=StrategyStatus.SHADOW,
        profile=profile,
        held=held,
    )
    ctx.market = SimpleNamespace(
        instrument=lambda _symbol: usdjpy_spec(),
        bars=lambda _symbol, timeframe, _count: (
            setup_bars if timeframe == "15m" else entry_bars
        ),
    )
    ctx.indicators = SimpleNamespace(atr=lambda _symbol, _timeframe, _period: 0.10)
    ctx.features = features
    return ctx


def test_closed_session_same_direction_setup_does_not_swallow_the_reversal():
    strategy = PostEventFailedBreakoutStrategy()
    held = VirtualPosition(
        strategy_id=strategy.strategy_id,
        symbol="USDJPY",
        direction=PositionDirection.SHORT,
        quantity=Decimal(1000),
        as_of=TOKYO_ONLY,
    )
    ctx = intraday_ctx(profile=CLOSED_EVERYWHERE, held=held)

    signal = strategy._evaluate("USDJPY", ctx)

    assert signal is not None
    assert signal.desired_direction is PositionDirection.LONG
    assert signal.exit_only is True
    assert SESSION_CLOSED_EXIT_ONLY in signal.reason_codes


def test_open_session_first_setup_still_consumes_the_evaluation():
    strategy = PostEventFailedBreakoutStrategy()
    ctx = intraday_ctx(profile=USDJPY_CORE)

    first = strategy._evaluate("USDJPY", ctx)

    assert first is not None
    assert first.desired_direction is PositionDirection.SHORT
    assert first.exit_only is False
    # 同じ setup を dedupe で落としたあと、反対向きの entry signal に化けない。
    assert strategy._evaluate("USDJPY", ctx) is None
