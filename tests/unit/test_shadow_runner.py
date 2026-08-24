"""Shadow runner: one instant per evaluation, decisions but no orders."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import (
    T0,
    FakeAccountSnapshotRepository,
    FakeBarRepository,
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
from trading.runner import StrategyBinding, StrategyRunner
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    StrategyStatus,
)

ACCOUNT = "Test-Broker Demo:10000001"


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
    risk_config = RiskConfig(trading_enabled=trading_enabled)
    return ShadowRunner(
        runner=StrategyRunner([binding]),
        portfolio=PortfolioManager(ledger, clock),
        ledger=ledger,
        risk=RiskEngine(risk_config, clock),
        risk_config=risk_config,
        market=market,
        snapshots=snapshot_store,
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
    return {
        "ticks": [make_tick("158.840", "158.844", time=T0, received_at=T0)],
        "snapshots": [make_snapshot("1000000", observed_at=T0)],
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
    runner = build(ticks=[make_tick("158.840", "158.844", time=T0, received_at=T0)])

    cycle = runner.evaluate_once()

    assert cycle.decisions == ()
    assert cycle.blocked == "no account snapshot collected"


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
    assert cycle.blocked is not None and "stale" not in cycle.blocked.lower()
    assert "old" in cycle.blocked


def test_a_snapshot_written_after_the_cycle_started_is_not_visible():
    # The clock is frozen for the cycle; the account collector runs in its own
    # process and can write partway through one. Reading that row would put a
    # value into the decision that was not knowable when it began.
    future = make_snapshot("2000000", observed_at=at(minutes=30))
    runner = build(
        ticks=[make_tick("158.840", "158.844", time=T0, received_at=T0)],
        snapshots=[make_snapshot("1000000", observed_at=T0), future],
        source_clock=FixedClock(at(minutes=1)),
    )

    (result,) = runner.evaluate_once().decisions

    assert result.decision.decided_at == at(minutes=1)


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


def test_a_later_cycle_moves_to_the_new_instant():
    source = FixedClock(at(minutes=1))
    runner = build(**quote_and_account(), source_clock=source)
    runner.evaluate_once()

    source.advance(minutes=2)
    (result,) = runner.evaluate_once().decisions

    assert result.decision.decided_at == at(minutes=3)


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
