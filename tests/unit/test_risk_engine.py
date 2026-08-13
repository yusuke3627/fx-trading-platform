from decimal import Decimal

from trading.domain.position import PositionAction
from trading.domain.risk import EventRiskMode, KillSwitchLevel
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine

from tests.support import FixedClock, T0, at, make_intent, make_snapshot, make_tick, usdjpy_spec


def make_context(**overrides) -> PreTradeContext:
    values = dict(
        now=T0,
        execution_enabled=True,
        broker_connected=True,
        account_reconciled=True,
        quote=make_tick("158.840", "158.844", time=T0),
        instrument=usdjpy_spec(),
        account=make_snapshot("1000000"),
        snapshots=[make_snapshot("1000000", observed_at=at(hours=-25))],
        open_positions_count=0,
        symbol_exposure_units=Decimal("0"),
        event_mode=EventRiskMode.NORMAL,
        kill_switch=KillSwitchLevel.NONE,
        unknown_commands=0,
        stop_distance_pips=Decimal("10"),
        requested_quantity=Decimal("2000"),
    )
    values.update(overrides)
    return PreTradeContext(**values)


def enabled_config(**overrides) -> RiskConfig:
    values = dict(
        trading_enabled=True,
        max_units_per_symbol={"USDJPY": 10000},
    )
    values.update(overrides)
    return RiskConfig(**values)


def engine(config: RiskConfig) -> RiskEngine:
    return RiskEngine(config, FixedClock())


def test_approves_within_risk_budget():
    # equity 1,000,000 x 0.05% = 500 budget; 10 pips x 0.01 = 0.1 loss/unit
    # -> 5,000 units allowed; requested 2,000 approved as-is.
    decision = engine(enabled_config()).evaluate(make_intent(), make_context())
    assert decision.approved, decision.reject_codes
    assert decision.approved_quantity == Decimal("2000")


def test_minimum_broker_size_never_overrides_risk():
    # equity 50,000 -> budget 25 -> 250 units allowed < volume_min 1,000.
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(
            account=make_snapshot("50000"),
            snapshots=[make_snapshot("50000", observed_at=at(hours=-25))],
        ),
    )
    assert not decision.approved
    assert "MINIMUM_BROKER_SIZE_EXCEEDS_RISK" in decision.reject_codes


def test_broker_stop_loss_is_mandatory_for_new_positions():
    decision = engine(enabled_config()).evaluate(
        make_intent(protected=False), make_context()
    )
    assert not decision.approved
    assert "PROTECTION_REQUIRED" in decision.reject_codes


def test_trading_disabled_rejects_opens():
    decision = engine(enabled_config(trading_enabled=False)).evaluate(
        make_intent(), make_context()
    )
    assert not decision.approved
    assert "TRADING_ENABLED" in decision.reject_codes


def test_halt_new_order_blocks_open_but_allows_close():
    config = enabled_config()
    open_decision = engine(config).evaluate(
        make_intent(action=PositionAction.OPEN),
        make_context(kill_switch=KillSwitchLevel.HALT_NEW_ORDER),
    )
    assert not open_decision.approved

    close_decision = engine(config).evaluate(
        make_intent(action=PositionAction.CLOSE),
        make_context(kill_switch=KillSwitchLevel.HALT_NEW_ORDER),
    )
    assert close_decision.approved, close_decision.reject_codes


def test_emergency_blocks_everything():
    decision = engine(enabled_config()).evaluate(
        make_intent(action=PositionAction.CLOSE),
        make_context(kill_switch=KillSwitchLevel.EMERGENCY),
    )
    assert not decision.approved


def test_daily_loss_halt():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(
            account=make_snapshot("990000"),
            snapshots=[make_snapshot("1000000", observed_at=at(hours=-10))],
        ),
    )
    assert not decision.approved
    assert "DAILY_LOSS_WITHIN_LIMIT" in decision.reject_codes


def test_unknown_orders_halt_new_risk():
    decision = engine(enabled_config()).evaluate(
        make_intent(), make_context(unknown_commands=1)
    )
    assert not decision.approved
    assert "NO_UNKNOWN_ORDERS" in decision.reject_codes


def test_reduced_event_mode_halves_size():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(event_mode=EventRiskMode.REDUCED, requested_quantity=Decimal("5000")),
    )
    # 5,000 allowed -> halved to 2,500 -> quantized to 2,000.
    assert decision.approved
    assert decision.approved_quantity == Decimal("2000")


def test_stale_quote_rejected():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(quote=make_tick("158.840", "158.844", time=at(seconds=-30))),
    )
    assert not decision.approved
    assert "QUOTE_FRESH" in decision.reject_codes
