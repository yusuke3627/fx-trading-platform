"""口座通貨への換算サービス。

risk domain は生の conversion rate を扱わない: 換算はこのサービスだけが行い、
呼び出し側は Money を渡して Money を受け取る（ADR-008）。staleness は DTO に
保存せず、convert() の呼び出し時点（use-time）で毎回評価する。stale な rate で
リスクを増やす行為（新規・増し玉の sizing）は fail-close し、既存 position の
監視は stale haircut 付きの last-good rate を許す（ADR-009 / ADR-010）。

換算は承認済み path のみ。自動の path 探索は価格 source の混在と staleness の
連鎖を招くため行わない（設計書 v2.1 §10.1）。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from trading.data.market import MarketDataService
from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Tick
from trading.domain.money import Currency, Money

CONVERSION_RATE_UNAVAILABLE = "CONVERSION_RATE_UNAVAILABLE"
CONVERSION_RATE_STALE = "CONVERSION_RATE_STALE"


class ConversionPurpose(StrEnum):
    """リスクを増やす換算か、既存リスクの監視のための換算か。

    RISK_INCREASING は新規 entry / size increase の sizing。rate が無い・stale・
    整合性不良なら失敗させ、取引しないことで安全側に倒す。
    MONITORING は既存 position の評価。止めると reduce / exit の判断まで
    失えるため、stale でも haircut 付きで値を返す。
    """

    RISK_INCREASING = "RISK_INCREASING"
    MONITORING = "MONITORING"


class ConversionError(Exception):
    code: str = CONVERSION_RATE_UNAVAILABLE


class ConversionRateUnavailableError(ConversionError):
    code = CONVERSION_RATE_UNAVAILABLE


class ConversionRateStaleError(ConversionError):
    code = CONVERSION_RATE_STALE


class ConversionStress(BaseModel):
    """direction 条件付き adverse conversion shock の interface。

    当面は決定論的な conservative floor（一律の adverse %）のみ。pair /
    direction / stop horizon の historical conditional quantile からの推定は
    portfolio exposure 対応（#58）で実装し、この型の生成元だけが変わる。
    """

    model_config = ConfigDict(frozen=True)

    # adverse は常に損失評価を「広げる」契約。負値はその契約を反転させ
    # sizing を過大にするため境界で拒否する。
    adverse_pct: Decimal = Field(ge=0, allow_inf_nan=False)


class ConversionTrace(BaseModel):
    """監査用の換算根拠。risk 計算側はこれを読まず Money だけを使う。"""

    model_config = ConfigDict(frozen=True)

    path: tuple[str, ...]
    source_known_at: tuple[datetime, ...]
    max_leg_age_ms: int
    purpose: ConversionPurpose


class ConversionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    money: Money
    trace: ConversionTrace


class AccountCurrencyConversionService(Protocol):
    def convert(
        self,
        money: Money,
        to_currency: Currency,
        now: datetime,
        purpose: ConversionPurpose,
        stress: ConversionStress | None = None,
    ) -> ConversionResult: ...


_HUNDRED = Decimal(100)


class MarketQuoteConversionService:
    """market data の fresh quote に基づく換算。

    損失評価が過小にならない側の rate を使う: 直接 quote は ask（変換先の
    金額が大きくなる側）、inverse は 1/bid。demo での broker PnL との差分
    計測で buffer を校正するまで、既定値は保守側の仮置き。
    """

    def __init__(
        self,
        market: MarketDataService,
        conversion_instruments: Iterable[InstrumentSpec] = (),
        max_quote_age_seconds: float = 5.0,
        stale_monitoring_haircut_pct: Decimal = Decimal(1),
    ) -> None:
        self._market = market
        self._max_quote_age_seconds = max_quote_age_seconds
        self._stale_monitoring_haircut_pct = stale_monitoring_haircut_pct
        # 承認 path は注入された InstrumentSpec から構成する: 直接 quote が
        # 立っているペアとその逆数のみ。symbol はハードコードしない — broker
        # alias（"USDJPY.oj" 等）では market data がその名前で保存されるため、
        # 固定文字列は常時 quote 欠損 = 全換算停止になる。EUR→JPY のような
        # multi-leg はここに現れず ConversionRateUnavailableError になる
        # （必要になった時点で承認 instrument を足す）。
        self._paths: dict[tuple[Currency, Currency], tuple[str, bool]] = {}
        for spec in conversion_instruments:
            self._paths[(spec.base_currency, spec.quote_currency)] = (spec.symbol, False)
            self._paths[(spec.quote_currency, spec.base_currency)] = (spec.symbol, True)

    def convert(
        self,
        money: Money,
        to_currency: Currency,
        now: datetime,
        purpose: ConversionPurpose,
        stress: ConversionStress | None = None,
    ) -> ConversionResult:
        if money.currency is to_currency:
            return ConversionResult(
                money=Money(amount=money.amount, currency=to_currency),
                trace=ConversionTrace(
                    path=(),
                    source_known_at=(),
                    max_leg_age_ms=0,
                    purpose=purpose,
                ),
            )

        path = self._paths.get((money.currency, to_currency))
        if path is None:
            raise ConversionRateUnavailableError(
                f"no approved conversion path {money.currency} -> {to_currency}"
            )
        symbol, inverse = path

        tick = self._market.latest_tick(symbol)
        if tick is None:
            raise ConversionRateUnavailableError(f"no quote for {symbol}")

        age_seconds = (now - tick.known_time).total_seconds()
        self._check_integrity(tick, age_seconds, symbol)

        rate = self._conservative_rate(tick, inverse)
        stale = age_seconds > self._max_quote_age_seconds
        if stale:
            if purpose is ConversionPurpose.RISK_INCREASING:
                raise ConversionRateStaleError(
                    f"{symbol} quote is {age_seconds:.1f}s old "
                    f"(max {self._max_quote_age_seconds}s)"
                )
            rate *= 1 + self._stale_monitoring_haircut_pct / _HUNDRED
        if stress is not None:
            rate *= 1 + stress.adverse_pct / _HUNDRED

        return ConversionResult(
            money=Money(amount=money.amount * rate, currency=to_currency),
            trace=ConversionTrace(
                path=(symbol,),
                source_known_at=(tick.known_time,),
                max_leg_age_ms=int(age_seconds * 1000),
                purpose=purpose,
            ),
        )

    def _check_integrity(self, tick: Tick, age_seconds: float, symbol: str) -> None:
        # 未来の timestamp や非正値の価格は staleness ではなく source の異常。
        # monitoring であっても信用できる last-good ではないため失敗させる。
        if age_seconds < 0:
            raise ConversionRateUnavailableError(
                f"{symbol} quote is stamped in the future"
            )
        if tick.bid <= 0 or tick.ask <= 0:
            raise ConversionRateUnavailableError(
                f"{symbol} quote has non-positive price bid={tick.bid} ask={tick.ask}"
            )
        # crossed quote（bid > ask）は feed の破損。保守側として選ぶはずの
        # ask / 1/bid がどちらも小さい側になり損失を過小評価するため拒否する。
        if tick.bid > tick.ask:
            raise ConversionRateUnavailableError(
                f"{symbol} quote is crossed bid={tick.bid} ask={tick.ask}"
            )

    def _conservative_rate(self, tick: Tick, inverse: bool) -> Decimal:
        if inverse:
            return Decimal(1) / tick.bid
        return tick.ask
