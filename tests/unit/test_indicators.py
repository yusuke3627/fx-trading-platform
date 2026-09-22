from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from tests.support import T0, FixedClock, make_bar, make_tick
from trading.backtest.market import ReplayMarketData
from trading.data.market import InMemoryMarketData, MarketDataService
from trading.indicators import IndicatorService
from trading.indicators.atr import atr
from trading.indicators.ema import ema, ema_series
from trading.indicators.market_structure import (
    detect_failed_breakout,
    is_lower_high,
    rolling_high,
    rolling_low,
)
from trading.indicators.momentum import rate_of_change, tick_momentum
from trading.indicators.volatility import realized_volatility
from trading.indicators.vwap import vwap


def bars_from_closes(closes, spread=0.5):
    return [
        make_bar(
            str(c),
            str(c + spread),
            str(c - spread),
            str(c),
            start=T0 + timedelta(minutes=i),
        )
        for i, c in enumerate(closes)
    ]


def test_ema_converges_on_linear_series():
    values = [float(v) for v in range(1, 11)]
    series = ema_series(values, 3)
    assert series[0] == 2.0  # SMA seed of 1,2,3
    assert ema(values, 3) == 9.0


def test_ema_insufficient_data_returns_none():
    assert ema([1.0, 2.0], 5) is None


def test_atr_constant_range():
    bars = bars_from_closes([100.0] * 10)
    assert atr(bars, period=3) == 1.0  # high-low = 1 everywhere


def test_atr_insufficient_data_returns_none():
    assert atr(bars_from_closes([100.0] * 3), period=14) is None


def test_vwap_weights_by_volume():
    b1 = make_bar("100", "100", "100", "100", tick_volume=1)
    b2 = make_bar("102", "102", "102", "102", tick_volume=3)
    assert vwap([b1, b2]) == 101.5


def test_vwap_zero_volume_falls_back_to_equal_weight():
    b1 = make_bar("100", "100", "100", "100")
    b2 = make_bar("102", "102", "102", "102")
    assert vwap([b1, b2]) == 101.0


def test_rolling_extremes():
    bars = bars_from_closes([100, 101, 105, 103, 102])
    assert rolling_high(bars, 5) == 105.5
    assert rolling_low(bars, 5) == 99.5


def test_failed_breakout_up_detected():
    level = 101.0
    bars = [
        make_bar("100.0", "100.5", "99.5", "100.2"),
        make_bar("100.2", "101.5", "100.0", "100.8"),  # traded above, closed below
        make_bar("100.8", "100.9", "100.3", "100.5"),  # failed to reclaim
    ]
    assert detect_failed_breakout(bars, level, side="UP") is True


def test_no_failed_breakout_when_level_holds():
    level = 101.0
    bars = [
        make_bar("100.0", "100.5", "99.5", "100.2"),
        make_bar("100.2", "101.5", "100.0", "101.2"),  # closed above the level
        make_bar("101.2", "101.8", "101.0", "101.5"),
    ]
    assert detect_failed_breakout(bars, level, side="UP") is False


def test_lower_high_detection():
    closes = [100, 101, 105, 101, 100, 101, 103, 101, 100]
    assert is_lower_high(bars_from_closes(closes), 2, 2) is True


def test_rate_of_change():
    assert rate_of_change([100.0, 110.0], 1) == 0.1
    assert rate_of_change([100.0], 1) is None


def test_realized_volatility_zero_for_constant_prices():
    assert realized_volatility([100.0] * 20, window=10) == 0.0


def test_tick_momentum_sign():
    ticks = [
        make_tick("100.000", "100.004", time=T0 + timedelta(seconds=i)) for i in range(5)
    ] + [make_tick("100.100", "100.104", time=T0 + timedelta(seconds=5))]
    momentum = tick_momentum(ticks, window_seconds=10)
    assert momentum is not None and momentum > 0


@pytest.mark.parametrize("market_type", [InMemoryMarketData, ReplayMarketData])
def test_reused_tick_window_keeps_boundaries_ties_late_ticks_and_decimal_subtraction(market_type):
    market = market_type()
    ticks = [
        make_tick("150.001", "150.003", time=T0),
        make_tick("150.004", "150.006", time=T0 + timedelta(seconds=10)),
        make_tick("150.005", "150.007", time=T0 + timedelta(seconds=10)),
        make_tick("150.002", "150.004", time=T0 + timedelta(seconds=5)),
        make_tick("140.000", "140.002", time=T0 - timedelta(microseconds=1)),
    ]
    for tick in ticks:
        market.add_tick(tick)
    service = IndicatorService(market)
    wide = market.ticks("USDJPY", 60)
    expected = float(ticks[2].mid - ticks[0].mid)
    assert expected != float(ticks[2].mid) - float(ticks[0].mid)
    assert service.tick_momentum("USDJPY", 10) == expected
    with patch.object(market, "ticks", side_effect=AssertionError("窓を再取得した")):
        assert service.tick_momentum("USDJPY", 10, ticks=wide) == expected
        assert service.tick_momentum("USDJPY", 10, ticks=[]) is None
        assert service.tick_momentum("USDJPY", 10, ticks=[ticks[0]]) is None


