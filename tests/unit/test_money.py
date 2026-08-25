"""Money / Currency 型: 通貨次元を型で守る（設計書 v2.1 34.1A）。"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.domain.money import Currency, CurrencyMismatchError, Money


def test_same_currency_add():
    total = Money(amount=Decimal("100.5"), currency=Currency.JPY).add(
        Money(amount=Decimal("0.5"), currency=Currency.JPY)
    )
    assert total == Money(amount=Decimal("101.0"), currency=Currency.JPY)


def test_cross_currency_add_raises():
    jpy = Money(amount=Decimal(100), currency=Currency.JPY)
    usd = Money(amount=Decimal(100), currency=Currency.USD)
    with pytest.raises(CurrencyMismatchError):
        jpy.add(usd)


def test_money_is_frozen():
    money = Money(amount=Decimal(1), currency=Currency.USD)
    with pytest.raises(ValidationError):
        money.amount = Decimal(2)


def test_unsupported_currency_rejected():
    with pytest.raises(ValueError):
        Currency("CHF")
