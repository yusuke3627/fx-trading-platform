"""Bar delivery through the backtest engine.

A strategy reads bars from its context, so the acceptance criteria are that
the bars exist at all, that none of them is visible before it closed, and that
the same dataset yields the same series every run.
"""
from datetime import UTC, datetime, timedelta

from tests.support import usdjpy_spec
from trading.backtest.costs import STRESS_SCENARIOS
from trading.backtest.data import synthetic_ticks
from trading.backtest.engine import BacktestEngine
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar
from trading.domain.signal import StrategySignal
from trading.risk.engine import RiskConfig
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    TimeframeMap,
)

DATASET_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


class BarObserver(Strategy):
    """Records what the bar feed looked like on every tick. It never signals:
    these tests are about what a strategy can SEE, not what it does."""

    strategy_id = "bar_observer"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY

    def __init__(self, timeframe: str) -> None:
        self._timeframe = timeframe
        self.observations: list[tuple[datetime, tuple[Bar, ...]]] = []

    async def on_event(
        self, event: EventEnvelope, context: StrategyContext
    ) -> list[StrategySignal]:
        symbol = context.config.instruments[0]
        self.observations.append(
            (
                context.clock.now(),
                tuple(context.market.bars(symbol, self._timeframe, 50)),
            )
        )
        return []


def run_observer(
    *, count: int, observed: str, configured: dict[str, str] | None = None
) -> BarObserver:
    """Replays `count` one-second ticks and returns the strategy instance."""
    spec = usdjpy_spec()
    built: list[BarObserver] = []

    def factory() -> BarObserver:
        strategy = BarObserver(observed)
        built.append(strategy)
        return strategy

    engine = BacktestEngine(
        risk_config=RiskConfig(),
        spec=spec,
        costs=STRESS_SCENARIOS["normal"],
        seed=7,
        strategy_factory=factory,
        strategy_config=StrategyConfig(
            strategy_id=BarObserver.strategy_id,
            enabled=True,
            instruments=[spec.symbol],
            timeframes=TimeframeMap(
                **({"entry": observed} if configured is None else configured)
            ),
        ),
    )
    engine.run(synthetic_ticks(spec=spec, start=DATASET_START, count=count, seed=7))
    return built[-1]


def test_strategy_reads_bars_folded_from_the_tick_stream():
    observer = run_observer(count=600, observed="1m")
    final = observer.observations[-1][1]
    assert final, "the strategy received no bars at all"
    # 600 one-second ticks span ten minutes, and the tenth bucket never closes
    # (no tick arrives after it), so nine bars have printed.
    assert len(final) == 9
    assert [b.start for b in final] == [
        DATASET_START + timedelta(minutes=i) for i in range(9)
    ]
    assert all(b.tick_volume == 60 for b in final)


def test_a_bar_becomes_visible_exactly_at_its_close():
    observer = run_observer(count=180, observed="1m")
    first_seen: dict[datetime, datetime] = {}
    for now, bars in observer.observations:
        for bar in bars:
            first_seen.setdefault(bar.start, now)

    assert first_seen, "no bar was ever observed"
    # The candle covering [00:00, 00:01) is unknowable until 00:01.
    assert first_seen[DATASET_START] == DATASET_START + timedelta(minutes=1)
    assert first_seen[DATASET_START + timedelta(minutes=1)] == DATASET_START + timedelta(
        minutes=2
    )


def test_no_observed_bar_is_known_after_the_replay_clock():
    observer = run_observer(count=600, observed="1m")
    assert any(bars for _, bars in observer.observations)
    for now, bars in observer.observations:
        for bar in bars:
            # known_at, not close_time: the candle's end sits on the broker's
            # clock and the replay runs on ours (ADR-005).
            assert bar.known_at <= now


def test_several_configured_timeframes_are_all_available():
    # A strategy declaring regime and entry roles must be able to read both
    # series out of the one tick stream.
    configured = {"regime": "5m", "entry": "1m"}
    fives = run_observer(count=900, observed="5m", configured=configured).observations[-1][1]
    ones = run_observer(count=900, observed="1m", configured=configured).observations[-1][1]

    assert [b.start for b in fives] == [
        DATASET_START + timedelta(minutes=5 * i) for i in range(2)
    ]
    assert all(b.tick_volume == 300 for b in fives)
    # Same ticks, finer grid: fourteen closed minutes over the same span.
    assert len(ones) == 14
    assert all(b.tick_volume == 60 for b in ones)


def test_bar_feed_is_deterministic_across_runs():
    first = run_observer(count=600, observed="1m")
    second = run_observer(count=600, observed="1m")
    assert first.observations[-1][1], "a vacuous comparison of two empty feeds"
    assert first.observations == second.observations


def test_a_strategy_without_configured_timeframes_receives_no_bars():
    # Deriving the builders from configuration is what keeps the existing
    # vertical-slice runs (whose probe declares no timeframes) unchanged.
    observer = run_observer(count=600, observed="1m", configured={})
    assert observer.observations
    assert all(bars == () for _, bars in observer.observations)
