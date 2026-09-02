"""Shadow runner: one instant per evaluation, decisions but no orders."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import (
    T0,
    FakeAccountSnapshotRepository,
    FakeBarRepository,
    FakeDecisionRepository,
    FakeTickRepository,
    FixedClock,
    at,
    eurusd_spec,
    make_snapshot,
    make_tick,
    usdjpy_spec,
)
from trading.data.market.stored import StoredMarketData
from trading.domain.account import AccountMode
from trading.domain.money import Currency
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.execution.mt5.adapter import MT5ConnectionError
from trading.execution.mt5.mapper import account_key_from_info
from trading.indicators import IndicatorService
from trading.intelligence.currency import CurrencyStateStore
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import (
    RuleBasedCurrencyRegimeService,
    RuleBasedRegimeService,
)
from trading.live.clock import CycleClock
from trading.live.shadow import (
    ShadowInstrument,
    ShadowRunner,
    broker_identity,
    describe,
)
from trading.portfolio.exposure import CurrencyExposureService
from trading.portfolio.manager import PortfolioManager
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import RiskConfig, RiskEngine
from trading.risk.event_risk import EventRiskCalendar, EventRiskWindow
from trading.runner import StrategyBinding, StrategyRunner
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    StrategyStatus,
)

ACCOUNT = "Test-Broker Demo:10000001"
# The span these tests operate in, declared as recorded.
COVERED = (at(days=-1), at(days=60))
# A calendar that knows the schedule and finds nothing near — as opposed to
# None, which means the schedule is not known at all.
NO_WINDOWS = EventRiskCalendar([], COVERED)


class SignallingStrategy(Strategy):
    """Emits one signal per event, so the path after the strategy is what the
    test is exercising rather than a strategy's own conditions."""

    strategy_id = "test_signaller"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.SCALP

    async def on_event(self, event, context):
        return [
            self.make_signal(
                context,
                symbol="USDJPY",
                direction=PositionDirection.SHORT,
                conviction=0.7,
                stop_distance_pips=Decimal(5),
                expected_horizon_seconds=300,
                reason_codes=["TEST"],
            )
        ]


class SilentStrategy(SignallingStrategy):
    strategy_id = "test_silent"

    async def on_event(self, event, context):
        return []


class MultiSymbolSignallingStrategy(SignallingStrategy):
    async def on_event(self, event, context):
        return [
            self.make_signal(
                context,
                symbol=symbol,
                direction=PositionDirection.SHORT,
                conviction=0.7,
                stop_distance_pips=Decimal(5),
                expected_horizon_seconds=300,
                reason_codes=["TEST"],
            )
            for symbol in context.config.instruments
        ]


class OutOfScopeSignallingStrategy(SignallingStrategy):
    async def on_event(self, event, context):
        return [
            self.make_signal(
                context,
                symbol="EURUSD",
                direction=PositionDirection.SHORT,
                conviction=0.7,
                stop_distance_pips=Decimal(5),
                expected_horizon_seconds=300,
                reason_codes=["TEST"],
            )
        ]


def build(
    *,
    ticks=(),
    snapshots=(),
    strategy=None,
    enabled=True,
    trading_enabled=False,
    source_clock=None,
    decisions=None,
    event_risk=NO_WINDOWS,
    event_mode_default=EventRiskMode.NORMAL,
    features=None,
    instruments=None,
):
    instruments = (
        list(instruments)
        if instruments is not None
        else [ShadowInstrument(usdjpy_spec(), trading_enabled=True)]
    )
    specs = [instrument.spec for instrument in instruments]
    clock = CycleClock(source_clock or FixedClock(at(minutes=1)))
    market = StoredMarketData(
        FakeTickRepository(ticks),
        FakeBarRepository(),
        clock,
        {spec.symbol: spec for spec in specs},
    )
    snapshot_store = FakeAccountSnapshotRepository()
    for snapshot in snapshots:
        snapshot_store.insert(ACCOUNT, snapshot)
    ledger = VirtualPositionLedger(clock)
    binding = StrategyBinding(
        strategy=(strategy or SignallingStrategy)(),
        context=_context(clock, market, ledger, enabled, specs),
    )
    risk_config = RiskConfig(
        trading_enabled=trading_enabled, event_mode_default=event_mode_default
    )
    return ShadowRunner(
        runner=StrategyRunner([binding]),
        portfolio=PortfolioManager(
            ledger, clock, MarketQuoteConversionService(market, specs)
        ),
        ledger=ledger,
        risk=RiskEngine(risk_config, clock, MarketQuoteConversionService(market, specs)),
        risk_config=risk_config,
        market=market,
        snapshots=snapshot_store,
        decisions=decisions or FakeDecisionRepository(),
        event_risk=event_risk,
        clock=clock,
        account_id=ACCOUNT,
        account_mode=AccountMode.HEDGING,
        instruments=instruments,
        exposure=CurrencyExposureService(
            MarketQuoteConversionService(market, specs)
        ),
        features=features,
    )


