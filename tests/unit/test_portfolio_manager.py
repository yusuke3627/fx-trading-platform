from decimal import Decimal
from uuid import uuid4

from tests.support import T0, FixedClock, make_tick, usdjpy_spec
from trading.data.market import InMemoryMarketData
from trading.domain.money import Currency
from trading.domain.position import PositionAction, PositionDirection, VirtualPosition
from trading.domain.signal import StrategySignal
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService


def make_signal(
    direction: PositionDirection = PositionDirection.SHORT,
    stop_pips: str = "10",
    symbol: str = "USDJPY",
    exit_only: bool = False,
) -> StrategySignal:
    return StrategySignal(
        signal_id=uuid4(),
        strategy_id="test_strategy",
        strategy_version="0.0.1",
        symbol=symbol,
        desired_direction=direction,
        conviction=0.5,
        expected_horizon_seconds=3600,
        stop_distance_pips=Decimal(stop_pips),
        reason_codes=["TEST"],
        exit_only=exit_only,
        generated_at=T0,
    )


def sizing(**overrides) -> SizingInput:
    values = {
        "equity": Decimal(1_000_000),
        "max_risk_per_trade_pct": Decimal("0.05"),
        "pip_size": Decimal("0.01"),
        "quote_currency": Currency.JPY,
        "volume_step": Decimal(1000),
        "entry_price": Decimal("158.840"),
    }
    values.update(overrides)
    return SizingInput(**values)


def manager_with(
    *positions: VirtualPosition, market: InMemoryMarketData | None = None
) -> PortfolioManager:
    ledger = VirtualPositionLedger(FixedClock())
    for p in positions:
        ledger.record(p)
    return PortfolioManager(
        ledger,
        FixedClock(),
        MarketQuoteConversionService(market or InMemoryMarketData(), [usdjpy_spec()]),
    )


def held(direction: PositionDirection) -> VirtualPosition:
    return VirtualPosition(
        strategy_id="test_strategy",
        symbol="USDJPY",
        direction=direction,
        quantity=Decimal(1000),
        as_of=T0,
    )


def test_open_intent_sized_by_risk_budget():
    # 1,000,000 x 0.05% = 500 budget; 10 pips x 0.01 = 0.1 loss/unit -> 5,000.
    intents = manager_with().intents_from_signal(make_signal(), sizing())
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action is PositionAction.OPEN
    assert intent.target_quantity == Decimal(5000)
    # SHORT stop sits above the entry price.
    assert intent.protection is not None
    assert intent.protection.stop_loss_price == Decimal("158.940")


def test_long_stop_sits_below_entry():
    intents = manager_with().intents_from_signal(
        make_signal(direction=PositionDirection.LONG), sizing()
    )
    assert intents[0].protection.stop_loss_price == Decimal("158.740")


def eurusd_sizing() -> SizingInput:
    return SizingInput(
        equity=Decimal(1_000_000),
        max_risk_per_trade_pct=Decimal("0.05"),
        pip_size=Decimal("0.0001"),
        quote_currency=Currency.USD,
        volume_step=Decimal(1000),
        entry_price=Decimal("1.08000"),
    )


def test_usd_quote_loss_is_converted_before_sizing():
    # EURUSD: budget 500 JPY; 20 pips × 0.0001 = 0.002 USD/unit。USDJPY ask
    # 150 で 0.3 JPY/unit → 1,666 → 1,000 units。修正前は USD の 0.002 を
    # そのまま JPY budget と比較して 250,000 units（150 倍超の over-size）
    # を許していた。
    market = InMemoryMarketData()
    market.add_tick(make_tick("149.996", "150.000", time=T0))
    intents = manager_with(market=market).intents_from_signal(
        make_signal(symbol="EURUSD", stop_pips="20"), eurusd_sizing()
    )
    assert len(intents) == 1
    assert intents[0].target_quantity == Decimal(1000)
    assert intents[0].target_quantity != Decimal(250_000)


def test_no_conversion_quote_emits_an_unsized_intent_for_risk_to_reject():
    # USDJPY quote が無ければ size できないが、intent は消さない: size 未定の
    # まま RiskEngine へ届き、CONVERSION_RATE_* が決定記録に残る（ADR-009）。
    intents = manager_with().intents_from_signal(
        make_signal(symbol="EURUSD", stop_pips="20"), eurusd_sizing()
    )
    assert len(intents) == 1
    assert intents[0].action is PositionAction.OPEN
    assert intents[0].target_quantity is None


def test_conversion_failure_keeps_the_reversal_close():
    # 反転シグナル時に換算が失敗しても、既存 position の CLOSE は生成される
    # （ADR-010: リスク削減は換算欠損で止めない）。抑止されるのは新規 OPEN の
    # size だけで、それも size 未定 intent として RiskEngine の判定に委ねる。
    held_long = VirtualPosition(
        strategy_id="test_strategy",
        symbol="EURUSD",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        as_of=T0,
    )
    intents = manager_with(held_long).intents_from_signal(
        make_signal(symbol="EURUSD", stop_pips="20"), eurusd_sizing()
    )
    assert [i.action for i in intents] == [PositionAction.CLOSE, PositionAction.OPEN]
    assert intents[1].target_quantity is None


def test_same_direction_becomes_increase():
    manager = manager_with(held(PositionDirection.SHORT))
    intents = manager.intents_from_signal(make_signal(), sizing())
    assert len(intents) == 1
    assert intents[0].action is PositionAction.INCREASE


def test_direction_flip_closes_then_opens():
    # The reversal must survive: CLOSE what exists, then OPEN the desired
    # side. Collapsing it into a bare exit would lose the deduped signal.
    manager = manager_with(held(PositionDirection.LONG))
    intents = manager.intents_from_signal(make_signal(), sizing())
    assert [i.action for i in intents] == [PositionAction.CLOSE, PositionAction.OPEN]

    close, reopen = intents
    # The close carries the held direction: CLOSE LONG (via SELL).
    assert close.direction is PositionDirection.LONG
    assert close.target_quantity == Decimal(0)
    assert close.protection is None

    assert reopen.direction is PositionDirection.SHORT
    assert reopen.target_quantity == Decimal(5000)
    assert reopen.protection is not None


def test_exit_only_signal_closes_the_held_position_without_reopening():
    intents = manager_with(held(PositionDirection.LONG)).intents_from_signal(
        make_signal(direction=PositionDirection.SHORT, exit_only=True), sizing()
    )

    assert [intent.action for intent in intents] == [PositionAction.CLOSE]
    assert intents[0].direction is PositionDirection.LONG
    assert intents[0].target_quantity == Decimal(0)
    assert intents[0].protection is None


def test_exit_only_signal_without_a_position_yields_nothing():
    assert manager_with().intents_from_signal(make_signal(exit_only=True), sizing()) == []

    zero_position = held(PositionDirection.LONG).model_copy(update={"quantity": Decimal(0)})
    assert manager_with(zero_position).intents_from_signal(
        make_signal(exit_only=True), sizing()
    ) == []


def test_exit_only_close_survives_conversion_failure():
    held_long = VirtualPosition(
        strategy_id="test_strategy",
        symbol="EURUSD",
        direction=PositionDirection.LONG,
        quantity=Decimal(1000),
        as_of=T0,
    )
    intents = manager_with(held_long).intents_from_signal(
        make_signal(symbol="EURUSD", exit_only=True), eurusd_sizing()
    )

    assert [intent.action for intent in intents] == [PositionAction.CLOSE]


def test_no_intents_without_stop_distance():
    assert manager_with().intents_from_signal(make_signal(stop_pips="0"), sizing()) == []
