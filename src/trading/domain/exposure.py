"""通貨 leg exposure のモデル（設計書 v2.1 §20–21）。

pair 単位の position を base / quote の通貨 leg に分解した結果。分解を行う
service は portfolio 層（`portfolio/exposure.py`）にあり、risk 層は本モデル
だけを読む。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from trading.domain.instrument import InstrumentSpec
from trading.domain.money import Currency, Money


class OpenPositionExposure(BaseModel):
    """分解の入力 1 件。provider（backtest simulator / ledger）が組み立てる。"""

    model_config = ConfigDict(frozen=True)

    spec: InstrumentSpec
    # +LONG / -SHORT の base 通貨 units。
    signed_units: Decimal
    # 現在の mark price（QUOTE per BASE）。
    mark_price: Decimal
    # broker 側 SL。無い position は stop risk 0 として扱う（protection は
    # 別の不変条件が保証しており、ここでは合計値の過小を招かない前提）。
    stop_loss_price: Decimal | None = None


class CurrencyExposure(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: Currency
    net_units: Decimal
    net_value_account: Money
    gross_value_account: Money


class PortfolioRiskSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    # 全 open position の stop 到達時損失の合計（口座通貨）。
    open_stop_risk: Money
    currency_exposures: Mapping[Currency, CurrencyExposure]
    known_at: datetime

    def net_value(self, currency: Currency) -> Decimal:
        exposure = self.currency_exposures.get(currency)
        return exposure.net_value_account.amount if exposure else Decimal(0)