def _context(clock, market, ledger, enabled, specs):
    features = InMemoryFeatureStore()
    return StrategyContext(
        clock=clock,
        market=market,
        indicators=IndicatorService(market),
        features=features,
        regime=RuleBasedRegimeService(features),
        currency_states=CurrencyStateStore(),
        currency_regime=RuleBasedCurrencyRegimeService(features),
        portfolio=ledger,
        config=StrategyConfig(
            strategy_id="test_signaller",
            enabled=enabled,
            status=StrategyStatus.SHADOW,
            instruments=[spec.symbol for spec in specs],
        ),
    )


def quote_and_account():
    # Everything as of the clock build() runs on: a quote older than
    # quote_max_age_seconds stops the cycle before anything is evaluated.
    return {
        "ticks": [make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=1))],
        "snapshots": [make_snapshot("1000000", observed_at=at(minutes=1))],
    }


def test_a_signal_becomes_an_intent_and_a_graded_decision():
    runner = build(**quote_and_account())

    (result,) = runner.evaluate_once().decisions

    assert result.signal.strategy_id == "test_signaller"
    assert result.intent.direction is PositionDirection.SHORT
    assert result.decision.intent_id == result.intent.intent_id


def test_nothing_is_evaluated_before_a_quote_is_collected():
    # Grading an intent with no price is a guess, not a decision.
    runner = build(ticks=[], snapshots=[make_snapshot("1000000", observed_at=T0)])

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert cycle.blocked == {"USDJPY": "no quote collected"}


def test_nothing_is_evaluated_before_the_account_is_known():
    # Every loss limit is measured against the account series; without a
    # snapshot there is nothing to measure.
    runner = build(
        ticks=[
            make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=1))
        ]
    )

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert cycle.blocked == {"USDJPY": "no account snapshot collected"}


def test_a_stale_quote_stops_the_evaluation():
    # A collector that is down leaves the last quote answering forever. Risk
    # would refuse an entry on it, but only after a strategy had already
    # formed a view on a price that is no longer the market.
    runner = build(
        ticks=[make_tick("158.840", "158.844", time=T0, received_at=T0)],
        snapshots=[make_snapshot("1000000", observed_at=at(minutes=1))],
        source_clock=FixedClock(at(minutes=1)),
    )

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert "quote is" in cycle.blocked["USDJPY"]


def test_a_stale_account_stops_the_evaluation():
    # `latest_known_before` keeps answering with the last row the collector
    # wrote, so a collector that died hours ago looks exactly like a healthy
    # one from here. Equity that old grades the loss limits against a book
    # that has since moved.
    runner = build(
        ticks=[make_tick("158.840", "158.844", time=T0, received_at=T0)],
        snapshots=[make_snapshot("1000000", observed_at=at(hours=-3))],
        source_clock=FixedClock(T0),
    )

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert "account snapshot is" in cycle.blocked["USDJPY"]