def test_service_read_follows_a_period_beyond_the_default_window():
    # A configured atr_period past DEFAULT_BAR_COUNT must widen the read;
    # a capped read would leave the indicator permanently None even with
    # plenty of history stored.
    from trading.data.market import InMemoryMarketData
    from trading.indicators import DEFAULT_BAR_COUNT, IndicatorService

    market = InMemoryMarketData()
    period = DEFAULT_BAR_COUNT + 50
    for bar in bars_from_closes([100.0 + 0.01 * i for i in range(period + 1)]):
        market.add_bar(bar)
    service = IndicatorService(market)

    assert service.atr("USDJPY", "1m", period) is not None
    assert service.ema("USDJPY", "1m", period) is not None


def test_atr_reuses_unchanged_bars_and_recomputes_after_a_correction():
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    market = Mock(spec=MarketDataService)
    market.bars.side_effect = lambda symbol, timeframe, count: bars[-count:]
    service = IndicatorService(market)

    with patch("trading.indicators._atr", wraps=atr) as calculate:
        expected = atr(bars, 3)
        assert service.atr("USDJPY", "1m", 3) == expected
        assert service.atr("USDJPY", "1m", 3) == expected
        assert calculate.call_count == 1

        # 最新足が同じでも、過去足の訂正は結果に反映する。
        bars[1] = bars[1].model_copy(update={"high": Decimal(110)})
        corrected = atr(bars, 3)
        assert corrected != expected
        assert service.atr("USDJPY", "1m", 3) == corrected
        assert calculate.call_count == 2


def test_atr_cache_follows_visible_history_and_can_return_to_insufficient_data():
    clock = FixedClock()
    market = InMemoryMarketData(clock)
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    for bar in bars:
        market.add_bar(bar)
    service = IndicatorService(market)

    assert service.atr("USDJPY", "1m", 3) is None
    clock.advance(minutes=4)
    assert service.atr("USDJPY", "1m", 3) == atr(bars[:4], 3)
    clock.advance(minutes=1)
    assert service.atr("USDJPY", "1m", 3) == atr(bars, 3)
    clock.advance(minutes=-5)
    assert service.atr("USDJPY", "1m", 3) is None


def test_atr_cache_separates_symbols_timeframes_and_periods():
    market = InMemoryMarketData()
    inputs = [
        ("USDJPY", "1m", [100.0, 101.0, 103.0, 102.0, 104.0]),
        ("USDJPY", "5m", [100.0, 103.0, 101.0, 110.0, 105.0]),
        ("EURUSD", "1m", [1.0, 1.01, 1.03, 1.02, 1.04]),
    ]
    windows = {}
    for symbol, timeframe, closes in inputs:
        bars = [
            bar.model_copy(update={"symbol": symbol, "timeframe": timeframe})
            for bar in bars_from_closes(closes)
        ]
        windows[symbol, timeframe] = bars
        for bar in bars:
            market.add_bar(bar)
    service = IndicatorService(market)
    for _ in range(2):
        for (symbol, timeframe), bars in windows.items():
            for period in (2, 3):
                assert service.atr(symbol, timeframe, period) == atr(bars, period)


def test_ema_reuses_unchanged_bars_and_recomputes_after_a_correction():
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    market = Mock(spec=MarketDataService)
    market.bars.side_effect = lambda symbol, timeframe, count: bars[-count:]
    service = IndicatorService(market)

    def closes_of(window):
        return [float(b.close) for b in window]

    with patch("trading.indicators._ema", wraps=ema) as calculate:
        expected = ema(closes_of(bars), 3)
        assert service.ema("USDJPY", "1m", 3) == expected
        assert service.ema("USDJPY", "1m", 3) == expected
        assert calculate.call_count == 1

        # 終値が変わらない訂正でも入力窓は変わるので、そのまま返さず計算し直す。
        bars[1] = bars[1].model_copy(update={"high": Decimal(110)})
        assert service.ema("USDJPY", "1m", 3) == expected
        assert calculate.call_count == 2

        # 終値の訂正は結果に反映する。
        bars[1] = bars[1].model_copy(update={"close": Decimal(90)})
        corrected = ema(closes_of(bars), 3)
        assert corrected != expected
        assert service.ema("USDJPY", "1m", 3) == corrected
        assert calculate.call_count == 3


