"""Shared test factories. All names/values are fictional test data."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.domain.account import AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.instrument import FillingMode, InstrumentSpec
from trading.domain.intent import PositionIntent, ProtectionSpec
from trading.domain.market import TIMEFRAME_SECONDS, Bar, Tick
from trading.domain.money import Currency
from trading.domain.order import CommandState, ExecutionCommand, ExecutionSide
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.risk import RiskDecision
from trading.domain.signal import StrategySignal

T0 = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)


def at(**kwargs) -> datetime:
    return T0 + timedelta(**kwargs)


class FixedClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def usdjpy_spec(**overrides) -> InstrumentSpec:
    values = {
        "symbol": "USDJPY",
        "base_currency": Currency.USD,
        "quote_currency": Currency.JPY,
        "digits": 3,
        "pip_size": Decimal("0.01"),
        "contract_size": Decimal(1000),
        "volume_min": Decimal(1000),
        "volume_step": Decimal(1000),
        "volume_max": Decimal(100000),
        "stop_level_points": 0,
        # IOC only, as OANDA Japan reports for USD/JPY.
        "accepted_filling_modes": frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def eurusd_spec(**overrides) -> InstrumentSpec:
    values = {
        "symbol": "EURUSD",
        "base_currency": Currency.EUR,
        "quote_currency": Currency.USD,
        "digits": 5,
        "pip_size": Decimal("0.0001"),
        "contract_size": Decimal(1000),
        "volume_min": Decimal(1000),
        "volume_step": Decimal(1000),
        "volume_max": Decimal(100000),
        "stop_level_points": 0,
        "accepted_filling_modes": frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def gbpusd_spec(**overrides) -> InstrumentSpec:
    values = {
        "symbol": "GBPUSD",
        "base_currency": Currency.GBP,
        "quote_currency": Currency.USD,
        "digits": 5,
        "pip_size": Decimal("0.0001"),
        "contract_size": Decimal(1000),
        "volume_min": Decimal(1000),
        "volume_step": Decimal(1000),
        "volume_max": Decimal(100000),
        "stop_level_points": 0,
        "accepted_filling_modes": frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def gbpjpy_spec(**overrides) -> InstrumentSpec:
    values = {
        "symbol": "GBPJPY",
        "base_currency": Currency.GBP,
        "quote_currency": Currency.JPY,
        "digits": 3,
        "pip_size": Decimal("0.01"),
        "contract_size": Decimal(1000),
        "volume_min": Decimal(1000),
        "volume_step": Decimal(1000),
        "volume_max": Decimal(100000),
        "stop_level_points": 0,
        "accepted_filling_modes": frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def make_tick(
    bid: str,
    ask: str,
    time: datetime = T0,
    symbol: str = "USDJPY",
    received_at: datetime | None = None,
) -> Tick:
    return Tick(
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        time=time,
        received_at=received_at,
    )


def make_bar(
    open_: str,
    high: str,
    low: str,
    close: str,
    start: datetime = T0,
    symbol: str = "USDJPY",
    timeframe: str = "1m",
    tick_volume: int = 0,
    known_at: datetime | None = None,
) -> Bar:
    # Default to a broker whose clock matches ours, so a test only states
    # known_at when the offset is what it is exercising.
    end = start + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=tick_volume,
        known_at=known_at if known_at is not None else end,
    )


class FakeTransport:
    """HttpTransport の台本版: 呼び出しごとに canned response を1つ返す。"""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.byte_calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.byte_calls.append(url)
        return self._responses.pop(0)

    def get_json(self, url: str, params: dict) -> object:
        self.get_calls.append((url, dict(params)))
        return self._responses.pop(0)

    def post_json(self, url: str, body: dict) -> object:
        self.post_calls.append((url, dict(body)))
        return self._responses.pop(0)


class FakeObservationRepository:
    """MacroObservationRepository in memory: series match plus the
    since-exclusive known_at window, the way Postgres answers it."""

    def __init__(self, observations: Sequence = ()) -> None:
        self.observations = list(observations)

    def known_before(self, series: str, t: datetime, since: datetime) -> list:
        return [
            o for o in self.observations if o.series == series and since < o.known_at <= t
        ]


class FakeEventRepository:
    """EventRepository reads in memory, mirroring the optional filters."""

    def __init__(self, events: Sequence[EventEnvelope] = ()) -> None:
        self.events = list(events)

    def known_before(
        self,
        t: datetime,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[EventEnvelope]:
        return [
            e
            for e in self.events
            if e.known_at <= t
            and (event_type is None or e.event_type == event_type)
            and (since is None or e.known_at > since)
        ]


class FakeTickRepository:
    """MarketTickRepository in memory, answering the visibility window the way
    Postgres does: event_time >= since AND received_at <= t, ordered by
    (event_time, arrival). Sorting is stable, so ticks sharing an event_time
    keep insertion order — the fake's stand-in for the id tie-break, which is
    what decides a bar's close and the latest price.
    """

    def __init__(self, ticks: Sequence[Tick] = ()) -> None:
        self.ticks = list(ticks)

    def known_before(self, symbol: str, t: datetime, since: datetime) -> list[Tick]:
        return [tick for tick in self._visible(symbol, t) if tick.time >= since]

    def latest_known_before(self, symbol: str, t: datetime) -> Tick | None:
        visible = self._visible(symbol, t)
        return visible[-1] if visible else None

    def earliest_known_after(
        self, symbol: str, t: datetime, since: datetime
    ) -> Tick | None:
        # Stops at the first match rather than building the window, mirroring
        # the LIMIT 1 the real query relies on.
        visible = self._visible(symbol, t)
        return next((tick for tick in visible if tick.time >= since), None)

    def _visible(self, symbol: str, t: datetime) -> list[Tick]:
        return sorted(
            (tick for tick in self.ticks if tick.symbol == symbol and tick.known_time <= t),
            key=lambda tick: tick.time,
        )

    def between(self, symbol: str, start: datetime, end: datetime) -> list[Tick]:
        return sorted(
            (
                tick
                for tick in self.ticks
                if tick.symbol == symbol and start <= tick.time < end
            ),
            key=lambda tick: tick.time,
        )


class FakeBarRepository:
    """MarketBarRepository in memory, mirroring the unique key: a bar already
    stored for a bucket is kept rather than overwritten.
    """

    def __init__(self, bars: Sequence[Bar] = ()) -> None:
        self.bars = list(bars)

    def insert_many(self, bars: Sequence[Bar]) -> int:
        stored = 0
        for bar in bars:
            key = (bar.symbol, bar.timeframe, bar.start)
            if any((b.symbol, b.timeframe, b.start) == key for b in self.bars):
                continue
            self.bars.append(bar)
            stored += 1
        return stored

    def known_before(
        self, symbol: str, timeframe: str, t: datetime, count: int
    ) -> list[Bar]:
        visible = [
            b
            for b in self.bars
            if b.symbol == symbol and b.timeframe == timeframe and b.known_at <= t
        ]
        return sorted(visible, key=lambda b: b.start)[-count:]


class FakeAccountSnapshotRepository:
    """AccountSnapshotRepository in memory, scoped per account and read by
    observed_at the way the stored series is.
    """

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, AccountSnapshot]] = []

    def insert(self, account_id: str, snapshot: AccountSnapshot) -> None:
        self.snapshots.append((account_id, snapshot))

    def known_before(
        self, account_id: str, t: datetime, since: datetime
    ) -> list[AccountSnapshot]:
        return [s for s in self._visible(account_id, t) if s.observed_at >= since]

    def latest_known_before(
        self, account_id: str, t: datetime
    ) -> AccountSnapshot | None:
        visible = self._visible(account_id, t)
        return visible[-1] if visible else None

    def _visible(self, account_id: str, t: datetime) -> list[AccountSnapshot]:
        return [s for s in self._of(account_id) if s.observed_at <= t]

    def _of(self, account_id: str) -> list[AccountSnapshot]:
        return sorted(
            (s for owner, s in self.snapshots if owner == account_id),
            key=lambda s: s.observed_at,
        )


class FakeDecisionRepository:
    """DecisionRepository in memory, scoped per account and keeping one row
    per signal the way the primary key does.
    """

    def __init__(self) -> None:
        self.trails: list[tuple[str, StrategySignal, PositionIntent, RiskDecision]] = []
        self.signals: list[tuple[str, StrategySignal]] = []

    def record(
        self,
        account_id: str,
        signal: StrategySignal,
        intent: PositionIntent,
        decision: RiskDecision,
    ) -> None:
        self.record_signal(account_id, signal)
        self.trails.append((account_id, signal, intent, decision))

    def record_signal(self, account_id: str, signal: StrategySignal) -> None:
        if all(s.signal_id != signal.signal_id for _, s in self.signals):
            self.signals.append((account_id, signal))

    def recent(
        self, account_id: str, limit: int
    ) -> list[tuple[StrategySignal, PositionIntent, RiskDecision]]:
        owned = [t[1:] for t in self.trails if t[0] == account_id]
        return list(reversed(owned))[:limit]


def make_snapshot(
    equity: str,
    observed_at: datetime = T0,
    high_water_mark: str | None = None,
    broker_connected: bool = True,
    margin: str = "0",
    margin_level: str | None = None,
    balance: str | None = None,
) -> AccountSnapshot:
    eq = Decimal(equity)
    hwm = Decimal(high_water_mark) if high_water_mark is not None else eq
    return AccountSnapshot(
        observed_at=observed_at,
        balance=Decimal(balance) if balance is not None else eq,
        equity=eq,
        margin=Decimal(margin),
        free_margin=eq,
        margin_level=Decimal(margin_level) if margin_level is not None else None,
        unrealized_pnl=Decimal(0),
        realized_pnl_day=Decimal(0),
        high_water_mark=hwm,
        drawdown_from_hwm=max(hwm - eq, Decimal(0)),
        broker_connected=broker_connected,
    )


def make_intent(
    action: PositionAction = PositionAction.OPEN,
    direction: PositionDirection = PositionDirection.SHORT,
    symbol: str = "USDJPY",
    protected: bool = True,
    target_quantity: str | None = "1000",
    delta_quantity: str | None = None,
) -> PositionIntent:
    protection = None
    if protected:
        protection = ProtectionSpec(
            stop_loss_price=Decimal("159.50"),
            take_profit_price=None,
            maximum_unprotected_seconds=30,
            source="STRATEGY",
        )
    return PositionIntent(
        intent_id=uuid4(),
        strategy_id="test_strategy",
        strategy_version="0.0.1",
        symbol=symbol,
        action=action,
        direction=direction,
        target_quantity=Decimal(target_quantity) if target_quantity else None,
        delta_quantity=Decimal(delta_quantity) if delta_quantity else None,
        protection=protection,
        reason_codes=["TEST"],
        generated_at=T0,
    )


def make_command(
    state: CommandState = CommandState.CREATED,
    side: ExecutionSide = ExecutionSide.SELL,
    action: PositionAction = PositionAction.OPEN,
    direction: PositionDirection = PositionDirection.SHORT,
    quantity: str = "1000",
    claim_expires_at: datetime | None = None,
    broker_request_started_at: datetime | None = None,
    broker_position_ticket: str | None = None,
    stop_loss: str | None = None,
) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=uuid4(),
        intent_id=uuid4(),
        idempotency_key=f"test-{uuid4().hex[:8]}",
        symbol="USDJPY",
        side=side,
        action=action,
        direction=direction,
        quantity=Decimal(quantity),
        stop_loss_price=Decimal(stop_loss) if stop_loss else None,
        state=state,
        claim_expires_at=claim_expires_at,
        broker_request_started_at=broker_request_started_at,
        broker_position_ticket=broker_position_ticket,
        created_at=T0,
    )


def make_event(
    known_at: datetime = T0,
    event_type: str = "market.tick",
    source: str = "test",
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source=source,
        retrieved_at=known_at,
        known_at=known_at,
    )