def test_a_snapshot_written_after_the_cycle_started_is_not_visible():
    # The clock is frozen for the cycle; the account collector runs in its own
    # process and can write partway through one. Reading that row would put a
    # value into the decision that was not knowable when it began.
    future = make_snapshot("2000000", observed_at=at(minutes=30))
    runner = build(
        ticks=[
            make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=1))
        ],
        snapshots=[make_snapshot("1000000", observed_at=at(minutes=1)), future],
        source_clock=FixedClock(at(minutes=1)),
    )

    (result,) = runner.evaluate_once().decisions

    assert result.decision.decided_at == at(minutes=1)


def test_a_loss_is_still_measured_after_an_outage_longer_than_the_window():
    # The collector was down for days and has just come back. Only the fresh
    # rows fall inside LOSS_WINDOW, so without the row from before it the
    # baseline would fall back to the oldest one visible — the post-restart
    # equity — and a 10% drawdown would grade as no loss at all.
    runner = build(
        ticks=[
            make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=1))
        ],
        snapshots=[
            make_snapshot("1000000", observed_at=at(days=-5)),
            make_snapshot("900000", observed_at=at(minutes=1)),
        ],
        source_clock=FixedClock(at(minutes=1)),
    )

    (result,) = runner.evaluate_once().decisions

    assert "DAILY_LOSS_WITHIN_LIMIT" in result.decision.reject_codes


def test_the_unverified_execution_path_is_reported_not_assumed():
    # Shadow never exercises the order path and no reconciliation has run, so
    # both appear as failed checks rather than being quietly passed.
    runner = build(**quote_and_account())

    (result,) = runner.evaluate_once().decisions

    assert "EXECUTION_ENABLED" in result.decision.reject_codes
    assert "ACCOUNT_RECONCILED" in result.decision.reject_codes
    assert result.decision.approved is False


def test_a_broker_clock_offset_does_not_age_the_quote():
    # The shape the live database actually holds: event_time on the broker's
    # clock (+3h at OANDA Japan), received_at on ours. Grading freshness
    # against the broker stamp would reject every quote as future-dated.
    runner = build(
        ticks=[
            make_tick("158.840", "158.844", time=at(hours=3), received_at=at(minutes=1))
        ],
        snapshots=[make_snapshot("1000000", observed_at=at(minutes=1))],
        source_clock=FixedClock(at(minutes=1)),
    )

    (result,) = runner.evaluate_once().decisions

    assert "QUOTE_FRESH" not in result.decision.reject_codes


def test_a_central_bank_window_halts_the_scalp_horizon():
    # The signalling strategy is SCALP, and the configuration says scalp halts
    # around a central-bank decision. Without the calendar wired, event_mode
    # stayed at the configured default and the halt never applied.
    window = EventRiskWindow(
        name="dual_central_bank_cluster",
        first_event_at=at(hours=2),
        last_event_at=at(hours=2),
        pre_hours=48,
        post_hours=24,
        actions={
            StrategyHorizon.SCALP: EventRiskMode.HALT,
            StrategyHorizon.SWING: EventRiskMode.REDUCED,
        },
    )
    runner = build(
        **quote_and_account(), event_risk=EventRiskCalendar([window], COVERED)
    )

    (result,) = runner.evaluate_once().decisions

    assert "EVENT_MODE_ALLOWS_ENTRY" in result.decision.reject_codes


def test_between_recorded_windows_entries_are_not_event_blocked():
    # Inside what the calendar covers, with no window active: the schedule is
    # known and known to be quiet. Two windows are what makes that
    # distinguishable — with one, "covered" and "inside the window" are the
    # same span.
    def decision_at(when):
        return EventRiskWindow(
            name="dual_central_bank_cluster",
            first_event_at=when,
            last_event_at=when,
            pre_hours=1,
            post_hours=1,
            actions={StrategyHorizon.SCALP: EventRiskMode.HALT},
        )

    calendar = EventRiskCalendar(
        [decision_at(at(hours=-5)), decision_at(at(hours=5))], COVERED
    )
    runner = build(**quote_and_account(), event_risk=calendar)

    (result,) = runner.evaluate_once().decisions

    assert "EVENT_MODE_ALLOWS_ENTRY" not in result.decision.reject_codes