def test_ema_cache_follows_visible_history_and_can_return_to_insufficient_data():
    clock = FixedClock()
    market = InMemoryMarketData(clock)
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    for bar in bars:
        market.add_bar(bar)
    service = IndicatorService(market)

    def closes_of(window):
        return [float(b.close) for b in window]

    assert service.ema("USDJPY", "1m", 4) is None
    clock.advance(minutes=4)
    assert service.ema("USDJPY", "1m", 4) == ema(closes_of(bars[:4]), 4)
    clock.advance(minutes=1)
    assert service.ema("USDJPY", "1m", 4) == ema(closes_of(bars), 4)
    clock.advance(minutes=-5)
    assert service.ema("USDJPY", "1m", 4) is None


def test_ema_cache_separates_symbols_timeframes_and_periods():
    market = InMemoryMarketData()
    inputs = [
        ("USDJPY", "1m", [100.0, 101.0, 103.0, 102.0, 104.0]),
        ("USDJPY", "5m", [100.0, 103.0, 101.0, 110.0, 105.0]),
        ("EURUSD", "1m", [1.0, 1.01, 1.03, 1.02, 1.04]),
    ]
    windows = {}
    for symbol, timeframe, closes in inputs:
        bars = [
            bar.model_copy(update={"symbol": symbol, "timeframe": timeframe})
            for bar in bars_from_closes(closes)
        ]
        windows[symbol, timeframe] = bars
        for bar in bars:
            market.add_bar(bar)
    service = IndicatorService(market)
    for _ in range(2):
        for (symbol, timeframe), bars in windows.items():
            for period in (2, 3):
                closes = [float(b.close) for b in bars]
                assert service.ema(symbol, timeframe, period) == ema(closes, period)


def test_atr_and_ema_caches_do_not_share_entries():
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    market = Mock(spec=MarketDataService)
    market.bars.side_effect = lambda symbol, timeframe, count: bars[-count:]
    service = IndicatorService(market)

    assert service.atr("USDJPY", "1m", 3) == atr(bars, 3)
    assert service.ema("USDJPY", "1m", 3) == ema([float(b.close) for b in bars], 3)
    assert service.atr("USDJPY", "1m", 3) != service.ema("USDJPY", "1m", 3)


@pytest.mark.parametrize(
    "method,target,compute,field,index,value",
    [
        ("momentum", "rate_of_change", rate_of_change, "close", 1, 90),
        ("realized_volatility", "_rvol", realized_volatility, "close", 1, 90),
        ("recent_high", "ms.rolling_high", rolling_high, "high", 2, 110),
        ("recent_low", "ms.rolling_low", rolling_low, "low", 2, 90),
    ],
)
def test_window_cache_reuses_bars_and_recomputes_after_a_correction(
    method, target, compute, field, index, value,
):
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    market = Mock(spec=MarketDataService)
    market.bars.side_effect = lambda symbol, timeframe, count: bars[-count:]
    service = IndicatorService(market, bar_count=5)
    indicator = getattr(service, method)

    def expected_value():
        inputs = [float(b.close) for b in bars] if field == "close" else bars
        return compute(inputs, 3)

    with patch(f"trading.indicators.{target}", wraps=compute) as calculate:
        expected = expected_value()
        assert expected is not None
        assert indicator("USDJPY", "1m", 3) == expected
        assert indicator("USDJPY", "1m", 3) == expected
        assert calculate.call_count == 1

        # 最新足を変えずに過去足を訂正しても、再計算する。
        bars[index] = bars[index].model_copy(update={field: Decimal(value)})
        corrected = expected_value()
        assert corrected != expected
        assert indicator("USDJPY", "1m", 3) == corrected
        assert indicator("USDJPY", "1m", 3) == corrected
        assert calculate.call_count == 2
        assert market.bars.call_count == 4
        market.bars.assert_called_with("USDJPY", "1m", 5)


def test_all_six_indicator_caches_do_not_share_entries():
    bars = bars_from_closes([100.0, 101.0, 103.0, 102.0, 104.0])
    closes = [float(b.close) for b in bars]
    market = Mock(spec=MarketDataService)
    market.bars.side_effect = lambda symbol, timeframe, count: bars[-count:]
    service = IndicatorService(market)
    cases = [
        ("atr", "_atr", atr, bars),
        ("ema", "_ema", ema, closes),
        ("momentum", "rate_of_change", rate_of_change, closes),
        ("realized_volatility", "_rvol", realized_volatility, closes),
        ("recent_high", "ms.rolling_high", rolling_high, bars),
        ("recent_low", "ms.rolling_low", rolling_low, bars),
    ]
    expected = {method: compute(inputs, 3) for method, _, compute, inputs in cases}
    assert None not in expected.values()
    assert expected["momentum"] != expected["realized_volatility"]
    assert expected["recent_high"] != expected["recent_low"]

    with ExitStack() as stack:
        calculations = [
            stack.enter_context(patch(f"trading.indicators.{target}", wraps=compute))
            for _, target, compute, _ in cases
        ]
        for _ in range(2):
            for method, _, _, _ in cases:
                assert getattr(service, method)("USDJPY", "1m", 3) == expected[method]
        assert [calculate.call_count for calculate in calculations] == [1] * 6
