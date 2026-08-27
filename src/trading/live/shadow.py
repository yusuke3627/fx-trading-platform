"""Shadow runner: the whole decision path, with nothing reaching the broker.

Strategies evaluate against live data, Portfolio sizes the signals into
intents, Risk grades them — and the result is reported instead of executed.
The runner holds no OMS and no broker adapter, so "no orders are sent" is a
property of what is wired rather than a flag that could be flipped.

The broker is touched once, at startup, for two facts that only it has: which
account the terminal is connected to, and the instrument's specification.
Every evaluation after that reads the stored series, so the loop runs on the
database alone and what a strategy sees is what was collected.

Two things this runner cannot claim, and reports honestly rather than
assuming: the order path is unverified (`execution_enabled=False`), and no
reconciliation has run (`account_reconciled=False`). Risk grades every check
regardless and lists what failed, so those two appear in reject_codes next to
whatever else did or did not pass.

Usage (Windows host with MT5 terminal):

    python -m trading.live.shadow --env shadow --symbol USDJPY
    python -m trading.live.shadow --env shadow --once
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.data.cli import poll_interval
from trading.data.features import StoredFeatureSource
from trading.data.market import MarketDataService
from trading.domain.account import AccountMode
from trading.domain.event import EventEnvelope
from trading.domain.exposure import OpenPositionExposure
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel, RiskDecision
from trading.domain.signal import StrategySignal
from trading.execution.mt5 import mapper
from trading.execution.mt5.adapter import MT5ConnectionError, load_mt5_module
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.live.clock import CycleClock
from trading.portfolio.exposure import CurrencyExposureService
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine
from trading.risk.event_risk import EventRiskCalendar
from trading.runner import StrategyRunner
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
    decision: RiskDecision


@dataclass(frozen=True)
class ShadowCycle:
    """What one evaluation produced, or why it produced nothing.

    An empty cycle is not self-explanatory: no strategy had anything to say,
    the market has not been collected, and the account series has gone stale
    all look identical from the outside, and they call for different actions.
    """

    at: datetime
    decisions: tuple[ShadowDecision, ...] = ()
    blocked: str | None = None


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
        instrument: InstrumentSpec,
        instrument_trading_enabled: bool,
        exposure: CurrencyExposureService,
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
        self._instrument = instrument
        self._instrument_trading_enabled = instrument_trading_enabled
        self._exposure = exposure
        self._features = features

    def evaluate_once(self) -> ShadowCycle:
        """One evaluation of every enabled strategy at a single instant."""
        now = self._clock.begin_cycle()
        quote = self._market.latest_tick(self._instrument.symbol)
        account = self._snapshots.latest_known_before(self._account_id, now)
        if quote is None:
            return ShadowCycle(at=now, blocked="no quote collected")
        # Risk would refuse an entry on a quote this old anyway; refusing here
        # keeps a strategy from forming a view on a price that is no longer
        # the market. The bound is the one Risk grades against, so the two
        # cannot drift apart.
        quote_age = (now - quote.known_time).total_seconds()
        if quote_age > self._risk_config.quote_max_age_seconds:
            return ShadowCycle(at=now, blocked=f"quote is {quote_age:.0f}s old")
        if account is None:
            return ShadowCycle(at=now, blocked="no account snapshot collected")
        age = now - account.observed_at
        if age > ACCOUNT_MAX_AGE:
            return ShadowCycle(at=now, blocked=f"account snapshot is {age} old")

        # Refreshed inside the cycle so every strategy in it reads one
        # consistent snapshot, taken at the frozen cycle instant.
        if self._features is not None:
            self._features.refresh(now)

        event = EventEnvelope(
            event_id=uuid4(),
            event_type="market.tick",
            source="live",
            retrieved_at=quote.known_time,
            known_at=quote.known_time,
        )
        collected = asyncio.run(self._runner.dispatch(event))
        if not collected:
            return ShadowCycle(at=now)
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

        results: list[ShadowDecision] = []
        for item in collected:
            signal = item.signal
            entry_price = (
                quote.ask
                if signal.desired_direction is PositionDirection.LONG
                else quote.bid
            )
            sizing = SizingInput(
                equity=account.equity,
                max_risk_per_trade_pct=self._risk_config.max_risk_per_trade_pct,
                pip_size=self._instrument.pip_size,
                quote_currency=self._instrument.quote_currency,
                volume_step=self._instrument.volume_step,
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
                context = self._pretrade_context(
                    signal, intent, quote, account, history, now, item.horizon
                )
                decision = self._risk.evaluate(intent, context)
                # Written before it is reported: the record is the point of a
                # shadow run, and printing a decision that never reached the
                # database would make the log and the trail disagree.
                self._decisions.record(self._account_id, signal, intent, decision)
                results.append(
                    ShadowDecision(signal=signal, intent=intent, decision=decision)
                )
        return ShadowCycle(at=now, decisions=tuple(results))

    def run(self, interval_seconds: float) -> None:
        blocked: str | None = None
        while True:
            cycle = self.evaluate_once()
            # A block is a standing condition, not an event: repeating it every
            # few seconds would bury the decisions between them.
            if cycle.blocked != blocked:
                blocked = cycle.blocked
                if blocked is not None:
                    print(f"{cycle.at.isoformat()} not evaluating: {blocked}")
            for result in cycle.decisions:
                print(describe(result))
            time.sleep(interval_seconds)

    def _event_mode(self, horizon: StrategyHorizon, now: datetime) -> EventRiskMode:
        """Graded per horizon: a central-bank decision halts scalp entries
        while swing only reduces.

        The configured default applies wherever the schedule is unknown —
        no calendar at all, or an instant past what the one we have covers.
        That is not the same as NORMAL: NORMAL says the schedule is known and
        nothing is near, and a gap in the meeting file must not read that way.
        """
        if self._event_risk is None:
            return self._risk_config.event_mode_default
        mode = self._event_risk.mode_for_instrument(self._instrument, horizon, now)
        return mode if mode is not None else self._risk_config.event_mode_default

    def _pretrade_context(
        self, signal, intent, quote, account, history, now, horizon
    ) -> PreTradeContext:
        symbol = signal.symbol
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
            instrument=self._instrument,
            account=account,
            snapshots=history,
            # The virtual ledger is the only book this runner can see, and no
            # fill ever reaches it, so both of these stay at zero. They are
            # read from the ledger rather than written as constants so that
            # the day a fill does arrive, they follow it.
            symbol_open_positions_count=len(
                self._ledger.positions_for_symbol(symbol)
            ),
            portfolio_open_positions_count=len(self._ledger.open_positions()),
            symbol_exposure_units=self._ledger.net_exposure(symbol),
            event_mode=self._event_mode(horizon, now),
            kill_switch=KillSwitchLevel.NONE,
            unknown_commands=0,
            account_mode=self._account_mode,
            instrument_trading_enabled=self._instrument_trading_enabled,
            # 仮想 book（fill は届かないため通常空）の通貨分解。stop は
            # VirtualPosition に無いので stop-risk は 0 として評価される。
            portfolio_risk=self._exposure.snapshot(
                [
                    OpenPositionExposure(
                        spec=self._instrument,
                        signed_units=p.signed_quantity,
                        mark_price=quote.mid,
                    )
                    for p in self._ledger.open_positions()
                ],
                now,
            ),
            stop_distance_pips=signal.stop_distance_pips,
            requested_quantity=intent.target_quantity or Decimal(0),
        )


def describe(result: ShadowDecision) -> str:
    decision = result.decision
    verdict = "APPROVED" if decision.approved else "REJECTED"
    rejects = " ".join(decision.reject_codes)
    return (
        f"{decision.decided_at.isoformat()} {result.signal.strategy_id} "
        f"{result.signal.symbol} {result.intent.action} "
        f"{result.signal.desired_direction} qty={result.intent.target_quantity} "
        f"{verdict}{' ' + rejects if rejects else ''}"
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
    from trading.live.wiring import build_runner, traded_symbols

    parser = argparse.ArgumentParser(description="Shadow strategy runner")
    parser.add_argument("--env", default="shadow")
    parser.add_argument("--symbol", default=None)
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
    symbol = args.symbol or config.market.primary_instruments[0]
    # Only this symbol's spec is loaded, so a symbol no running strategy trades
    # leaves every evaluation asking for an instrument that is not there — and
    # doing nothing about it, quietly, for as long as the process lives.
    traded = traded_symbols(config)
    if symbol not in traded:
        raise SystemExit(f"no enabled strategy trades {symbol}: {sorted(traded)}")
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    account_id, instrument = broker_identity(symbol)
    # instruments 設定に無い symbol は発注不可として評価する（fail-close）。
    instrument_policy = config.instruments.get(symbol)

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
        {symbol: instrument},
    )
    ledger = VirtualPositionLedger(clock)
    # 換算も市場と同じ stored series を読む: sizing の quote 鮮度制約と
    # risk config の quote_max_age を一致させる。換算 path の spec は取引銘柄
    # と独立に config から解決する — 非 JPY quote の銘柄（EURUSD 等）では
    # 取引銘柄自身の path（EUR↔USD）だけでは JPY 換算が張れない。
    conversion_specs = [instrument] + [
        broker_identity(sym)[1]
        for sym in config.market.conversion_instruments
        if sym != symbol
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
            config, market=market, clock=clock, ledger=ledger, features=store
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
        instrument=instrument,
        instrument_trading_enabled=instrument_policy is not None
        and instrument_policy.trading_enabled,
        exposure=CurrencyExposureService(conversion),
        features=features,
    )

    print(f"shadow on {symbol} for {account_id}")
    if args.once:
        cycle = runner.evaluate_once()
        if cycle.blocked is not None:
            raise SystemExit(f"not evaluating: {cycle.blocked}")
        for result in cycle.decisions:
            print(describe(result))
        print(f"{len(cycle.decisions)} decisions")
    else:
        runner.run(args.interval_seconds)


if __name__ == "__main__":
    main()