def test_beyond_the_recorded_calendar_the_configured_default_applies():
    # The meeting file stops somewhere. Past its last window the calendar has
    # nothing to say, and that must not read as "nothing is near".
    far_off = EventRiskWindow(
        name="dual_central_bank_cluster",
        first_event_at=at(days=30),
        last_event_at=at(days=30),
        pre_hours=48,
        post_hours=24,
        actions={StrategyHorizon.SCALP: EventRiskMode.HALT},
    )
    runner = build(
        **quote_and_account(),
        # Recorded only around that distant decision; now is outside it.
        event_risk=EventRiskCalendar([far_off], (at(days=28), at(days=32))),
        event_mode_default=EventRiskMode.HALT,
    )

    (result,) = runner.evaluate_once().decisions

    assert "EVENT_MODE_ALLOWS_ENTRY" in result.decision.reject_codes


def test_without_a_calendar_the_configured_default_applies():
    # "No schedule is known" is not "nothing is near". With no calendar the
    # configured default decides, and replacing it with NORMAL would turn an
    # unknown schedule into a clear one.
    runner = build(
        **quote_and_account(),
        event_risk=None,
        event_mode_default=EventRiskMode.HALT,
    )

    (result,) = runner.evaluate_once().decisions

    assert "EVENT_MODE_ALLOWS_ENTRY" in result.decision.reject_codes


def test_the_configured_trading_switch_reaches_the_decision():
    off = build(**quote_and_account(), trading_enabled=False)
    on = build(**quote_and_account(), trading_enabled=True)

    (rejected,) = off.evaluate_once().decisions
    (graded,) = on.evaluate_once().decisions

    assert "TRADING_ENABLED" in rejected.decision.reject_codes
    assert "TRADING_ENABLED" not in graded.decision.reject_codes


# The instant build() runs on unless a test moves the clock.
CYCLE_AT = at(minutes=1)

TWO_PAIRS = (
    ShadowInstrument(usdjpy_spec(), trading_enabled=True),
    ShadowInstrument(eurusd_spec(), trading_enabled=False),
)


def usdjpy_tick(time=CYCLE_AT):
    return make_tick("158.840", "158.844", time=time, received_at=time)


def eurusd_tick(time=CYCLE_AT):
    return make_tick("1.08000", "1.08010", time=time, symbol="EURUSD", received_at=time)


def two_pairs(*, ticks=None, snapshots=None):
    """USDJPY and EURUSD evaluated together, each with a fresh quote and the
    account known unless a test takes one away. EURUSD is not trading-enabled,
    which is how the two symbols are told apart in what Risk says about them."""
    return {
        "ticks": [usdjpy_tick(), eurusd_tick()] if ticks is None else ticks,
        "snapshots": (
            [make_snapshot("1000000", observed_at=CYCLE_AT)]
            if snapshots is None
            else snapshots
        ),
        "strategy": MultiSymbolSignallingStrategy,
        "instruments": TWO_PAIRS,
    }


def by_symbol(cycle):
    return {result.signal.symbol: result for result in cycle.decisions}


def test_each_symbol_is_sized_with_its_own_spec_and_quote():
    # 500 JPY of risk budget against 5 pips: 0.05 JPY per unit on USDJPY, and
    # 0.0005 USD per unit on EURUSD converted at the USDJPY ask. The
    # quantities differ only if pip size, quote currency and entry price all
    # came from the signal's own instrument.
    runner = build(**two_pairs())

    results = by_symbol(runner.evaluate_once())

    assert set(results) == {"USDJPY", "EURUSD"}
    assert results["USDJPY"].intent.target_quantity == Decimal(10000)
    assert results["EURUSD"].intent.target_quantity == Decimal(6000)


def test_each_symbols_trading_switch_reaches_its_decision():
    runner = build(**two_pairs())

    results = by_symbol(runner.evaluate_once())

    assert "INSTRUMENT_TRADING_ENABLED" not in results["USDJPY"].decision.reject_codes
    assert "INSTRUMENT_TRADING_ENABLED" in results["EURUSD"].decision.reject_codes


