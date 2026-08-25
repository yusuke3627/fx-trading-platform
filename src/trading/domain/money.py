"""口座通貨換算の土台となる金額型。

risk domain の金額は通貨次元を型で持つ。JPY の risk budget と USD 建て損失の
ような異通貨の比較・加算をコードレビューではなく型エラーで止めるための型で、
通貨を持たない Decimal のままでよいのは ratio / units / price / indicator 値に
限る（設計書 v2.1 §10, §42）。
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Currency(StrEnum):
    """対象4通貨。ここに無い通貨の instrument はシステムに入れない。"""

    USD = "USD"
    JPY = "JPY"
    GBP = "GBP"
    EUR = "EUR"


class CurrencyMismatchError(ValueError):
    """異なる通貨どうしの演算。明示的な conversion を経ずには許さない。"""


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: Currency

    def add(self, other: Money) -> Money:
        if self.currency is not other.currency:
            raise CurrencyMismatchError(
                f"cannot add {other.currency} to {self.currency}"
            )
        return Money(amount=self.amount + other.amount, currency=self.currency)
