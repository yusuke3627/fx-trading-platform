from decimal import Decimal

import pytest

from trading.domain.account import AccountMode
from trading.domain.fill import ProtectionReason
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
