from decimal import Decimal

from tests.support import T0, FixedClock, at, make_intent, make_snapshot, make_tick, usdjpy_spec
from trading.domain.account import AccountMode
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine


def make_context(**overrides) -> PreTradeContext:
    values = {
        "now": T0,
        "execution_enabled": True,
        "broker_connected": True,
        "account_reconciled": True,
        "quote": make_tick("158.840", "158.844", time=T0),
        "instrument": usdjpy_spec(),
        "account": make_snapshot("1000000"),
        "snapshots": [make_snapshot("1000000", observed_at=at(hours=-25))],
        "open_positions_count": 0,
        "symbol_exposure_units": Decimal(0),
        "event_mode": EventRiskMode.NORMAL,
        "kill_switch": KillSwitchLevel.NONE,
        "unknown_commands": 0,
        "stop_distance_pips": Decimal(10),
        "requested_quantity": Decimal(2000),
    }
    values.update(overrides)
    return PreTradeContext(**values)


def enabled_config(**overrides) -> RiskConfig:
    values = {
        "trading_enabled": True,
        "max_units_per_symbol": {"USDJPY": 10000},
    }
    values.update(overrides)
    return RiskConfig(**values)


def engine(config: RiskConfig) -> RiskEngine:
    return RiskEngine(config, FixedClock())


def test_approves_within_risk_budget():
    # equity 1,000,000 x 0.05% = 500 budget; 10 pips x 0.01 = 0.1 loss/unit
    # -> 5,000 units allowed; requested 2,000 approved as-is.
    decision = engine(enabled_config()).evaluate(make_intent(), make_context())
    assert decision.approved, decision.reject_codes
    assert decision.approved_quantity == Decimal(2000)


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


def test_incident_state_does_not_block_exits():
    # Halting exits during an UNKNOWN / unreconciled incident would keep
    # market risk on the book until reconciliation finishes; these halts
    # apply to new risk only.
    decision = engine(enabled_config()).evaluate(
        make_intent(action=PositionAction.CLOSE),
        make_context(
            unknown_commands=1,
            untracked_fills=1,
            position_mismatch=True,
            account_reconciled=False,
        ),
    )
    assert decision.approved, decision.reject_codes


def test_execution_disabled_blocks_all_orders_including_exits():
    # execution_enabled=False signals a broken execution path (e.g. account
    # mode mismatch); even exits must not be built there.
    decision = engine(enabled_config()).evaluate(
        make_intent(action=PositionAction.CLOSE),
        make_context(execution_enabled=False),
    )
    assert not decision.approved
    assert "EXECUTION_ENABLED" in decision.reject_codes


def test_netting_net_reducing_open_not_blocked_by_position_cap():
    # NETTING with max_open_positions=1 and a SHORT book: a LONG that shrinks
    # |net| adds no broker position and must not be rejected by the cap.
    decision = engine(enabled_config()).evaluate(
        make_intent(direction=PositionDirection.LONG),
        make_context(
            open_positions_count=1,
            symbol_exposure_units=Decimal(-10000),
            requested_quantity=Decimal(2000),
            account_mode=AccountMode.NETTING,
        ),
    )
    assert decision.approved, decision.reject_codes


def test_hedging_opposite_direction_still_counts_against_cap():
    # On HEDGING the opposite-direction OPEN is a separate ticket: the cap
    # applies regardless of the net direction.
    decision = engine(enabled_config()).evaluate(
        make_intent(direction=PositionDirection.LONG),
        make_context(
            open_positions_count=1,
            symbol_exposure_units=Decimal(-10000),
            requested_quantity=Decimal(2000),
        ),
    )
    assert not decision.approved
    assert "MAX_OPEN_POSITIONS" in decision.reject_codes


def test_hedging_increase_counts_against_position_cap():
    # On MT5 hedging every order opens a new ticket — a same-direction
    # INCREASE adds a broker position exactly like an OPEN.
    decision = engine(enabled_config()).evaluate(
        make_intent(action=PositionAction.INCREASE, direction=PositionDirection.SHORT),
        make_context(
            open_positions_count=1,
            symbol_exposure_units=Decimal(-2000),
            symbol_gross_exposure_units=Decimal(2000),
            requested_quantity=Decimal(1000),
        ),
    )
    assert not decision.approved
    assert "MAX_OPEN_POSITIONS" in decision.reject_codes


def test_netting_increase_not_blocked_by_position_cap():
    # Netting merges an INCREASE into the single net position: no new broker
    # position is created, so the cap does not apply.
    decision = engine(enabled_config()).evaluate(
        make_intent(action=PositionAction.INCREASE, direction=PositionDirection.SHORT),
        make_context(
            open_positions_count=1,
            symbol_exposure_units=Decimal(-2000),
            requested_quantity=Decimal(1000),
            account_mode=AccountMode.NETTING,
        ),
    )
    assert decision.approved, decision.reject_codes


