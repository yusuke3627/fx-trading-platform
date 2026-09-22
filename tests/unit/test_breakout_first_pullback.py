"""初回接触の消費、PIT、時計の分離と既存exit経路の検査。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import FixedClock, held, make_bar, make_event, make_tick, usdjpy_spec
from trading.config import load_config
from trading.data.market import InMemoryMarketData
from trading.domain.position import PositionDirection
from trading.indicators import IndicatorService
from trading.strategy.base import (
    HORIZON_EXPIRED,
    StrategyConfig,
    StrategyStatus,
    market_span_to_calendar,
)
from trading.strategy.intraday.breakout_first_pullback import BreakoutFirstPullbackStrategy
from trading.strategy.registry import STRATEGIES

START = datetime(2026, 1, 14, 10, tzinfo=UTC)


def context(*, short=False, offset=timedelta(0), start=START, params=None, arm=True):
    config = load_config("backtest").strategies["breakout_first_pullback"]
    values = config.parameters.model_dump()
    values["defaults"].update(params or {})
    config = StrategyConfig.model_validate({**config.model_dump(), "parameters": values})
    clock = FixedClock(start)
    market = InMemoryMarketData(clock)
    market.set_instrument(usdjpy_spec())
    for i in range(21):
        row = ("149.9", "150", "149.8", "149.9")
        if i == 20:
            row = ("149.9", "150.25", "149.8", "150.2")
        market.add_bar(bar(row, start - timedelta(hours=21 - i), short, offset, "1h"))
    for i in range(200):
        row = ("149.9", "150", "149.8", "149.9")
        if i == 199:
            row = ("149.9", "150.25", "149.8", "150.2")
        market.add_bar(bar(row, start - timedelta(minutes=5 * (200 - i)), short, offset))
    ctx = SimpleNamespace(
        config=config, clock=clock, market=market, indicators=IndicatorService(market),
        portfolio=SimpleNamespace(position=lambda *_args: None),
    )
    strategy = BreakoutFirstPullbackStrategy()
    if arm:
        assert strategy._evaluate("USDJPY", ctx) is None
        assert "USDJPY" in strategy._breakouts
    return strategy, ctx


def bar(row, start, short=False, offset=timedelta(0), timeframe="5m"):
    if short:
        row = [str(Decimal(300) - Decimal(value)) for value in (row[0], row[2], row[1], row[3])]
    minutes = 60 if timeframe == "1h" else 5
    return make_bar(*row, start=start + offset, timeframe=timeframe,
                    known_at=start + timedelta(minutes=minutes))


def touch(ctx, *, minutes=5, row=("150.2", "150.21", "149.98", "150.02"),
          bid="150.015", ask="150.025", short=False, offset=timedelta(0), at=None,
          tick_age=0, quote_lag=0, known_delay=0):
    end = at or START + timedelta(minutes=minutes)
    item = bar(row, end - timedelta(minutes=5), short, offset)
    if known_delay:
        item = item.model_copy(update={"known_at": item.known_at + timedelta(seconds=known_delay)})
    ctx.market.add_bar(item)
    if short:
        bid, ask = str(Decimal(300) - Decimal(ask)), str(Decimal(300) - Decimal(bid))
    ctx.market.add_tick(make_tick(
        bid, ask, time=end + offset - timedelta(seconds=quote_lag),
        received_at=end - timedelta(seconds=tick_age),
    ))
    ctx.clock.advance(seconds=(end - ctx.clock.now()).total_seconds())
    return item


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("offset", [timedelta(0), timedelta(hours=3)])
def test_symmetric_entry_freezes_atr_and_uses_execution_side(short, offset):
    strategy, ctx = context(short=short, offset=offset)
    frozen_atr = strategy._breakouts["USDJPY"].atr
    item = touch(ctx, short=short, offset=offset)
    signal = strategy._evaluate("USDJPY", ctx)
    assert signal is not None
    assert signal.desired_direction is (PositionDirection.SHORT if short else PositionDirection.LONG)
    tick = ctx.market.latest_tick("USDJPY")
    risk = item.high + frozen_atr / 4 - tick.bid if short else tick.ask - item.low + frozen_atr / 4
    assert signal.stop_distance_pips == round(risk / Decimal("0.01"), 1)
    assert signal.take_profit_distance_pips == signal.stop_distance_pips * 2
    assert signal.expected_horizon_seconds == 14400
    assert signal.exit_only is False
    assert strategy._breakouts["USDJPY"].atr == frozen_atr
    assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=10, short=short, offset=offset)
    assert strategy._evaluate("USDJPY", ctx) is None  # Riskが拒否して保有がなくても再発行しない


@pytest.mark.parametrize("first_only", [True, False])
@pytest.mark.parametrize("first_failure", ["close", "price", "spread", "session", "held"])
def test_first_only_ablation_has_a_real_second_chance(first_only, first_failure):
    strategy, ctx = context(params={"first_pullback_only": first_only})
    kwargs = {}
    if first_failure == "close":
        kwargs["row"] = ("150.2", "150.21", "149.98", "150")
    elif first_failure == "price":
        kwargs.update(bid="150.10", ask="150.11")
    elif first_failure == "spread":
        kwargs.update(bid="149.99", ask="150.025")
    elif first_failure == "session":
        strategy._session_permits_entry = lambda *_args: False
    elif first_failure == "held":
        ctx.portfolio.position = lambda *_args: held(PositionDirection.SHORT)
    touch(ctx, **kwargs)
    assert strategy._evaluate("USDJPY", ctx) is None
    strategy._session_permits_entry = lambda *_args: True
    ctx.portfolio.position = lambda *_args: None
    # 同じ確定足のgate回復だけでは再試行しない。
    touch_tick = make_tick("150.015", "150.025", time=ctx.clock.now())
    ctx.market.add_tick(touch_tick)
    assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=10)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is (not first_only)


@pytest.mark.parametrize("missing_first", [True, False])
def test_missing_first_or_middle_m5_permanently_invalidates(missing_first):
    strategy, ctx = context(params={"first_pullback_only": False})
    if not missing_first:
        touch(ctx, row=("150.2", "150.3", "150.1", "150.2"))
        assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=10 if missing_first else 15)
    assert strategy._evaluate("USDJPY", ctx) is None
    assert strategy._breakouts["USDJPY"].consumed
    touch(ctx, minutes=15 if missing_first else 20)
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize("delay", [0, 1])
def test_sixty_minute_deadline_includes_exact_boundary(delay):
    strategy, ctx = context()
    for minutes in range(5, 60, 5):
        touch(ctx, minutes=minutes, row=("150.2", "150.3", "150.1", "150.2"))
        assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=60)
    ctx.clock.advance(seconds=delay)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is (delay == 0)


@pytest.mark.parametrize("tick_age,quote_lag,delay", [(6, 0, 0), (0, 1, 0), (0, 0, 6)])
def test_stale_received_quote_broker_quote_or_confirmation_consumes(tick_age, quote_lag, delay):
    strategy, ctx = context()
    touch(ctx, tick_age=tick_age, quote_lag=quote_lag)
    ctx.clock.advance(seconds=delay)
    assert strategy._evaluate("USDJPY", ctx) is None
    assert strategy._breakouts["USDJPY"].consumed


def test_future_bar_and_tick_are_not_used_until_known():
    strategy, ctx = context()
    touch(ctx, known_delay=1)
    assert strategy._evaluate("USDJPY", ctx) is None
    ctx.clock.advance(seconds=1)
    assert strategy._evaluate("USDJPY", ctx) is not None


@pytest.mark.parametrize("broken", ["h1_gap", "m5_gap", "h1_future", "stale_start", "equal", "ema"])
def test_bad_breakout_history_never_arms(broken):
    strategy, ctx = context(arm=False, params={"breakout_lookback_bars": 2} if broken == "ema" else None)
    hours = ctx.market._bars[("USDJPY", "1h")]
    if broken == "h1_gap":
        hours[-3] = hours[-3].model_copy(update={"start": hours[-3].start - timedelta(minutes=1)})
    elif broken == "m5_gap":
        ctx.market._bars[("USDJPY", "5m")].pop(-3)
    elif broken == "h1_future":
        hours[-1] = hours[-1].model_copy(update={"known_at": START + timedelta(seconds=1)})
    elif broken == "stale_start":
        ctx.clock.advance(seconds=6)
    elif broken == "equal":
        hours[-1] = hours[-1].model_copy(update={"close": Decimal(150)})
    else:
        # 突破窓をEMAより短く設定した場合も、EMA方向を省略しない。
        hours[0] = hours[0].model_copy(update={
            "open": Decimal(200), "high": Decimal(200), "low": Decimal(200), "close": Decimal(200),
        })
    assert strategy._evaluate("USDJPY", ctx) is None
    assert "USDJPY" not in strategy._breakouts


def test_longer_breakout_window_does_not_change_ema_initialization(monkeypatch):
    from trading.indicators.ema import ema_series
    from trading.strategy.intraday import breakout_first_pullback

    observed = []

    def observe_ema(closes, period):
        series = ema_series(closes, period)
        observed.append(series[-2:])
        return series

    monkeypatch.setattr(breakout_first_pullback, "ema_series", observe_ema)
    for older_close in ("140", "149.9"):
        strategy, ctx = context(arm=False, params={"breakout_lookback_bars": 40})
        ctx.market._bars[("USDJPY", "1h")][:0] = [
            make_bar(
                older_close, "150", "139", older_close,
                start=START - timedelta(hours=hours_before), timeframe="1h",
            )
            for hours_before in range(41, 21, -1)
        ]
        assert strategy._evaluate("USDJPY", ctx) is None
        assert "USDJPY" in strategy._breakouts
        touch(ctx)
        assert strategy._evaluate("USDJPY", ctx) is not None

    assert observed[0] == pytest.approx([149.9, 149.9 + (150.2 - 149.9) * 2 / 21])
    assert observed[1] == pytest.approx(observed[0])


@pytest.mark.parametrize("date,end_hour", [(datetime(2026, 1, 14, tzinfo=UTC), 22),
                                          (datetime(2026, 8, 18, tzinfo=UTC), 21)])
@pytest.mark.parametrize("remaining,emits", [(14460, False), (14461, True), (3600, False)])
def test_new_york_rollover_guard_in_winter_and_summer(date, end_hour, remaining, emits):
    end = date.replace(hour=end_hour) - timedelta(seconds=remaining)
    # session時刻は実UTC、brokerラベルのH1境界はテストの既存足を使う。
    strategy, ctx = context(start=end - timedelta(minutes=5))
    touch(ctx, at=end)
    assert (strategy._evaluate("USDJPY", ctx) is not None) is emits


async def test_horizon_exit_runs_after_session_closed_without_reversal_entry():
    strategy, ctx = context()
    position = held(PositionDirection.LONG).model_copy(update={
        "strategy_id": strategy.strategy_id, "as_of": START,
    })
    ctx.portfolio.position = lambda *_args: position
    assert await strategy.on_event(make_event(known_at=START), ctx) == []
    strategy._session_permits_entry = lambda *_args: False
    ctx.clock.advance(hours=4)
    signals = await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx)
    assert len(signals) == 1
    assert signals[0].exit_only and signals[0].reason_codes == [HORIZON_EXPIRED]
    assert await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx) == []
    ctx.portfolio.position = lambda *_args: None
    assert await strategy.on_event(make_event(known_at=ctx.clock.now()), ctx) == []


@pytest.mark.parametrize("environment", ["backtest", "demo", "shadow", "micro_live", "production"])
def test_registration_and_every_overlay_keep_research_disabled(environment):
    config = load_config(environment).strategies["breakout_first_pullback"]
    assert STRATEGIES[config.strategy_id] is BreakoutFirstPullbackStrategy
    assert not config.enabled and config.status is StrategyStatus.RESEARCH_ONLY
    assert config.timeframes.all() == ("5m", "1h")
    params = config.params_for("USDJPY")
    assert params.param("first_pullback_only", False) is True
    assert params.param("expected_horizon_seconds", 0) == 14400
    assert params.param("rollover_entry_buffer_seconds", 0) == 60
    assert params.param("take_profit_r", 0) == "2"
    assert BreakoutFirstPullbackStrategy.bar_window(config) == 200
    assert BreakoutFirstPullbackStrategy.warmup(config) == market_span_to_calendar(21 * 3600)


@pytest.mark.parametrize("params,window,span", [
    ({"ema_period": 300}, 301, 301 * 3600),
    ({"breakout_lookback_bars": 400}, 401, 401 * 3600),
    ({"atr_period": 300}, 301, 301 * 300),
    ({"pullback_wait_seconds": 90000}, 301, 301 * 300),
])
def test_retention_and_warmup_follow_largest_instrument_override(params, window, span):
    config = StrategyConfig(
        strategy_id="breakout_first_pullback", instruments=["USDJPY", "EURUSD"],
        timeframes={"regime": "1h", "entry": "5m"},
        parameters={"instruments": {"EURUSD": params}},
    )
    assert BreakoutFirstPullbackStrategy.bar_window(config) == window
    assert BreakoutFirstPullbackStrategy.warmup(config) == market_span_to_calendar(span)


def test_same_h1_revision_cannot_rearm_a_consumed_breakout():
    strategy, ctx = context()
    touch(ctx)
    assert strategy._evaluate("USDJPY", ctx) is not None
    hours = ctx.market._bars[("USDJPY", "1h")]
    hours[-1] = hours[-1].model_copy(update={"close": Decimal("150.3"), "known_at": ctx.clock.now()})
    touch(ctx, minutes=10)
    assert strategy._evaluate("USDJPY", ctx) is None


def test_old_setup_gets_last_m5_before_next_h1_replaces_it():
    strategy, ctx = context()
    for minutes in range(5, 60, 5):
        touch(ctx, minutes=minutes, row=("150.2", "150.3", "150.1", "150.2"))
        assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=60)
    ctx.market.add_bar(make_bar("150.2", "150.3", "149.98", "150.02", start=START,
                                timeframe="1h"))
    assert strategy._evaluate("USDJPY", ctx) is not None


def test_entry_at_hour_boundary_still_observes_new_breakout_immediately():
    strategy, ctx = context(params={"retest_tolerance_atr": "2"})
    old_setup = strategy._breakouts["USDJPY"]
    for minutes in range(5, 60, 5):
        touch(ctx, minutes=minutes, row=("150.9", "151", "150.9", "150.9"))
        assert strategy._evaluate("USDJPY", ctx) is None
    touch(ctx, minutes=60, row=("150.9", "151", "150.2", "150.3"),
          bid="150.295", ask="150.305")
    ctx.market.add_bar(make_bar("150.2", "151", "150.2", "150.3",
                                start=START, timeframe="1h"))

    assert strategy._evaluate("USDJPY", ctx) is not None
    assert old_setup.consumed
    current = strategy._breakouts["USDJPY"]
    assert current is not old_setup
    assert current.level == Decimal("150.25")
    assert current.next_start == START + timedelta(hours=1)
    assert current.expires_at == START + timedelta(hours=2)
