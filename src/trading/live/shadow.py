"""Shadow runner: the whole decision path, with nothing reaching the broker.

Strategies evaluate against live data, Portfolio sizes the signals into
intents, the Arbitrator picks which sized entries are graded in priority order,
and Risk grades them — the result is reported instead of executed.
The runner holds no OMS and no broker adapter, so "no orders are sent" is a
property of what is wired rather than a flag that could be flipped.

The broker is touched at startup for two facts that only it has: which account
the terminal is connected to, and the instruments' specifications.
Every evaluation after that reads the stored series, so the loop runs on the
database alone and what a strategy sees is what was collected.

Two things this runner cannot claim, and reports honestly rather than
assuming: the order path is unverified (`execution_enabled=False`), and no
reconciliation has run (`account_reconciled=False`). Risk grades every check
regardless and lists what failed, so those two appear in reject_codes next to
whatever else did or did not pass.

Usage (Windows host with MT5 terminal):

    python -m trading.live.shadow --env shadow --symbol USDJPY --symbol EURUSD
    python -m trading.live.shadow --env shadow --once
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from trading.data.cli import poll_interval
from trading.data.features import StoredFeatureSource
from trading.data.market import MarketDataService
from trading.domain.account import AccountMode
from trading.domain.arbitration import (
    ArbitrationCandidate,
    ArbitrationDecision,
    CandidateSignal,
)
from trading.domain.event import EventEnvelope
from trading.domain.exposure import OpenPositionExposure, PortfolioRiskSnapshot
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent
from trading.domain.market import Tick
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel, RiskDecision
from trading.domain.signal import StrategySignal
from trading.execution.mt5 import mapper
from trading.execution.mt5.adapter import MT5ConnectionError, load_mt5_module
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.live.clock import CycleClock
from trading.portfolio.arbitrator import PortfolioArbitrator
from trading.portfolio.exposure import CurrencyExposureService
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine
from trading.risk.event_risk import EventRiskCalendar
from trading.runner import CollectedSignal, StrategyRunner
from trading.storage.repository import AccountSnapshotRepository, DecisionRepository
from trading.strategy.base import StrategyHorizon

DEFAULT_INTERVAL_SECONDS = 5.0

# How far back the snapshots handed to Risk reach. The rolling window is 24h
# and the JST day opens at most 24h ago, and each needs a row at or before its
# start: a window that only just covered them would leave the baseline to the
# fallback (the oldest row known) the moment a snapshot was missed.
LOSS_WINDOW = timedelta(hours=48)

# How old the account may be before an evaluation is worthless. The collector
# writes every 60s, so minutes of silence means it is down — and `latest`
# keeps answering with the last row it wrote, so nothing else here would
# notice. Equity that old grades the loss limits against a book that has since
# moved, which is worse than not grading them.
ACCOUNT_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class ShadowDecision:
    signal: StrategySignal
    intent: PositionIntent
    # Risk の grade。Arbitrator が退けた候補は Risk に届かないため None。
    decision: RiskDecision | None
    # entry 候補の裁定。exit（CLOSE）は裁定を経ないため None。
    arbitration: ArbitrationDecision | None = None


@dataclass(frozen=True)
class ShadowInstrument:
    spec: InstrumentSpec
    trading_enabled: bool


@dataclass(frozen=True)
class ShadowCycle:
    """What one evaluation produced, or why it produced nothing.

    An empty cycle is not self-explanatory: no strategy had anything to say,
    the market has not been collected, and the account series has gone stale
    all look identical from the outside, and they call for different actions.
    """

    at: datetime
    decisions: tuple[ShadowDecision, ...] = ()
    blocked: dict[str, str] = field(default_factory=dict)


class ShadowRunner:
    def __init__(
        self,
        *,
        runner: StrategyRunner,
        portfolio: PortfolioManager,
        ledger: VirtualPositionLedger,
        risk: RiskEngine,
        risk_config: RiskConfig,
        market: MarketDataService,
        snapshots: AccountSnapshotRepository,
        decisions: DecisionRepository,
        event_risk: EventRiskCalendar | None,
        clock: CycleClock,
        account_id: str,
        account_mode: AccountMode,
        instruments: Sequence[ShadowInstrument],
        exposure: CurrencyExposureService,
        arbitrator: PortfolioArbitrator,
        features: StoredFeatureSource | None = None,
    ) -> None:
        self._runner = runner
        self._portfolio = portfolio
        self._ledger = ledger
        self._risk = risk
        self._risk_config = risk_config
        self._market = market
        self._snapshots = snapshots
        self._decisions = decisions
        self._event_risk = event_risk
        self._clock = clock
        self._account_id = account_id
        self._account_mode = account_mode
        self._instruments = {
            instrument.spec.symbol: instrument for instrument in instruments
        }
        self._exposure = exposure
        self._arbitrator = arbitrator
        self._features = features

    def evaluate_once(self) -> ShadowCycle:
        """One evaluation of every enabled strategy at a single instant."""
        now = self._clock.begin_cycle()
        account = self._snapshots.latest_known_before(self._account_id, now)
        if account is None:
            return ShadowCycle(
                at=now,
                blocked={
                    symbol: "no account snapshot collected"
                    for symbol in self._instruments
                },
            )
        age = now - account.observed_at
        if age > ACCOUNT_MAX_AGE:
            return ShadowCycle(
                at=now,
                blocked={
                    symbol: f"account snapshot is {age} old"
                    for symbol in self._instruments
                },
            )

        quotes: dict[str, Tick] = {}
        blocked: dict[str, str] = {}
        for symbol in self._instruments:
            quote = self._market.latest_tick(symbol)
            if quote is None:
                blocked[symbol] = "no quote collected"
                continue
            # Risk would refuse an entry on a quote this old anyway; refusing
            # here keeps a strategy from forming a view on a stale price. The
            # bound is the one Risk grades against, so the two cannot drift.
            quote_age = (now - quote.known_time).total_seconds()
            if quote_age > self._risk_config.quote_max_age_seconds:
                blocked[symbol] = f"quote is {quote_age:.0f}s old"
                continue
            quotes[symbol] = quote
        if not quotes:
            return ShadowCycle(at=now, blocked=blocked)

        # Refreshed inside the cycle so every strategy in it reads one
        # consistent snapshot, taken at the frozen cycle instant.
        if self._features is not None:
            self._features.refresh(now)

        event = EventEnvelope(
            event_id=uuid4(),
            event_type="market.tick",
            source="live",
            retrieved_at=now,
            known_at=now,
        )
        collected = asyncio.run(self._runner.dispatch(event))
        # 戦略は 1 dispatch で自分の全 instruments を評価するので、quote gate で
        # 止めた symbol の signal もここに混ざる。捨てると _new_setup の dedupe
        # だけが消費され、quote 復旧後も同じ setup が再生成されないため、sizing
        # できなくても trail には残す。quotes にも blocked にも無い symbol は
        # このプロセスの評価対象外なので記録しない。
        candidates: list[CollectedSignal] = []
        for item in collected:
            if item.signal.symbol in quotes:
                candidates.append(item)
            elif item.signal.symbol in blocked:
                self._decisions.record_signal(self._account_id, item.signal)
        if not candidates:
            return ShadowCycle(at=now, blocked=blocked)
        # Read once for the cycle: the window is the same for every intent in
        # it, and it is the largest query the loop makes.
        history = list(
            self._snapshots.known_before(self._account_id, now, now - LOSS_WINDOW)
        )
        # Each loss window needs a row at or before its own start. After an
        # outage longer than LOSS_WINDOW that row falls outside the range, and
        # _baseline_equity then falls back to the oldest row it can see — which
        # just after a restart is the current equity, reporting no loss at all.
        # One row from before the range is what keeps the baseline real.
        baseline = self._snapshots.latest_known_before(
            self._account_id, now - LOSS_WINDOW
        )
        if baseline is not None:
            history.insert(0, baseline)

        # 仮想 book（fill は届かないため通常空）。stop は VirtualPosition に無いので
        # stop-risk は 0 として評価される。mark は当 cycle の quote: fill が届かない
        # 以上、quote gate で止めた symbol に position があることはない。
        base_book = [
            OpenPositionExposure(
                spec=self._instruments[position.symbol].spec,
                signed_units=position.signed_quantity,
                mark_price=quotes[position.symbol].mid,
            )
            for position in self._ledger.open_positions()
        ]

        sized: list[tuple[CollectedSignal, PositionIntent]] = []
        for item in candidates:
            signal = item.signal
            instrument = self._instruments[signal.symbol].spec
            quote = quotes[signal.symbol]
            entry_price = (
                quote.ask
                if signal.desired_direction is PositionDirection.LONG
                else quote.bid
            )
            sizing = SizingInput(
                equity=account.equity,
                max_risk_per_trade_pct=self._risk_config.max_risk_per_trade_pct,
                pip_size=instrument.pip_size,
                quote_currency=instrument.quote_currency,
                volume_step=instrument.volume_step,
                entry_price=entry_price,
            )
            intents = self._portfolio.intents_from_signal(signal, sizing)
            if not intents:
                # Sizing landed below one volume step, or the stop distance was
                # not usable. The signal still happened, and a strategy whose
                # signals never become intents is the kind of thing a shadow
                # run exists to surface — losing it here would hide it.
                self._decisions.record_signal(self._account_id, signal)
                continue
            for intent in intents:
                sized.append((item, intent))

        exits: list[tuple[CollectedSignal, PositionIntent]] = []
        entries: dict[UUID, tuple[CollectedSignal, PositionIntent]] = {}
        arbitration_candidates: list[ArbitrationCandidate] = []
        for item, intent in sized:
            if intent.action is PositionAction.CLOSE:
                exits.append((item, intent))
                continue
            entries[item.signal.signal_id] = (item, intent)
            arbitration_candidates.append(
                self._candidate(item.signal, intent, quotes[intent.symbol])
            )
        arbitration = self._arbitrator.select(arbitration_candidates, base_book, now)

        # Written before each result is reported: the record is the point of a
        # shadow run, and printing a decision that never reached the database
        # would make the log and the trail disagree.
        results: list[ShadowDecision] = []
        # exit は risk 削減なので裁定を経ず、既存 book で grade する。
        for item, intent in exits:
            decision = self._grade(
                item,
                intent,
                quotes[intent.symbol],
                account,
                history,
                now,
                base_book,
            )
            self._decisions.record(self._account_id, item.signal, intent, decision)
            results.append(
                ShadowDecision(signal=item.signal, intent=intent, decision=decision)
            )
        # 受理候補は priority 順に、先に受理した候補を含む book で grade する。
        # これが accept ごとの既存 Risk limit 再計算になる。
        for verdict in arbitration.accepted:
            item, intent = entries[verdict.signal_id]
            decision = self._grade(
                item,
                intent,
                quotes[intent.symbol],
                account,
                history,
                now,
                verdict.book_before,
            )
            self._decisions.record(
                self._account_id,
                item.signal,
                intent,
                decision,
                arbitration=verdict,
            )
            results.append(
                ShadowDecision(
                    signal=item.signal,
                    intent=intent,
                    decision=decision,
                    arbitration=verdict,
                )
            )
        # 却下候補は Risk に届かないが trail には残す（捨てると setup の dedupe
        # だけが消費される）。
        for verdict in arbitration.rejected:
            item, intent = entries[verdict.signal_id]
            self._decisions.record_arbitration(
                self._account_id, item.signal, intent, verdict
            )
            results.append(
                ShadowDecision(
                    signal=item.signal,
                    intent=intent,
                    decision=None,
                    arbitration=verdict,
                )
            )
        return ShadowCycle(at=now, decisions=tuple(results), blocked=blocked)

    def run(self, interval_seconds: float) -> None:
        blocked: dict[str, str] = {}
        while True:
            cycle = self.evaluate_once()
            # A block is a standing condition, not an event: repeating it every
            # few seconds would bury the decisions between them.
            for symbol, reason in cycle.blocked.items():
                if blocked.get(symbol) != reason:
                    print(
                        f"{cycle.at.isoformat()} not evaluating {symbol}: {reason}"
                    )
            blocked = cycle.blocked
            for result in cycle.decisions:
                print(describe(result))
            time.sleep(interval_seconds)

    def _event_mode(
        self,
        instrument: InstrumentSpec,
        horizon: StrategyHorizon,
        now: datetime,
    ) -> EventRiskMode:
        """Graded per horizon: a central-bank decision halts scalp entries
        while swing only reduces.

        The configured default applies wherever the schedule is unknown —
        no calendar at all, or an instant past what the one we have covers.
        That is not the same as NORMAL: NORMAL says the schedule is known and
        nothing is near, and a gap in the meeting file must not read that way.
        """
        if self._event_risk is None:
            return self._risk_config.event_mode_default
        mode = self._event_risk.mode_for_instrument(instrument, horizon, now)
        return mode if mode is not None else self._risk_config.event_mode_default

    def _candidate(
        self, signal: StrategySignal, intent: PositionIntent, quote: Tick
    ) -> ArbitrationCandidate:
        entry_price = (
            quote.ask
            if signal.desired_direction is PositionDirection.LONG
            else quote.bid
        )
        quantity = intent.target_quantity or Decimal(0)
        signed_units = (
            quantity
            if signal.desired_direction is PositionDirection.LONG
            else -quantity
        )
        stop = intent.protection.stop_loss_price if intent.protection else None
        exposure = OpenPositionExposure(
            spec=self._instruments[signal.symbol].spec,
            signed_units=signed_units,
            mark_price=entry_price,
            stop_loss_price=stop,
        )
        # shadow は live 発注許可前の pair も証拠収集するため、裁定上は
        # as-if 有効にする。実際の instrument policy は Risk が報告する。
        return ArbitrationCandidate(
            signal=CandidateSignal.from_signal(signal),
            exposure=exposure,
            trading_enabled=True,
        )

    def _grade(
        self,
        item: CollectedSignal,
        intent: PositionIntent,
        quote: Tick,
        account,
        history,
        now: datetime,
        book: Sequence[OpenPositionExposure],
    ) -> RiskDecision:
        portfolio_risk = self._exposure.snapshot(book, now)
        context = self._pretrade_context(
            item.signal,
            intent,
            quote,
            account,
            history,
            now,
            item.horizon,
            book,
            portfolio_risk,
        )
        return self._risk.evaluate(intent, context)

    def _pretrade_context(
        self,
        signal,
        intent,
        quote,
        account,
        history,
        now,
        horizon,
        book: Sequence[OpenPositionExposure],
        portfolio_risk: PortfolioRiskSnapshot,
    ) -> PreTradeContext:
        symbol = signal.symbol
        instrument = self._instruments[symbol]
        return PreTradeContext(
            now=now,
            # Nothing here has exercised the order path, so reporting it as
            # usable would be a claim this runner is not in a position to make.
            execution_enabled=False,
            broker_connected=account.broker_connected,
            # Reconciliation belongs to the trading application's startup,
            # which does not exist yet.
            account_reconciled=False,
            quote=quote,
            instrument=instrument.spec,
            account=account,
            snapshots=history,
            # book は ledger の仮想 position（fill は届かないため通常空）と、
            # 当 cycle で Arbitrator が先に受理した候補を含む。
            symbol_open_positions_count=sum(
                1 for exposure in book if exposure.spec.symbol == symbol
            ),
            portfolio_open_positions_count=len(book),
            symbol_exposure_units=sum(
                (
                    exposure.signed_units
                    for exposure in book
                    if exposure.spec.symbol == symbol
                ),
                Decimal(0),
            ),
            event_mode=self._event_mode(instrument.spec, horizon, now),
            kill_switch=KillSwitchLevel.NONE,
            unknown_commands=0,
            account_mode=self._account_mode,
            instrument_trading_enabled=instrument.trading_enabled,
            portfolio_risk=portfolio_risk,
            stop_distance_pips=signal.stop_distance_pips,
            requested_quantity=intent.target_quantity or Decimal(0),
        )


def describe(result: ShadowDecision) -> str:
    decision = result.decision
    arbitration = result.arbitration
    if decision is None:
        assert arbitration is not None
        return (
            f"{arbitration.decided_at.isoformat()} {result.signal.strategy_id} "
            f"{result.signal.symbol} {result.intent.action} "
            f"{result.signal.desired_direction} qty={result.intent.target_quantity} "
            f"ARBITRATED_OUT {arbitration.reason_code}"
        )
    verdict = "APPROVED" if decision.approved else "REJECTED"
    rank = f" rank={arbitration.rank}" if arbitration is not None else ""
    rejects = " ".join(decision.reject_codes)
    return (
        f"{decision.decided_at.isoformat()} {result.signal.strategy_id} "
        f"{result.signal.symbol} {result.intent.action} "
        f"{result.signal.desired_direction} qty={result.intent.target_quantity} "
        f"{verdict}{rank}{' ' + rejects if rejects else ''}"
    )


def broker_identity(
    symbol: str, mt5_module: Any | None = None
) -> tuple[str, InstrumentSpec]:
    """The account the terminal is on, and the symbol's specification.

    Read once at startup and then held: these are the two facts the stored
    series does not carry. The MT5 module is used directly rather than through
    the execution adapter, the same way the collectors do it — a shadow run
    must not be holding an object that can send orders.
    """
    mt5 = mt5_module if mt5_module is not None else load_mt5_module()
    if not mt5.initialize():
        raise MT5ConnectionError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info failed: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise MT5ConnectionError(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            raise MT5ConnectionError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        return (
            mapper.account_key_from_info(info),
            mapper.instrument_spec_from_symbol_info(symbol_info),
        )
    finally:
        mt5.shutdown()


def main() -> None:
    import os

    from trading.config import load_config
    from trading.data.market.stored import StoredMarketData
    from trading.data.policy.risk_windows import central_bank_calendar
    from trading.live.wiring import build_runner, runner_symbols
    from trading.portfolio.arbitrator import PortfolioArbitrator

    parser = argparse.ArgumentParser(description="Shadow strategy runner")
    parser.add_argument("--env", default="shadow")
    parser.add_argument("--symbol", action="append", default=None)
    parser.add_argument(
        "--interval-seconds", type=poll_interval, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="evaluate once and exit instead of following the market",
    )
    args = parser.parse_args()

    config = load_config(args.env)
    try:
        symbols = runner_symbols(config, args.symbol)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    instrument_specs: dict[str, InstrumentSpec] = {}
    for symbol in symbols:
        account_id, instrument_specs[symbol] = broker_identity(symbol)
    instruments = [
        ShadowInstrument(
            spec=instrument_specs[symbol],
            trading_enabled=config.instruments[symbol].trading_enabled,
        )
        for symbol in symbols
    ]

    # Imported here so the module stays unit-testable without the db extra.
    from trading.storage.postgres import (
        PostgresAccountSnapshotRepository,
        PostgresDecisionRepository,
        PostgresEventRepository,
        PostgresMacroObservationRepository,
        PostgresMarketBarRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(dsn)
    clock = CycleClock()
    market = StoredMarketData(
        PostgresMarketTickRepository(conn),
        PostgresMarketBarRepository(conn),
        clock,
        instrument_specs,
    )
    ledger = VirtualPositionLedger(clock)
    # 換算も市場と同じ stored series を読む: sizing の quote 鮮度制約と
    # risk config の quote_max_age を一致させる。換算 path の spec は取引銘柄
    # と独立に config から解決する — 非 JPY quote の銘柄（EURUSD 等）では
    # 取引銘柄自身の path（EUR↔USD）だけでは JPY 換算が張れない。
    conversion_specs = list(instrument_specs.values()) + [
        broker_identity(sym)[1]
        for sym in config.market.conversion_instruments
        if sym not in instrument_specs
    ]
    conversion = MarketQuoteConversionService(
        market,
        conversion_specs,
        max_quote_age_seconds=config.risk.quote_max_age_seconds,
    )
    store = InMemoryFeatureStore()
    features = StoredFeatureSource(
        PostgresMacroObservationRepository(conn),
        PostgresEventRepository(conn),
        InterventionRiskConfig(
            version=config.intelligence.intervention_risk.version,
            weights=config.intelligence.intervention_risk.weights,
        ),
        store,
    )
    runner = ShadowRunner(
        runner=build_runner(
            config,
            market=market,
            clock=clock,
            ledger=ledger,
            features=store,
            currency_states=features.currency_states,
        ),
        portfolio=PortfolioManager(ledger, clock, conversion),
        ledger=ledger,
        risk=RiskEngine(config.risk, clock, conversion),
        risk_config=config.risk,
        market=market,
        snapshots=PostgresAccountSnapshotRepository(conn),
        decisions=PostgresDecisionRepository(conn),
        event_risk=central_bank_calendar(config),
        clock=clock,
        account_id=account_id,
        account_mode=AccountMode(config.broker.expected_account_mode),
        instruments=instruments,
        exposure=CurrencyExposureService(conversion),
        arbitrator=PortfolioArbitrator(config.arbitrator),
        features=features,
    )

    print(f"shadow on {', '.join(symbols)} for {account_id}")
    if args.once:
        cycle = runner.evaluate_once()
        for symbol, reason in cycle.blocked.items():
            print(f"{cycle.at.isoformat()} not evaluating {symbol}: {reason}")
        for result in cycle.decisions:
            print(describe(result))
        print(f"{len(cycle.decisions)} decisions")
        if cycle.blocked:
            raise SystemExit(1)
    else:
        runner.run(args.interval_seconds)


if __name__ == "__main__":
    main()
