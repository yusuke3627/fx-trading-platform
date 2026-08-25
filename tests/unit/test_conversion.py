"""口座通貨換算: use-time staleness / fail-close / 保守的 rate（設計書 34.1）。"""
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.support import T0, at, make_tick
from trading.data.market import InMemoryMarketData
from trading.domain.money import Currency, Money
from trading.risk.conversion import (
    CONVERSION_RATE_STALE,
    CONVERSION_RATE_UNAVAILABLE,
    ConversionPurpose,
    ConversionRateStaleError,
    ConversionRateUnavailableError,
    ConversionStress,
    MarketQuoteConversionService,
)

USD_100 = Money(amount=Decimal(100), currency=Currency.USD)


def service_with_tick(tick=None, **kwargs) -> MarketQuoteConversionService:
    market = InMemoryMarketData()
    if tick is not None:
        market.add_tick(tick)
    return MarketQuoteConversionService(market, **kwargs)


def test_identity_conversion_needs_no_quote():
    service = service_with_tick()
    result = service.convert(
        Money(amount=Decimal("123.45"), currency=Currency.JPY),
        Currency.JPY,
        now=T0,
        purpose=ConversionPurpose.RISK_INCREASING,
    )
    assert result.money == Money(amount=Decimal("123.45"), currency=Currency.JPY)
    assert result.trace.path == ()
    assert result.trace.max_leg_age_ms == 0


def test_direct_conversion_uses_ask_side():
    # 損失評価を過小にしない側: USD→JPY は JPY 額が大きくなる ask。
    service = service_with_tick(make_tick("150.000", "150.004", time=T0))
    result = service.convert(
        USD_100, Currency.JPY, now=T0, purpose=ConversionPurpose.RISK_INCREASING
    )
    assert result.money == Money(amount=Decimal("15000.400"), currency=Currency.JPY)
    assert result.trace.path == ("USDJPY",)
    assert result.trace.source_known_at == (T0,)
    assert result.trace.purpose is ConversionPurpose.RISK_INCREASING


def test_inverse_conversion_uses_one_over_bid():
    # JPY→USD は USD 額が大きくなる 1/bid。
    service = service_with_tick(make_tick("125.000", "125.004", time=T0))
    result = service.convert(
        Money(amount=Decimal(250), currency=Currency.JPY),
        Currency.USD,
        now=T0,
        purpose=ConversionPurpose.RISK_INCREASING,
    )
    assert result.money == Money(amount=Decimal(2), currency=Currency.USD)


def test_unapproved_path_rejected():
    service = service_with_tick(make_tick("150.000", "150.004", time=T0))
    with pytest.raises(ConversionRateUnavailableError):
        service.convert(
            Money(amount=Decimal(1), currency=Currency.GBP),
            Currency.JPY,
            now=T0,
            purpose=ConversionPurpose.MONITORING,
        )


def test_missing_quote_rejected_for_both_purposes():
    service = service_with_tick()
    for purpose in ConversionPurpose:
        with pytest.raises(ConversionRateUnavailableError) as exc:
            service.convert(USD_100, Currency.JPY, now=T0, purpose=purpose)
        assert exc.value.code == CONVERSION_RATE_UNAVAILABLE


def test_non_positive_price_rejected():
    service = service_with_tick(make_tick("0", "150.004", time=T0))
    with pytest.raises(ConversionRateUnavailableError):
        service.convert(
            USD_100, Currency.JPY, now=T0, purpose=ConversionPurpose.MONITORING
        )


def test_future_stamped_quote_rejected_even_for_monitoring():
    # 未来 timestamp は staleness ではなく source 異常。last-good に使わない。
    service = service_with_tick(make_tick("150.000", "150.004", time=at(seconds=10)))
    with pytest.raises(ConversionRateUnavailableError):
        service.convert(
            USD_100, Currency.JPY, now=T0, purpose=ConversionPurpose.MONITORING
        )


def test_stale_quote_fails_risk_increasing():
    service = service_with_tick(
        make_tick("150.000", "150.004", time=T0), max_quote_age_seconds=5.0
    )
    with pytest.raises(ConversionRateStaleError) as exc:
        service.convert(
            USD_100,
            Currency.JPY,
            now=T0 + timedelta(seconds=6),
            purpose=ConversionPurpose.RISK_INCREASING,
        )
    assert exc.value.code == CONVERSION_RATE_STALE


def test_age_at_threshold_is_still_fresh():
    service = service_with_tick(
        make_tick("150.000", "150.004", time=T0), max_quote_age_seconds=5.0
    )
    result = service.convert(
        USD_100,
        Currency.JPY,
        now=T0 + timedelta(seconds=5),
        purpose=ConversionPurpose.RISK_INCREASING,
    )
    assert result.trace.max_leg_age_ms == 5000


def test_stale_quote_haircut_for_monitoring():
    service = service_with_tick(
        make_tick("150.000", "150.004", time=T0),
        max_quote_age_seconds=5.0,
        stale_monitoring_haircut_pct=Decimal(1),
    )
    result = service.convert(
        USD_100,
        Currency.JPY,
        now=T0 + timedelta(seconds=6),
        purpose=ConversionPurpose.MONITORING,
    )
    # 15000.400 × 1.01 — stale の間はリスクを大きめに見積もる。
    assert result.money == Money(amount=Decimal("15150.404"), currency=Currency.JPY)


def test_stress_widens_the_rate():
    service = service_with_tick(make_tick("150.000", "150.004", time=T0))
    result = service.convert(
        USD_100,
        Currency.JPY,
        now=T0,
        purpose=ConversionPurpose.RISK_INCREASING,
        stress=ConversionStress(adverse_pct=Decimal(2)),
    )
    assert result.money == Money(amount=Decimal("15300.408"), currency=Currency.JPY)
