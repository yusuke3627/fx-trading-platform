"""通貨 leg への exposure 分解 service（設計書 v2.1 §20–21）。

EURUSD SHORT と GBPUSD SHORT と USDJPY LONG は「独立した 3 trade」ではなく
共通の USD LONG factor であり、それを検出できる単位が currency exposure。

口座通貨 mark は各 leg を「そのペア自身の quote 通貨建て」で評価してから
quote → 口座通貨へ換算する（base leg = U × P も quote 建て）。対象 4 ペアの
quote は USD / JPY のみなので、承認済み path（USDJPY 直接・逆数）だけで全通貨
の mark が成立し、EUR→JPY のような multi-leg path を必要としない。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trading.domain.exposure import (
    CurrencyExposure,
    OpenPositionExposure,
    PortfolioRiskSnapshot,
)
from trading.domain.money import Currency, Money
from trading.risk.conversion import (
    AccountCurrencyConversionService,
    ConversionPurpose,
)


class CurrencyExposureService:
    def __init__(
        self,
        conversion: AccountCurrencyConversionService,
        account_currency: Currency = Currency.JPY,
    ) -> None:
        self._conversion = conversion
        self._account_currency = account_currency

    def snapshot(
        self, positions: Sequence[OpenPositionExposure], now: datetime
    ) -> PortfolioRiskSnapshot:
        """既存 book の評価。監視目的なので conversion は MONITORING で呼び、
        stale quote でも haircut 付きで値を返す（ADR-009 / ADR-010）。"""
        net_units: dict[Currency, Decimal] = {}
        net_value: dict[Currency, Decimal] = {}
        gross_value: dict[Currency, Decimal] = {}
        stop_risk = Decimal(0)

        for position in positions:
            spec = position.spec
            units = position.signed_units
            quote_value = units * position.mark_price

            # BASE leg: +U units。quote 建て価値 U×P を口座通貨へ。
            base_account = self._to_account(quote_value, spec.quote_currency, now)
            self._add(net_units, spec.base_currency, units)
            self._add(net_value, spec.base_currency, base_account)
            self._add(gross_value, spec.base_currency, abs(base_account))

            # QUOTE leg: -U×P units（quote 通貨そのもの）。
            self._add(net_units, spec.quote_currency, -quote_value)
            self._add(net_value, spec.quote_currency, -base_account)
            self._add(gross_value, spec.quote_currency, abs(base_account))

            if position.stop_loss_price is not None:
                loss_quote = abs(position.mark_price - position.stop_loss_price) * abs(
                    units
                )
                stop_risk += self._to_account(loss_quote, spec.quote_currency, now)

        exposures = {
            currency: CurrencyExposure(
                currency=currency,
                net_units=net_units[currency],
                net_value_account=Money(
                    amount=net_value[currency], currency=self._account_currency
                ),
                gross_value_account=Money(
                    amount=gross_value[currency], currency=self._account_currency
                ),
            )
            for currency in net_units
        }
        return PortfolioRiskSnapshot(
            open_stop_risk=Money(amount=stop_risk, currency=self._account_currency),
            currency_exposures=exposures,
            known_at=now,
        )

    def _to_account(self, amount: Decimal, currency: Currency, now: datetime) -> Decimal:
        return self._conversion.convert(
            Money(amount=amount, currency=currency),
            self._account_currency,
            now=now,
            purpose=ConversionPurpose.MONITORING,
        ).money.amount

    @staticmethod
    def _add(bucket: dict[Currency, Decimal], currency: Currency, value: Decimal) -> None:
        bucket[currency] = bucket.get(currency, Decimal(0)) + value
