from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from tests.support import T0, FixedClock, make_bar, make_tick
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
