"""初期履歴だけを変え、同じ評価時刻の実指標と実戦略の判断を照合する。

合成の確定足を使う検査であり、実市場の欠測、約定や収益性は評価しない。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import FixedClock, make_bar, make_event, make_tick, usdjpy_spec
from trading.data.market import InMemoryMarketData
from trading.domain.market import TIMEFRAME_SECONDS, Bar, Tick
from trading.indicators import IndicatorService
from trading.intelligence import features as f
from trading.intelligence.currency import CurrencyStateStore
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RuleBasedCurrencyRegimeService, RuleBasedRegimeService
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.strategy.base import StrategyConfig, StrategyContext, market_span_to_calendar
from trading.strategy.registry import STRATEGIES

START = datetime(2026, 8, 18, 8, tzinfo=UTC)
TIMEFRAMES = {
    "failed_spike_reversal": {"entry": "1m"},
    "post_event_failed_breakout": {"setup": "15m", "entry": "5m"},
    "range_edge_reversal": {"regime": "1h", "entry": "5m"},
    "monetary_policy_convergence": {"trend": "1d", "trigger": "4h"},
}


def history(timeframe: str, count: int, *, swing_rebound: bool = False) -> list[Bar]:
    """週末を除いた履歴。古い値の初期化誤差が残る系列も選べる。"""
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    end = START
    starts = []
    while len(starts) < count:
        start = end - step
        if start.weekday() < 5:
            starts.append(start)
        end = start
    bars = []
    for i, start in enumerate(reversed(starts)):
        close = Decimal("149.5")
        if timeframe == "1d":
            close = Decimal(149) + Decimal(i) / count
            if swing_rebound:
                close = (
                    Decimal(160) if i < count - 60
                    else Decimal(149) + Decimal(i - count + 60) / 59
                )
        bars.append(make_bar(
            str(close), str(close + Decimal("0.1")), str(close - Decimal("0.1")),
            str(close), start=start, timeframe=timeframe,
        ))
    return bars


def scenario(strategy_id: str, *, swing_rebound: bool = False):
    params = {}
    if strategy_id == "post_event_failed_breakout":
        params = {"resistance_lookback": 3}
    elif strategy_id == "range_edge_reversal":
        params = {"range_width_min_atr": 0.5}
    config = StrategyConfig(
        strategy_id=strategy_id, instruments=["USDJPY"],
        timeframes=TIMEFRAMES[strategy_id], parameters={"defaults": params},
    )
    long_seconds = STRATEGIES[strategy_id].warmup(config).total_seconds() * 2
    series = {
        tf: history(tf, max(400, int(long_seconds / TIMEFRAME_SECONDS[tf]) + 200),
                    swing_rebound=swing_rebound)
        for tf in config.timeframes.all()
    }
    ticks = [make_tick("149.50", "149.51", time=START)]
    if strategy_id == "failed_spike_reversal":
        prices = ["149.5"] * 8 + ["150.5"] + ["150.0"] * 5
        prices += ["149.7", "149.6", "149.5", "149.4", "149.3"]
        ticks = [
            make_tick(price, str(Decimal(price) + Decimal("0.01")),
                      time=START - timedelta(seconds=(len(prices) - 1 - i) * 10))
            for i, price in enumerate(prices)
        ]
    elif strategy_id == "post_event_failed_breakout":
        for bar in series["15m"][-4:]:
            index = series["15m"].index(bar)
            series["15m"][index] = bar.model_copy(update={"high": Decimal("150.00")})
        rows = [
            ("149.70", "149.90", "149.60", "149.80"),
            ("149.80", "150.10", "149.70", "149.90"),
            ("149.90", "149.95", "149.60", "149.85"),
        ]
        series["5m"][-3:] = [
            make_bar(*row, start=START - timedelta(minutes=5 * (3 - i)), timeframe="5m")
            for i, row in enumerate(rows)
        ]
    elif strategy_id == "range_edge_reversal":
        series["1h"] = [
            bar.model_copy(update={"high": Decimal(150), "low": Decimal(149)})
            for bar in series["1h"]
        ]
        series["5m"].extend([
            make_bar("149.1", "149.15", "148.9", "148.95", start=START, timeframe="5m"),
            make_bar("148.95", "149.08", "148.92", "149.05",
                     start=START + timedelta(minutes=5), timeframe="5m"),
        ])
        ticks.append(make_tick("149.05", "149.06", time=START + timedelta(minutes=10)))
    return config, [bar for bars in series.values() for bar in bars], ticks


async def decisions(
    config: StrategyConfig, bars: list[Bar], ticks: list[Tick], warmup: timedelta
) -> list[dict]:
    clock = FixedClock(START)
    market = InMemoryMarketData(clock)
    market.set_instrument(usdjpy_spec())
    for bar in bars:
        if bar.start >= START - warmup:
            market.add_bar(bar)
    for tick in ticks:
        if tick.time >= START - warmup:
            market.add_tick(tick)
    store = InMemoryFeatureStore()
    for name, value in {
        f.FED_POLICY_SHIFT_SCORE: 1.0, f.BOJ_POLICY_SHIFT_SCORE: -0.5,
        f.US2Y_CHANGE_5D: -0.1, f.INTERVENTION_RISK: 0.1,
    }.items():
        store.set(name, value)
    indicators = IndicatorService(market)
    context = StrategyContext(
        clock=clock, market=market, indicators=indicators, features=store,
        regime=RuleBasedRegimeService(store), currency_states=CurrencyStateStore(),
        currency_regime=RuleBasedCurrencyRegimeService(store),
        portfolio=VirtualPositionLedger(clock), config=config,
    )
    strategy = STRATEGIES[config.strategy_id]()
    trace = []
    for at in (START, START + timedelta(minutes=10), START + timedelta(minutes=10, seconds=1)):
        clock.advance(seconds=(at - clock.now()).total_seconds())
        signals = await strategy.on_event(make_event(known_at=at), context)
        trace.append({
            "at": at,
            "indicators": {
                tf: {
                    "atr14": indicators.atr("USDJPY", tf, 14),
                    "ema20": indicators.ema("USDJPY", tf, 20),
                    "ema50": indicators.ema("USDJPY", tf, 50),
                }
                for tf in config.timeframes.all()
            },
            "signals": [signal.model_dump(exclude={"signal_id"}) for signal in signals],
        })
    return trace


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
async def test_declared_warmup_matches_longer_history(strategy_id):
    config, bars, ticks = scenario(strategy_id)
    warmup = STRATEGIES[strategy_id].warmup(config)
    assert sum(bar.start >= START - warmup for bar in bars) < sum(
        bar.start >= START - warmup * 2 for bar in bars
    ), "履歴の切り出しが同じでは検査にならない"
    declared = await decisions(config, bars, ticks, warmup)
    longer = await decisions(config, bars, ticks, warmup * 2)

    assert declared == longer
    assert any(row["signals"] for row in longer), "空の判断列同士だけでは検査にならない"
    assert all(
        value is not None
        for row in longer for values in row["indicators"].values() for value in values.values()
    )


async def test_short_history_changes_swing_indicator_and_entry_decision():
    config, bars, ticks = scenario("monetary_policy_convergence", swing_rebound=True)
    full = await decisions(config, bars, ticks, STRATEGIES[config.strategy_id].warmup(config))
    # EMAの期間50だけを初期化に必要な本数とした旧条件を対照にする。
    short = await decisions(config, bars, ticks, market_span_to_calendar(50 * 86400))

    assert short[0]["indicators"]["1d"] != full[0]["indicators"]["1d"]
    assert short[0]["signals"]
    assert full[0]["signals"] == []
    with pytest.raises(AssertionError):
        assert short == full


@pytest.mark.parametrize(
    ("timeframes", "params", "days"),
    [
        ({"trend": "1d", "trigger": "4h"}, {}, 200),
        ({"trend": "1h", "trigger": "1d"}, {}, 200),
        ({"trend": "1h", "trigger": "1d"}, {"atr_period": 300}, 301),
        ({"trend": "1h", "trigger": "1d"}, {"support_lookback": 400}, 410),
    ],
)
def test_swing_warmup_follows_largest_read_window(timeframes, params, days):
    config = StrategyConfig(
        strategy_id="monetary_policy_convergence", instruments=["USDJPY", "EURUSD"],
        timeframes=timeframes,
        parameters={"instruments": {"EURUSD": params}},
    )
    assert STRATEGIES[config.strategy_id].warmup(config) == market_span_to_calendar(days * 86400)
