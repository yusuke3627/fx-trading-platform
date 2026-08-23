from decimal import Decimal

import pytest

from tests.support import usdjpy_spec
from trading.domain.account import AccountMode
from trading.domain.fill import ProtectionReason
from trading.domain.instrument import FillingMode
from trading.domain.order import ExecutionSide
from trading.execution.mt5 import mapper


def test_pip_size_by_quote_digits():
    assert mapper.pip_size_for_digits(3) == Decimal("0.01")
    assert mapper.pip_size_for_digits(2) == Decimal("0.01")
    assert mapper.pip_size_for_digits(5) == Decimal("0.0001")
    assert mapper.pip_size_for_digits(4) == Decimal("0.0001")


def test_unsupported_digits_rejected():
    with pytest.raises(ValueError):
        mapper.pip_size_for_digits(6)


def test_protection_reason_mapping():
    assert mapper.protection_reason_from(mapper.DEAL_REASON_SL) is ProtectionReason.STOP_LOSS
    assert mapper.protection_reason_from(mapper.DEAL_REASON_TP) is ProtectionReason.TAKE_PROFIT
    assert mapper.protection_reason_from(mapper.DEAL_REASON_SO) is ProtectionReason.STOP_OUT
    assert mapper.protection_reason_from(mapper.DEAL_REASON_CLIENT) is None
    assert mapper.protection_reason_from(None) is None


def test_account_mode_mapping():
    assert mapper.account_mode_from(0) is AccountMode.NETTING
    assert mapper.account_mode_from(1) is AccountMode.EXCHANGE
    assert mapper.account_mode_from(2) is AccountMode.HEDGING
    with pytest.raises(ValueError):
        mapper.account_mode_from(99)


def test_units_lots_roundtrip():
    contract = Decimal(1000)
    assert mapper.units_to_lots(Decimal(1500), contract) == 1.5
    assert mapper.lots_to_units(1.5, contract) == Decimal("1500.0")


def test_accepted_filling_from_broker_mask():
    assert mapper.accepted_filling_from_mask(mapper.SYMBOL_FILLING_IOC) == frozenset(
        {FillingMode.IMMEDIATE_OR_CANCEL}
    )
    assert mapper.accepted_filling_from_mask(mapper.SYMBOL_FILLING_FOK) == frozenset(
        {FillingMode.FILL_OR_KILL}
    )
    assert mapper.accepted_filling_from_mask(
        mapper.SYMBOL_FILLING_FOK | mapper.SYMBOL_FILLING_IOC
    ) == frozenset({FillingMode.FILL_OR_KILL, FillingMode.IMMEDIATE_OR_CANCEL})
    assert mapper.accepted_filling_from_mask(0) == frozenset()


def test_order_filling_prefers_fill_or_kill():
    both = frozenset({FillingMode.FILL_OR_KILL, FillingMode.IMMEDIATE_OR_CANCEL})
    assert mapper.order_filling_for(both) == mapper.ORDER_FILLING_FOK
    assert (
        mapper.order_filling_for(frozenset({FillingMode.IMMEDIATE_OR_CANCEL}))
        == mapper.ORDER_FILLING_IOC
    )
    with pytest.raises(ValueError):
        mapper.order_filling_for(frozenset())


def test_market_order_request_carries_a_filling_the_broker_accepts():
    """OANDA Japan accepts IOC only for USD/JPY; omitting type_filling makes
    MT5 default to FOK and the broker rejects the order (retcode 10030)."""
    spec = usdjpy_spec()
    assert spec.accepted_filling_modes == frozenset({FillingMode.IMMEDIATE_OR_CANCEL})

    request = mapper.market_order_request(
        symbol="USDJPY",
        side=ExecutionSide.BUY,
        units=spec.volume_min,
        spec=spec,
    )

    assert request["type_filling"] == mapper.ORDER_FILLING_IOC


def test_market_order_request_uses_fill_or_kill_when_the_broker_accepts_it():
    spec = usdjpy_spec(
        accepted_filling_modes=frozenset(
            {FillingMode.FILL_OR_KILL, FillingMode.IMMEDIATE_OR_CANCEL}
        )
    )

    request = mapper.market_order_request(
        symbol="USDJPY",
        side=ExecutionSide.BUY,
        units=spec.volume_min,
        spec=spec,
    )

    assert request["type_filling"] == mapper.ORDER_FILLING_FOK
