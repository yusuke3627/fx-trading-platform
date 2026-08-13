from datetime import timedelta

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

from tests.support import T0, make_bar, make_tick


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