def test_netting_headroom_allows_net_reduction():
    # NETTING at the SHORT cap: a LONG order shrinks |net| and must not be
    # blocked by the unit cap.
    decision = engine(enabled_config()).evaluate(
        make_intent(direction=PositionDirection.LONG),
        make_context(
            symbol_exposure_units=Decimal(-10000),
            requested_quantity=Decimal(2000),
            account_mode=AccountMode.NETTING,
        ),
    )
    assert decision.approved, decision.reject_codes
    assert decision.approved_quantity == Decimal(2000)


def test_netting_reversal_open_capped_as_from_flat():
    # The OMS never crosses zero in one order: a reversal OPEN executes from
    # flat after the close leg, so its approved size must fit the cap alone
    # (max 2000 with net -1000 approves 2000, never max + |current| = 3000).
    decision = engine(enabled_config(max_units_per_symbol={"USDJPY": 2000})).evaluate(
        make_intent(direction=PositionDirection.LONG),
        make_context(
            symbol_exposure_units=Decimal(-1000),
            requested_quantity=Decimal(3000),
            account_mode=AccountMode.NETTING,
        ),
    )
    assert decision.approved, decision.reject_codes
    assert decision.approved_quantity == Decimal(2000)


def test_netting_same_direction_at_cap_rejected():
    decision = engine(enabled_config()).evaluate(
        make_intent(direction=PositionDirection.SHORT),
        make_context(
            symbol_exposure_units=Decimal(-10000),
            account_mode=AccountMode.NETTING,
        ),
    )
    assert not decision.approved
    assert "MINIMUM_BROKER_SIZE_EXCEEDS_RISK" in decision.reject_codes


def test_hedging_cap_is_gross_even_when_net_flat():
    # LONG 10k + SHORT 10k on hedging is net-flat but consumes margin on
    # both tickets: the unit cap bounds the GROSS sum, so a further order is
    # rejected.
    decision = engine(enabled_config()).evaluate(
        make_intent(direction=PositionDirection.LONG),
        make_context(
            symbol_exposure_units=Decimal(0),
            symbol_gross_exposure_units=Decimal(10000),
            requested_quantity=Decimal(2000),
        ),
    )
    assert not decision.approved
    assert "MINIMUM_BROKER_SIZE_EXCEEDS_RISK" in decision.reject_codes


def test_reduced_event_mode_halves_size():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(event_mode=EventRiskMode.REDUCED, requested_quantity=Decimal(5000)),
    )
    # 5,000 allowed -> halved to 2,500 -> quantized to 2,000.
    assert decision.approved
    assert decision.approved_quantity == Decimal(2000)


def test_unconfigured_symbol_limit_fails_closed():
    # Instrument independence: a symbol missing from max_units_per_symbol is
    # a config omission, never "unlimited".
    decision = engine(enabled_config(max_units_per_symbol={})).evaluate(
        make_intent(), make_context()
    )
    assert not decision.approved
    assert "SYMBOL_LIMIT_CONFIGURED" in decision.reject_codes


def test_unknown_margin_level_with_open_margin_rejected():
    # A missing snapshot value must never approve new risk.
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(account=make_snapshot("1000000", margin="50000")),
    )
    assert not decision.approved
    assert "MARGIN_BUFFER" in decision.reject_codes


def test_margin_level_above_threshold_passes():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(
            account=make_snapshot("1000000", margin="50000", margin_level="500")
        ),
    )
    assert decision.approved, decision.reject_codes


def test_stale_quote_rejected():
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(quote=make_tick("158.840", "158.844", time=at(seconds=-30))),
    )
    assert not decision.approved
    assert "QUOTE_FRESH" in decision.reject_codes


def test_quote_on_a_broker_clock_ahead_of_ours_is_still_fresh():
    # ADR-005: event time belongs to the broker's clock, which at OANDA Japan
    # runs three hours ahead of ours. Aging it against our now would make every
    # quote look future-dated and reject the lot.
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(
            quote=make_tick(
                "158.840", "158.844", time=at(hours=3), received_at=at(seconds=-1)
            )
        ),
    )
    assert decision.approved, decision.reject_codes


def test_quote_stale_on_our_clock_rejected_despite_a_recent_broker_time():
    # The mirror image: a reconnect can deliver a quote the broker stamped
    # moments ago but that only reached us much later. What matters is when it
    # became usable here.
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(
            quote=make_tick(
                "158.840", "158.844", time=at(hours=3), received_at=at(seconds=-30)
            )
        ),
    )
    assert not decision.approved
    assert "QUOTE_FRESH" in decision.reject_codes


def test_future_dated_quote_rejected():
    # A quote from the future is look-ahead (replay leak or broken broker
    # clock), not freshness.
    decision = engine(enabled_config()).evaluate(
        make_intent(),
        make_context(quote=make_tick("158.840", "158.844", time=at(seconds=30))),
    )
    assert not decision.approved
    assert "QUOTE_FRESH" in decision.reject_codes