def test_event_windows_apply_to_each_symbols_currency_legs():
    # An ECB decision touches EUR: it halts EURUSD scalp entries and says
    # nothing about USDJPY, which only holds if each intent is graded with
    # its own instrument's legs.
    window = EventRiskWindow(
        name="eur_central_bank_window",
        first_event_at=at(hours=2),
        last_event_at=at(hours=2),
        pre_hours=48,
        post_hours=24,
        actions={StrategyHorizon.SCALP: EventRiskMode.HALT},
        affected_currencies=frozenset({Currency.EUR}),
    )
    runner = build(**two_pairs(), event_risk=EventRiskCalendar([window], COVERED))

    results = by_symbol(runner.evaluate_once())

    assert "EVENT_MODE_ALLOWS_ENTRY" not in results["USDJPY"].decision.reject_codes
    assert "EVENT_MODE_ALLOWS_ENTRY" in results["EURUSD"].decision.reject_codes


def test_a_missing_quote_only_blocks_its_symbol():
    # collector は symbol ごとに独立しており、1 pair の停止で他を止めない。
    # blocked symbol も dispatch で dedupe を消費するため、signal は trail に残す。
    store = FakeDecisionRepository()
    runner = build(**two_pairs(ticks=[usdjpy_tick()]), decisions=store)

    cycle = runner.evaluate_once()

    assert cycle.blocked == {"EURUSD": "no quote collected"}
    assert [result.signal.symbol for result in cycle.decisions] == ["USDJPY"]
    assert [signal.symbol for _, signal in store.signals] == ["EURUSD", "USDJPY"]


def test_a_signal_outside_this_runners_symbols_is_ignored():
    store = FakeDecisionRepository()
    runner = build(
        **quote_and_account(),
        strategy=OutOfScopeSignallingStrategy,
        decisions=store,
    )

    cycle = runner.evaluate_once()

    assert cycle.blocked == {}
    assert cycle.decisions == ()
    assert store.signals == []


def test_a_stale_quote_only_blocks_its_symbol():
    runner = build(**two_pairs(ticks=[usdjpy_tick(), eurusd_tick(time=T0)]))

    cycle = runner.evaluate_once()

    assert "quote is" in cycle.blocked["EURUSD"]
    assert [result.signal.symbol for result in cycle.decisions] == ["USDJPY"]


def test_a_missing_account_blocks_every_symbol_before_dispatch():
    # Every symbol is sized from the same equity and graded against the same
    # loss history, so no account means no evaluation at all: not a
    # per-symbol condition, and nothing is dispatched or refreshed.
    store = FakeDecisionRepository()
    features = RecordingFeatureSource()
    runner = build(**two_pairs(snapshots=[]), decisions=store, features=features)

    cycle = runner.evaluate_once()

    assert cycle.blocked == {
        "USDJPY": "no account snapshot collected",
        "EURUSD": "no account snapshot collected",
    }
    assert cycle.decisions == ()
    assert store.signals == []
    assert features.refreshed_at == []


def test_a_disabled_strategy_is_never_evaluated():
    runner = build(**quote_and_account(), enabled=False)

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    # Nothing is wrong: the runner simply had nothing to decide.
    assert cycle.blocked == {}


def test_a_strategy_with_nothing_to_say_produces_no_decision():
    runner = build(**quote_and_account(), strategy=SilentStrategy)

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert cycle.blocked == {}


def test_the_whole_evaluation_reads_one_instant():
    # The signal is stamped from the strategy's clock and the decision from
    # Risk's; both are the same object, and a cycle must not let it move
    # between them.
    source = FixedClock(at(minutes=1))
    runner = build(**quote_and_account(), source_clock=source)

    (result,) = runner.evaluate_once().decisions

    assert result.signal.generated_at == result.decision.decided_at == at(minutes=1)


