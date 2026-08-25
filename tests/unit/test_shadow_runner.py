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
    make_snapshot,
    make_tick,
    usdjpy_spec,
)
from trading.data.market.stored import StoredMarketData
from trading.domain.account import AccountMode
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.execution.mt5.adapter import MT5ConnectionError
from trading.execution.mt5.mapper import account_key_from_info
from trading.indicators import IndicatorService
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RuleBasedRegimeService
from trading.live.clock import CycleClock
from trading.live.shadow import ShadowRunner, broker_identity, describe
from trading.portfolio.manager import PortfolioManager
from trading.portfolio.virtual_ledger import VirtualPositionLedger
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
# A calendar that knows the schedule and finds nothing near, as opposed to
# None, which means the schedule is not known at all.
NO_WINDOWS = EventRiskCalendar([])


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
):
    clock = CycleClock(source_clock or FixedClock(at(minutes=1)))
    market = StoredMarketData(
        FakeTickRepository(ticks), FakeBarRepository(), clock, {"USDJPY": usdjpy_spec()}
    )
    snapshot_store = FakeAccountSnapshotRepository()
    for snapshot in snapshots:
        snapshot_store.insert(ACCOUNT, snapshot)
    ledger = VirtualPositionLedger(clock)
    binding = StrategyBinding(
        strategy=(strategy or SignallingStrategy)(),
        context=_context(clock, market, ledger, enabled),
    )
    risk_config = RiskConfig(
        trading_enabled=trading_enabled, event_mode_default=event_mode_default
    )
    return ShadowRunner(
        runner=StrategyRunner([binding]),
        portfolio=PortfolioManager(ledger, clock),
        ledger=ledger,
        risk=RiskEngine(risk_config, clock),
        risk_config=risk_config,
        market=market,
        snapshots=snapshot_store,
        decisions=decisions or FakeDecisionRepository(),
        event_risk=event_risk,
        clock=clock,
        account_id=ACCOUNT,
        account_mode=AccountMode.HEDGING,
        instrument=usdjpy_spec(),
    )


def _context(clock, market, ledger, enabled):
    features = InMemoryFeatureStore()
    return StrategyContext(
        clock=clock,
        market=market,
        indicators=IndicatorService(market),
        features=features,
        regime=RuleBasedRegimeService(features),
        portfolio=ledger,
        config=StrategyConfig(
            strategy_id="test_signaller",
            enabled=enabled,
            status=StrategyStatus.SHADOW,
            instruments=["USDJPY"],
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
    assert cycle.blocked == "no quote collected"


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
    assert cycle.blocked == "no account snapshot collected"


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
    assert cycle.blocked is not None and "quote is" in cycle.blocked


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
    assert cycle.blocked is not None and "account snapshot is" in cycle.blocked


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
    runner = build(**quote_and_account(), event_risk=EventRiskCalendar([window]))

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

    calendar = EventRiskCalendar([decision_at(at(hours=-5)), decision_at(at(hours=5))])
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
        event_risk=EventRiskCalendar([far_off]),
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


def test_a_disabled_strategy_is_never_evaluated():
    runner = build(**quote_and_account(), enabled=False)

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    # Nothing is wrong: the runner simply had nothing to decide.
    assert cycle.blocked is None


def test_a_strategy_with_nothing_to_say_produces_no_decision():
    runner = build(**quote_and_account(), strategy=SilentStrategy)

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert cycle.blocked is None


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
