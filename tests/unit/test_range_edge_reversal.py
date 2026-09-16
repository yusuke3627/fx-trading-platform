"""事前登録したレンジ・戻り・価格条件と、既存決済経路との接続。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import FixedClock, held, make_bar, make_event, make_tick, usdjpy_spec
from trading.data.market import InMemoryMarketData
from trading.domain.position import PositionDirection
from trading.indicators import IndicatorService
from trading.indicators.ema import ema_series
from trading.indicators.session import Session, session_start, sessions_at
from trading.intelligence.features import InMemoryFeatureStore
from trading.strategy.base import (
    HORIZON_EXPIRED,
    StrategyConfig,
    StrategyHorizon,
    StrategyStatus,
    TimeframeMap,
    market_span_to_calendar,
)
from trading.strategy.intraday.range_edge_reversal import RangeEdgeReversalStrategy
from trading.strategy.parameters import StrategyParameters
from trading.strategy.registry import STRATEGIES
from trading.strategy.sessions import SessionProfile

NOW = datetime(2026, 1, 15, 10, tzinfo=UTC)
LONDON_START = NOW.replace(hour=8)
USDJPY_CORE = SessionProfile(
    sessions={"tokyo": "ALLOWED", "london": "ALLOWED", "new_york": "PREFERRED"}
)
DEFAULTS = {
    "range_lookback_bars": 24,
    "range_stale_hours": 72,
    "range_slope_lookback": 6,
    "range_slope_max_atr": 0.5,
    "range_width_min_atr": 1.5,
    "ema_period": 20,
    "atr_period": 14,
    "reentry_max_bars": 6,
    "entry_band_fraction": 0.2,
    "stop_buffer_atr": 0.25,
    "min_reward_to_risk": 1.5,
    "take_profit_enabled": True,
    "horizon_exit_enabled": True,
    "expected_horizon_seconds": 3600,
    "session_end_buffer_seconds": 3600,
    "spread_gate": {"max_spread_to_atr": "0.5"},
}


def range_context(
    regime_bars,
    entry_bars,
    tick,
    *,
    now,
    params=None,
    position=None,
    atr_regime=0.20,
    atr_entry=0.04,
    profile=USDJPY_CORE,
    status=StrategyStatus.RESEARCH_ONLY,
):
    instrument_params = {"absolute_max_spread_pips": "1.5"}
    if profile is not None:
        instrument_params["session_profile"] = "probe"
    config = StrategyConfig(
        strategy_id="range_edge_reversal",
        status=status,
        instruments=["USDJPY"],
        timeframes=TimeframeMap(regime="1h", entry="5m"),
        parameters=StrategyParameters(
            defaults={**DEFAULTS, **(params or {})},
            instruments={"USDJPY": instrument_params},
        ),
        session_profiles={} if profile is None else {"probe": profile},
    )
    clock = FixedClock(now)

    def bars(_symbol, timeframe, count):
        source = entry_bars if timeframe == "5m" else regime_bars
        return [bar for bar in source if bar.known_at <= clock.now()][-count:]

    return SimpleNamespace(
        config=config,
        market=SimpleNamespace(
            instrument=lambda _symbol: usdjpy_spec(),
            bars=bars,
            latest_tick=lambda _symbol: tick,
        ),
        indicators=SimpleNamespace(
            atr=lambda _symbol, timeframe, _period: atr_entry if timeframe == "5m" else atr_regime
        ),
        features=InMemoryFeatureStore(),
        clock=clock,
        portfolio=SimpleNamespace(
            position=lambda strategy_id, symbol: (
                position
                if position is not None
                and (position.strategy_id, position.symbol) == (strategy_id, symbol)
                else None
            )
        ),
    )


def flat_regime_bars(anchor=LONDON_START, count=30, high="150.00", low="149.00", close="149.50"):
    return [
        make_bar(
            close,
            high,
            low,
            close,
            start=anchor - timedelta(hours=count - i),
            timeframe="1h",
        )
        for i in range(count)
    ]


def entry_bars_from(rows, now=NOW, direction=PositionDirection.LONG):
    bars = []
    for i, (open_, high, low, close) in enumerate(rows):
        if direction is PositionDirection.SHORT:
            open_, high, low, close = (
                str(Decimal(299) - Decimal(value)) for value in (open_, low, high, close)
            )
        bars.append(
            make_bar(
                open_,
                high,
                low,
                close,
                start=now - timedelta(minutes=5 * (len(rows) - i)),
                timeframe="5m",
            )
        )
    return bars


def reentry_bars(now=NOW, direction=PositionDirection.LONG, run=1):
    inside = ("149.20", "149.30", "149.10", "149.20")
    breach = ("149.10", "149.15", "148.90", "148.95")
    outside = ("148.95", "148.99", "148.92", "148.95")
    returned = ("148.95", "149.08", "148.92", "149.05")
    rows = [inside] * (6 - run)
    if run:
        rows += [breach] + [outside] * (run - 1)
    rows += [returned]
    return entry_bars_from(rows, now, direction)


def entry_tick(now=NOW, direction=PositionDirection.LONG):
    return make_tick(
        *("149.05", "149.06") if direction is PositionDirection.LONG else ("149.94", "149.95"),
        time=now,
    )


def prepared(
    *,
    now=NOW,
    direction=PositionDirection.LONG,
    regime_bars=None,
    entry_bars=None,
    tick=None,
    built_at=None,
    **kwargs,
):
    start = max(session_start(session, now) for session in sessions_at(now))
    ctx = range_context(
        flat_regime_bars(start) if regime_bars is None else regime_bars,
        reentry_bars(now, direction) if entry_bars is None else entry_bars,
        entry_tick(now, direction) if tick is None else tick,
        now=start if built_at is None else built_at,
        **kwargs,
    )
    strategy = RangeEdgeReversalStrategy()
    assert strategy._evaluate("USDJPY", ctx) is None
    ctx.clock.advance(seconds=(now - ctx.clock.now()).total_seconds())
    return strategy, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize("take_profit_enabled", [True, False])
async def test_reentry_emits_quote_based_stop_and_midpoint_target(direction, take_profit_enabled):
    strategy, ctx = prepared(
        direction=direction, params={"take_profit_enabled": take_profit_enabled}
    )
    signals = await strategy.on_event(make_event(known_at=NOW), ctx)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.desired_direction is direction
    assert signal.stop_distance_pips == Decimal("17.0")
    assert signal.take_profit_distance_pips == (Decimal("44.0") if take_profit_enabled else None)
    assert signal.exit_only is False
    assert signal.conviction == 0.5
    assert signal.expected_edge_r == Decimal(1)
    assert signal.expected_horizon_seconds == 3600
    assert signal.strategy_version == "0.1.0"
    edge = "LOWER" if direction is PositionDirection.LONG else "UPPER"
    assert signal.reason_codes == ["RANGE_REGIME_FLAT", f"RANGE_{edge}_EDGE_REENTRY"]


def test_range_is_fixed_until_a_later_session_opens_and_removed_outside_sessions():
    start = NOW.replace(hour=0)
    bars = flat_regime_bars(start)
    ctx = range_context(bars, [], None, now=start + timedelta(hours=2))
    strategy = RangeEdgeReversalStrategy()
    assert strategy._evaluate("USDJPY", ctx) is None
    original = strategy._ranges["USDJPY"]
    assert (original.session, original.start) == (Session.TOKYO, start)
    bars.append(
        make_bar(
            "149.5", "150.4", "148.8", "149.5", start=start + timedelta(hours=2), timeframe="1h"
        )
    )
    ctx.clock.advance(hours=1)
    assert strategy._evaluate("USDJPY", ctx) is None
    assert strategy._ranges["USDJPY"] == original
    ctx.clock.advance(hours=5, minutes=30)
    strategy._evaluate("USDJPY", ctx)
    london = strategy._ranges["USDJPY"]
    assert (london.session, london.start) == (Session.LONDON, LONDON_START)
    assert (london.high, london.low) == (Decimal("150.4"), Decimal("148.8"))
    ctx.clock.advance(hours=5)
    strategy._evaluate("USDJPY", ctx)
    new_york = strategy._ranges["USDJPY"]
    assert (new_york.session, new_york.start) == (Session.NEW_YORK, NOW.replace(hour=13))
    ctx.clock.advance(hours=9, minutes=30)
    assert strategy._evaluate("USDJPY", ctx) is None
    assert "USDJPY" not in strategy._ranges


def test_late_first_evaluation_builds_from_currently_known_bars():
    bars = flat_regime_bars()
    bars.append(make_bar("149.5", "151", "149", "149.5", start=LONDON_START, timeframe="1h"))
    ctx = range_context(bars, [], None, now=NOW)
    strategy = RangeEdgeReversalStrategy()
    strategy._evaluate("USDJPY", ctx)
    rng = strategy._ranges["USDJPY"]
    assert rng.high == 151.0
    assert rng.built_at == NOW
    assert rng.start == LONDON_START
    assert rng.invalidated is False


@pytest.mark.parametrize("count", [20, 24, 25, 26])
def test_regime_requires_all_declared_bars_and_does_not_rebuild_when_missing_bars_arrive(count):
    bars = flat_regime_bars(count=count)
    strategy, ctx = prepared(regime_bars=bars)
    assert strategy._ranges["USDJPY"].eligible is (count >= 26)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is (count >= 26)
    if count < 26:
        original = strategy._ranges["USDJPY"]
        bars[:] = flat_regime_bars()
        assert strategy._evaluate("USDJPY", ctx) is None
        assert strategy._ranges["USDJPY"] == original


@pytest.mark.parametrize(("age", "eligible"), [(49, True), (50, False)])
def test_oldest_range_bar_must_be_no_older_than_72_hours(age, eligible):
    bars = flat_regime_bars(LONDON_START - timedelta(hours=age))
    strategy, ctx = prepared(regime_bars=bars)
    assert strategy._ranges["USDJPY"].eligible is eligible
    assert (strategy._evaluate("USDJPY", ctx) is not None) is eligible
    if not eligible:
        bars[:] = flat_regime_bars()
        assert strategy._evaluate("USDJPY", ctx) is None
        assert strategy._ranges["USDJPY"].eligible is False


def test_trending_ema_and_insufficient_range_width_are_ineligible():
    bars = flat_regime_bars(high="151", low="148")
    bars = [
        bar.model_copy(
            update={
                "open": Decimal(149) + Decimal("0.05") * i,
                "close": Decimal(149) + Decimal("0.05") * i,
            }
        )
        for i, bar in enumerate(bars)
    ]
    series = ema_series([float(bar.close) for bar in bars[-26:]], 20)
    assert len(series) == 7
    assert series[0] == pytest.approx(149.675)
    assert series[-1] == pytest.approx(149.975)
    for regime in (bars, flat_regime_bars(high="149.2", low="149", close="149.1")):
        strategy, ctx = prepared(regime_bars=regime)
        assert strategy._evaluate("USDJPY", ctx) is None
        assert strategy._ranges["USDJPY"].eligible is False


@pytest.mark.parametrize(("last_close", "eligible"), [("154.25", True), ("154.251", False)])
def test_ema_slope_upper_boundary_is_inclusive(last_close, eligible):
    bars = flat_regime_bars(count=26, high="155", low="148", close="149")
    bars[-1] = bars[-1].model_copy(
        update={"open": Decimal(last_close), "close": Decimal(last_close)}
    )
    strategy, _ = prepared(regime_bars=bars, atr_regime=1.0)
    assert strategy._ranges["USDJPY"].eligible is eligible


@pytest.mark.parametrize(("high", "eligible"), [("149.75", True), ("149.749", False)])
def test_range_width_lower_boundary_is_inclusive(high, eligible):
    bars = flat_regime_bars(high=high, close="149.375")
    strategy, _ = prepared(regime_bars=bars, atr_regime=0.5)
    assert strategy._ranges["USDJPY"].eligible is eligible


@pytest.mark.parametrize(("high", "eligible"), [("149.350", True), ("149.349", False)])
def test_decimal_range_width_at_one_point_five_atr_is_eligible(high, eligible):
    bars = flat_regime_bars(high=high, low="149.05", close="149.20")
    strategy, _ = prepared(regime_bars=bars, atr_regime=0.20)
    # 幅 0.300 は 0.20 × 1.5 と等しく、0.299 は下限未満。
    assert strategy._ranges["USDJPY"].eligible is eligible


@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize(("run", "emits"), [(0, True), (5, True), (6, False)])
def test_reentry_counts_breach_bar_and_allows_at_most_six_observed_bars(direction, run, emits):
    strategy, ctx = prepared(
        direction=direction, entry_bars=reentry_bars(direction=direction, run=run)
    )
    signal = strategy._evaluate("USDJPY", ctx)
    assert (signal is not None) is emits
    if emits:
        assert signal.desired_direction is direction


@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize(("price", "emits"), [("149.20", True), ("149.201", False)])
def test_entry_band_boundary_is_inclusive(direction, price, emits):
    price = Decimal(price)
    bid, ask = price - Decimal("0.01"), price
    if direction is PositionDirection.SHORT:
        bid, ask = Decimal(299) - ask, Decimal(299) - bid
    strategy, ctx = prepared(
        direction=direction,
        tick=make_tick(str(bid), str(ask), time=NOW),
        params={"min_reward_to_risk": 0.5},
    )
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


@pytest.mark.parametrize("take_profit_enabled", [True, False])
def test_deep_breach_fails_reward_to_risk_even_without_take_profit(take_profit_enabled):
    bars = reentry_bars()
    bars[-2] = bars[-2].model_copy(update={"low": Decimal("148.60")})
    strategy, ctx = prepared(entry_bars=bars, params={"take_profit_enabled": take_profit_enabled})
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize(("ask", "emits"), [("149.50", True), ("149.501", False)])
def test_reward_to_risk_lower_boundary_is_inclusive(ask, emits):
    bars = entry_bars_from(
        [("149.5", "149.6", "149.4", "149.5")] * 5
        + [("149.4", "149.5", "149.125", "149.2"), ("149.2", "149.6", "149.15", "149.5")]
    )
    strategy, ctx = prepared(
        regime_bars=flat_regime_bars(high="151.25", low="149.25", close="150.25"),
        entry_bars=bars,
        tick=make_tick(str(Decimal(ask) - Decimal("0.01")), ask, time=NOW),
        atr_entry=0.5,
    )
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize("take_profit_enabled", [True, False])
@pytest.mark.parametrize(("price", "emits"), [("149.080", True), ("149.081", False)])
def test_decimal_quote_at_exact_reward_to_risk_boundary(direction, take_profit_enabled, price, emits):
    bars = reentry_bars(direction=direction)
    price = Decimal(price)
    bid, ask = price - Decimal("0.01"), price
    if direction is PositionDirection.LONG:
        bars[-2] = bars[-2].model_copy(update={"low": Decimal("148.81")})
    else:
        bars[-2] = bars[-2].model_copy(update={"high": Decimal("150.19")})
        bid, ask = Decimal(299) - ask, Decimal(299) - bid
    strategy, ctx = prepared(
        direction=direction,
        entry_bars=bars,
        tick=make_tick(str(bid), str(ask), time=NOW),
        params={"take_profit_enabled": take_profit_enabled},
    )
    signal = strategy._evaluate("USDJPY", ctx)
    assert (signal is not None) is emits
    if emits:
        # 損切り 148.80 / 150.20、損失幅 0.28、中央まで 0.42 で RR = 1.5。
        assert signal.desired_direction is direction
        assert signal.stop_distance_pips == Decimal("28.0")
        assert signal.take_profit_distance_pips == (Decimal("42.0") if take_profit_enabled else None)


@pytest.mark.parametrize(
    ("spread", "atr_entry", "emits"),
    [
        ("0.03", 0.04, False),
        ("0.015", 0.04, True),
        ("0.016", 0.04, False),
        ("0.015", 0.03, True),
        ("0.015", 0.029, False),
    ],
)
def test_spread_gate_checks_absolute_and_atr_limits_separately(spread, atr_entry, emits):
    tick = make_tick(str(Decimal("149.06") - Decimal(spread)), "149.06", time=NOW)
    strategy, ctx = prepared(tick=tick, atr_entry=atr_entry)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


@pytest.mark.parametrize("first_direction", list(PositionDirection))
def test_one_signal_per_range_and_direction_including_later_attempts(first_direction):
    bars = reentry_bars(direction=first_direction)
    strategy, ctx = prepared(direction=first_direction, entry_bars=bars)
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._evaluate("USDJPY", ctx) is None
    ctx.clock.advance(minutes=5)
    bars[:] = reentry_bars(ctx.clock.now(), first_direction)
    assert strategy._evaluate("USDJPY", ctx) is None
    opposite = (
        PositionDirection.SHORT
        if first_direction is PositionDirection.LONG
        else PositionDirection.LONG
    )
    bars[:] = reentry_bars(ctx.clock.now(), opposite)
    ctx.market.latest_tick = lambda _symbol: entry_tick(ctx.clock.now(), opposite)
    signal = strategy._evaluate("USDJPY", ctx)
    assert signal is not None
    assert signal.desired_direction is opposite
    ctx.clock.advance(hours=3)
    bars.clear()
    assert strategy._evaluate("USDJPY", ctx) is None
    ctx.clock.advance(minutes=15)
    bars[:] = reentry_bars(ctx.clock.now(), first_direction)
    ctx.market.latest_tick = lambda _symbol: entry_tick(ctx.clock.now(), first_direction)
    assert strategy._evaluate("USDJPY", ctx) is not None


@pytest.mark.parametrize("rejected_by", ["band", "rr"])
def test_rejected_attempt_does_not_consume_setup_slot(rejected_by):
    bars = reentry_bars()
    strategy, ctx = prepared(entry_bars=bars)
    if rejected_by == "band":
        ctx.market.latest_tick = lambda _symbol: make_tick("149.24", "149.25", time=NOW)
    else:
        bars[-2] = bars[-2].model_copy(update={"low": Decimal("148.6")})
    assert strategy._evaluate("USDJPY", ctx) is None
    bars[:] = reentry_bars()
    ctx.market.latest_tick = lambda _symbol: entry_tick()
    assert strategy._evaluate("USDJPY", ctx) is not None


@pytest.mark.parametrize(
    ("close", "invalidated"),
    [("150.3", True), ("148.8", True), ("150", False), ("149", False), ("149.5", False)],
)
def test_h1_close_invalidates_permanently_but_boundary_and_wicks_do_not(close, invalidated):
    regime = flat_regime_bars()
    strategy, ctx = prepared(regime_bars=regime)
    regime.append(
        make_bar("149.5", "150.4", "148.7", close, start=NOW - timedelta(hours=1), timeframe="1h")
    )
    assert (strategy._evaluate("USDJPY", ctx) is None) is invalidated
    assert strategy._ranges["USDJPY"].invalidated is invalidated
    if invalidated:
        regime.append(make_bar("149.5", "149.6", "149.4", "149.5", start=NOW, timeframe="1h"))
        ctx.clock.advance(hours=1)
        assert strategy._evaluate("USDJPY", ctx) is None
        assert strategy._ranges["USDJPY"].invalidated is True
        ctx.clock.advance(hours=2)
        strategy._evaluate("USDJPY", ctx)
        assert strategy._ranges["USDJPY"].invalidated is False


@pytest.mark.parametrize(
    ("hour", "minute", "second", "emits"),
    [
        (20, 59, 59, True),
        (21, 0, 0, True),
        (21, 0, 1, False),
        (21, 30, 0, False),
        (16, 30, 0, True),
    ],
)
def test_entry_stops_only_with_less_than_one_hour_to_the_latest_session_end(
    hour,
    minute,
    second,
    emits,
):
    now = NOW.replace(hour=hour, minute=minute, second=second)
    last_bar_time = now.replace(minute=minute // 5 * 5, second=0)
    strategy, ctx = prepared(now=now, entry_bars=reentry_bars(last_bar_time))
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


@pytest.mark.asyncio
async def test_session_end_does_not_generate_a_close_by_itself():
    now = NOW.replace(hour=22)
    position = held(PositionDirection.LONG).model_copy(
        update={"strategy_id": "range_edge_reversal", "as_of": now - timedelta(minutes=10)}
    )
    ctx = range_context(
        flat_regime_bars(), reentry_bars(now), entry_tick(now), now=now, position=position
    )
    assert await RangeEdgeReversalStrategy().on_event(make_event(known_at=now), ctx) == []


@pytest.mark.parametrize("missing", ["spec", "tick", "regime_atr", "entry_atr", "entry_bars"])
@pytest.mark.parametrize("empty_value", [None, 0])
def test_missing_inputs_do_not_emit_a_signal(missing, empty_value):
    kwargs = {}
    if missing == "regime_atr":
        kwargs["atr_regime"] = empty_value
    elif missing == "entry_atr":
        kwargs["atr_entry"] = empty_value
    elif missing == "entry_bars":
        kwargs["entry_bars"] = reentry_bars()[-6:] if empty_value == 0 else []
    strategy, ctx = prepared(**kwargs)
    if missing == "spec":
        ctx.market.instrument = lambda _symbol: None
    elif missing == "tick":
        ctx.market.latest_tick = lambda _symbol: None
    assert strategy._evaluate("USDJPY", ctx) is None


def test_future_bars_neither_change_range_nor_invalidate_or_trigger_it():
    regime = flat_regime_bars()
    future_h1 = make_bar("149.5", "152", "149", "151", start=NOW, timeframe="1h")
    regime.append(future_h1)
    entry = reentry_bars()
    entry.append(make_bar("149.05", "151", "148", "151", start=NOW, timeframe="5m"))
    strategy, ctx = prepared(regime_bars=regime, entry_bars=entry)
    assert strategy._ranges["USDJPY"].high == 150.0
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._ranges["USDJPY"].invalidated is False
    strategy, ctx = prepared(entry_bars=reentry_bars(NOW + timedelta(minutes=10)))
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize("direction", list(PositionDirection))
def test_touching_the_edge_without_breaching_is_not_a_setup(direction):
    bars = reentry_bars(direction=direction, run=0)
    update = (
        {"low": Decimal(149), "open": Decimal(149)}
        if (direction is PositionDirection.LONG)
        else {"high": Decimal(150), "open": Decimal(150)}
    )
    bars[-1] = bars[-1].model_copy(update=update)
    strategy, ctx = prepared(direction=direction, entry_bars=bars)
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize(
    ("close", "emits"),
    [("149", True), ("150", True), ("148.999", False), ("150.001", False)],
)
def test_return_close_must_be_inside_inclusive_range_edges(direction, close, emits):
    bars = reentry_bars(direction=direction)
    close = Decimal(close) if direction is PositionDirection.LONG else Decimal(299) - Decimal(close)
    bars[-1] = bars[-1].model_copy(
        update={
            "close": close,
            "high": max(bars[-1].high, close),
            "low": min(bars[-1].low, close),
        }
    )
    strategy, ctx = prepared(direction=direction, entry_bars=bars)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


@pytest.mark.parametrize("direction", list(PositionDirection))
def test_stop_uses_the_extreme_of_the_whole_attempt(direction):
    bars = reentry_bars(direction=direction, run=5)
    update = (
        {"low": Decimal("148.85")}
        if direction is PositionDirection.LONG
        else {"high": Decimal("150.15")}
    )
    bars[-3] = bars[-3].model_copy(update=update)
    strategy, ctx = prepared(direction=direction, entry_bars=bars)
    signal = strategy._evaluate("USDJPY", ctx)
    assert signal is not None
    assert signal.stop_distance_pips == Decimal("22.0")


@pytest.mark.parametrize(("built_minutes_ago", "emits"), [(4, False), (5, False), (6, True)])
def test_breach_must_be_known_strictly_after_range_construction(built_minutes_ago, emits):
    strategy, ctx = prepared(built_at=NOW - timedelta(minutes=built_minutes_ago))
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


def test_six_observed_bars_can_span_more_than_thirty_minutes():
    bars = reentry_bars(run=5)
    bars = [
        bar.model_copy(
            update={
                "known_at": NOW - timedelta(minutes=10 * (6 - i)),
                "start": NOW - timedelta(minutes=10 * (6 - i) + 5),
            }
        )
        for i, bar in enumerate(bars)
    ]
    assert bars[-1].known_at - bars[1].known_at == timedelta(minutes=50)
    strategy, ctx = prepared(entry_bars=bars)
    assert strategy._evaluate("USDJPY", ctx) is not None


@pytest.mark.parametrize("direction", list(PositionDirection))
@pytest.mark.parametrize("price", ["148.88", "148.89", "148.8901"])
def test_nonpositive_or_rounded_zero_stop_is_rejected(direction, price):
    price = Decimal(price)
    bid, ask = price - Decimal("0.01"), price
    if direction is PositionDirection.SHORT:
        bid, ask = Decimal(299) - ask, Decimal(299) - bid
    strategy, ctx = prepared(direction=direction, tick=make_tick(str(bid), str(ask), time=NOW))
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize("take_profit_enabled", [True, False])
def test_rounded_zero_target_is_rejected_only_when_take_profit_is_enabled(take_profit_enabled):
    entry = entry_bars_from(
        [("149.0002", "149.0003", "149.0001", "149.0002")] * 5
        + [
            ("149.0001", "149.0002", "148.9992", "148.9995"),
            ("148.9995", "149.0002", "148.9994", "149.0001"),
        ]
    )
    strategy, ctx = prepared(
        regime_bars=flat_regime_bars(high="149.0008", low="149", close="149.0004"),
        entry_bars=entry,
        tick=make_tick("149.00009", "149.0001", time=NOW),
        atr_regime=0.0001,
        atr_entry=0.0004,
        params={"min_reward_to_risk": 0.1, "take_profit_enabled": take_profit_enabled},
    )
    signal = strategy._evaluate("USDJPY", ctx)
    assert (signal is not None) is (not take_profit_enabled)
    if signal is not None:
        assert signal.stop_distance_pips == Decimal("0.1")
        assert signal.take_profit_distance_pips is None


def test_registry_and_default_data_declarations():
    strategy = RangeEdgeReversalStrategy
    config = StrategyConfig(strategy_id=strategy.strategy_id)
    assert STRATEGIES[strategy.strategy_id] is strategy
    assert strategy.horizon is StrategyHorizon.INTRADAY
    assert strategy.warmup(config) == timedelta(days=3, hours=12, minutes=24)
    assert strategy.bar_window(config) == 200
    assert strategy.tick_window_seconds(config) == 0.0


@pytest.mark.parametrize(
    ("overrides", "regime_count", "entry_count", "atr_count"),
    [
        ({"range_lookback_bars": 15000}, 15000, 7, 15),
        ({"ema_period": 220, "range_slope_lookback": 80}, 300, 7, 15),
        ({"atr_period": 300}, 301, 7, 301),
        ({"reentry_max_bars": 400}, 26, 401, 15),
    ],
)
def test_data_declarations_cover_largest_instrument_override(
    overrides,
    regime_count,
    entry_count,
    atr_count,
):
    config = StrategyConfig(
        strategy_id="range_edge_reversal",
        instruments=["USDJPY", "EURUSD"],
        timeframes=TimeframeMap(regime="4h", entry="1h"),
        parameters=StrategyParameters(defaults=DEFAULTS, instruments={"EURUSD": overrides}),
    )
    assert RangeEdgeReversalStrategy.bar_window(config) == max(200, regime_count, entry_count)
    assert RangeEdgeReversalStrategy.warmup(config) == market_span_to_calendar(
        max(regime_count * 14400, (atr_count + entry_count) * 3600)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", list(PositionDirection))
async def test_horizon_fires_at_3600_seconds_before_normal_evaluation_and_only_once(
    monkeypatch, direction
):
    position = held(direction).model_copy(
        update={"strategy_id": "range_edge_reversal", "as_of": NOW - timedelta(seconds=3599)}
    )
    ctx = range_context([], [], None, now=NOW, position=position)
    strategy = RangeEdgeReversalStrategy()
    assert await strategy.on_event(make_event(known_at=NOW), ctx) == []
    ctx.clock.advance(seconds=1)

    def unexpected_evaluation(_symbol, _ctx):
        pytest.fail("時間切れのイベントで通常評価が呼ばれた")

    monkeypatch.setattr(strategy, "_evaluate", unexpected_evaluation)
    assert (
        await strategy.on_event(
            make_event(known_at=ctx.clock.now(), event_type="account.snapshot"), ctx
        )
        == []
    )
    signals = await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.exit_only is True
    assert signal.desired_direction is not direction
    assert signal.reason_codes == [HORIZON_EXPIRED]
    assert signal.take_profit_distance_pips is None
    assert signal.expected_horizon_seconds == 3600
    evaluated = []

    def record_evaluation(symbol, _ctx):
        evaluated.append(symbol)

    monkeypatch.setattr(strategy, "_evaluate", record_evaluation)
    assert await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx) == []
    assert evaluated == ["USDJPY"]


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [NOW, NOW.replace(hour=21, minute=30), NOW.replace(hour=23)])
@pytest.mark.parametrize("enabled", [True, False])
async def test_horizon_is_independent_of_setup_material_session_gate_and_entry_cutoff(now, enabled):
    position = held(PositionDirection.LONG).model_copy(
        update={"strategy_id": "range_edge_reversal", "as_of": now - timedelta(seconds=3600)}
    )
    ctx = range_context(
        [],
        [],
        None,
        now=now,
        position=position,
        params={"horizon_exit_enabled": enabled},
        profile=SessionProfile(sessions={}),
    )
    signals = await RangeEdgeReversalStrategy().on_event(make_event(known_at=now), ctx)
    assert len(signals) == int(enabled)
    if enabled:
        assert signals[0].reason_codes == [HORIZON_EXPIRED]


@pytest.mark.asyncio
async def test_entry_cutoff_also_stops_opposite_setup_for_a_held_position():
    now = NOW.replace(hour=21, minute=30)
    position = held(PositionDirection.LONG).model_copy(
        update={"strategy_id": "range_edge_reversal", "as_of": now - timedelta(minutes=10)}
    )
    strategy, ctx = prepared(now=now, direction=PositionDirection.SHORT, position=position)
    assert await strategy.on_event(make_event(known_at=now), ctx) == []


@pytest.mark.asyncio
async def test_real_market_and_indicators_produce_a_signal_without_features():
    regime = flat_regime_bars(high="149.52", low="149.48")
    regime[10] = regime[10].model_copy(update={"high": Decimal(150)})
    regime[20] = regime[20].model_copy(update={"low": Decimal(149)})
    entry = entry_bars_from(
        [("149.20", "149.22", "149.18", "149.20")] * 30
        + [("149.10", "149.15", "148.90", "148.95"), ("148.95", "149.08", "148.92", "149.05")]
    )
    ctx = range_context([], [], None, now=LONDON_START)
    market = InMemoryMarketData(ctx.clock)
    market.set_instrument(usdjpy_spec())
    for bar in regime + entry:
        market.add_bar(bar)
    market.add_tick(entry_tick())
    ctx.market = market
    ctx.indicators = IndicatorService(market)

    def unexpected_feature(*_args, **_kwargs):
        pytest.fail("通貨強弱・マクロ特徴量が参照された")

    ctx.features = SimpleNamespace(get=unexpected_feature)
    strategy = RangeEdgeReversalStrategy()
    assert await strategy.on_event(make_event(known_at=LONDON_START), ctx) == []
    assert strategy._ranges["USDJPY"].eligible is True
    assert ctx.indicators.atr("USDJPY", "1h", 14) * 1.5 <= 1.0
    ctx.clock.advance(hours=2)
    atr_entry = ctx.indicators.atr("USDJPY", "5m", 14)
    # 直前までの TR は 0.04、最後の 2 本は 0.30 / 0.16。
    # Wilder 更新 2 回で ATR = 0.0658163265…、損失幅 = 17.64540816… pips。
    assert atr_entry == pytest.approx(0.06581632653061224)
    signals = await strategy.on_event(make_event(known_at=NOW), ctx)
    assert len(signals) == 1
    assert signals[0].desired_direction is PositionDirection.LONG
    assert signals[0].stop_distance_pips == Decimal("17.6")
    assert signals[0].take_profit_distance_pips == Decimal("44.0")