def test_every_graded_decision_is_recorded():
    # Recording the trail is what a shadow run is for; the printed line is a
    # convenience on top of it.
    store = FakeDecisionRepository()
    runner = build(**quote_and_account(), decisions=store)

    (result,) = runner.evaluate_once().decisions

    assert store.trails == [(ACCOUNT, result.signal, result.intent, result.decision)]


def test_a_signal_that_sizes_to_nothing_is_still_recorded():
    # The risk budget can come out below one volume step. No intent is built,
    # but the strategy did form a view — and a strategy whose signals never
    # become intents is exactly what a shadow run is watching for.
    store = FakeDecisionRepository()
    runner = build(
        ticks=[
            make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=1))
        ],
        snapshots=[make_snapshot("1", observed_at=at(minutes=1))],
        source_clock=FixedClock(at(minutes=1)),
        decisions=store,
    )

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert [(owner, s.strategy_id) for owner, s in store.signals] == [
        (ACCOUNT, "test_signaller")
    ]
    assert store.trails == []


def test_a_cycle_that_decides_nothing_records_nothing():
    store = FakeDecisionRepository()
    runner = build(**quote_and_account(), strategy=SilentStrategy, decisions=store)

    runner.evaluate_once()

    assert store.trails == []


def test_a_blocked_cycle_records_nothing():
    store = FakeDecisionRepository()
    runner = build(
        ticks=[make_tick("158.840", "158.844", time=T0, received_at=T0)],
        snapshots=[make_snapshot("1000000", observed_at=at(minutes=1))],
        source_clock=FixedClock(at(minutes=1)),
        decisions=store,
    )

    assert runner.evaluate_once().blocked is not None
    assert store.trails == []


class RecordingFeatureSource:
    def __init__(self):
        self.refreshed_at = []

    def refresh(self, now):
        self.refreshed_at.append(now)


def test_features_are_refreshed_at_the_cycle_instant():
    source = RecordingFeatureSource()
    runner = build(**quote_and_account(), features=source)

    cycle = runner.evaluate_once()

    assert source.refreshed_at == [cycle.at]


def test_a_blocked_cycle_does_not_refresh_features():
    # No strategy will read the store this cycle, and half the point of the
    # guards is that a stalled collector stops the loop touching anything.
    source = RecordingFeatureSource()
    runner = build(features=source)

    assert runner.evaluate_once().blocked is not None
    assert source.refreshed_at == []


def test_describe_names_the_strategy_and_the_verdict():
    runner = build(**quote_and_account())

    (result,) = runner.evaluate_once().decisions
    line = describe(result)

    assert "test_signaller" in line
    assert "REJECTED" in line


class FakeMt5:
    def __init__(self, *, selects: bool = True) -> None:
        self.selects = selects
        self.was_shut_down = False

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.was_shut_down = True

    def account_info(self):
        return SimpleNamespace(login=10000001, server="Test-Broker Demo")

    def symbol_select(self, symbol, enable) -> bool:
        return self.selects

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol,
            currency_base="USD",
            currency_profit="JPY",
            digits=3,
            trade_contract_size=100000.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=10.0,
            trade_stops_level=5,
            filling_mode=2,
        )

    def last_error(self):
        return (-1, "fake terminal")


def test_startup_reads_the_account_and_the_instrument_from_the_terminal():
    terminal = FakeMt5()

    account_id, instrument = broker_identity("USDJPY", terminal)

    assert account_id == ACCOUNT
    assert instrument.symbol == "USDJPY"
    assert instrument.pip_size == Decimal("0.01")


def test_the_terminal_is_released_even_when_startup_fails():
    # A symbol missing from Market Watch yields no quote at all, so it stops
    # the run — but the terminal connection must not be left open by it.
    terminal = FakeMt5(selects=False)

    with pytest.raises(MT5ConnectionError):
        broker_identity("USDJPY", terminal)

    assert terminal.was_shut_down


def test_the_account_key_matches_what_the_collector_stores():
    # The runner reads the series the account collector writes; a key built
    # differently on either side would read an empty history.
    terminal = FakeMt5()

    account_id, _ = broker_identity("USDJPY", terminal)

    assert account_id == account_key_from_info(terminal.account_info())
