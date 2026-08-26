"""通貨 leg 分解: LONG/SHORT 符号・pair 横断集計・triangle 近似（設計書 34.3）。"""
from decimal import Decimal

from tests.support import T0, eurusd_spec, gbpjpy_spec, gbpusd_spec, make_tick, usdjpy_spec
from trading.data.market import InMemoryMarketData
from trading.domain.exposure import OpenPositionExposure
from trading.domain.money import Currency
from trading.portfolio.exposure import CurrencyExposureService
from trading.risk.conversion import MarketQuoteConversionService


def service() -> CurrencyExposureService:
    market = InMemoryMarketData()
    # bid = ask = 150: mark の検証を換算の保守側選択と切り離す。
    market.add_tick(make_tick("150.000", "150.000", time=T0))
    return CurrencyExposureService(
        MarketQuoteConversionService(market, [usdjpy_spec()])
    )


def long_position(spec, units: str, price: str, stop: str | None = None):
    return OpenPositionExposure(
        spec=spec,
        signed_units=Decimal(units),
        mark_price=Decimal(price),
        stop_loss_price=Decimal(stop) if stop else None,
    )


def test_usdjpy_long_is_usd_long_jpy_short():
    snapshot = service().snapshot(
        [long_position(usdjpy_spec(), "1000", "150.000")], now=T0
    )
    usd = snapshot.currency_exposures[Currency.USD]
    jpy = snapshot.currency_exposures[Currency.JPY]
    assert usd.net_units == Decimal(1000)
    assert usd.net_value_account.amount == Decimal(150_000)
    assert jpy.net_units == Decimal(-150_000)
    assert jpy.net_value_account.amount == Decimal(-150_000)
    assert usd.gross_value_account.amount == Decimal(150_000)


def test_eurusd_short_is_usd_long_exposure():
    snapshot = service().snapshot(
        [long_position(eurusd_spec(), "-1000", "1.08000")], now=T0
    )
    eur = snapshot.currency_exposures[Currency.EUR]
    usd = snapshot.currency_exposures[Currency.USD]
    assert eur.net_units == Decimal(-1000)
    assert eur.net_value_account.amount == Decimal(-162_000)  # 1080 USD × 150
    assert usd.net_units == Decimal(1080)
    assert usd.net_value_account.amount == Decimal(162_000)


def test_common_usd_leg_nets_across_pairs():
    # USDJPY LONG と EURUSD SHORT は同じ USD LONG factor として合算される。
    snapshot = service().snapshot(
        [
            long_position(usdjpy_spec(), "1000", "150.000"),
            long_position(eurusd_spec(), "-1000", "1.08000"),
        ],
        now=T0,
    )
    usd = snapshot.currency_exposures[Currency.USD]
    assert usd.net_value_account.amount == Decimal(312_000)  # 150k + 162k
    assert usd.gross_value_account.amount == Decimal(312_000)


def test_triangle_decomposition_matches_the_synthetic_pair():
    # GBPJPY ≒ GBPUSD × USDJPY: leg 分解は合成側と直接側で同じ通貨 net になる
    # （pair 名の cluster ではなく通貨 leg で構造リスクを見る根拠）。
    direct = service().snapshot(
        [long_position(gbpjpy_spec(), "1000", "195.000")], now=T0
    )
    synthetic = service().snapshot(
        [
            long_position(gbpusd_spec(), "1000", "1.30000"),
            long_position(usdjpy_spec(), "1300", "150.000"),
        ],
        now=T0,
    )
    for currency in (Currency.GBP, Currency.JPY):
        assert direct.net_value(currency) == synthetic.net_value(currency)
    assert synthetic.net_value(Currency.USD) == Decimal(0)


def test_stop_risk_sums_in_account_currency():
    snapshot = service().snapshot(
        [
            long_position(usdjpy_spec(), "1000", "150.000", stop="149.900"),
            long_position(eurusd_spec(), "-1000", "1.08000", stop="1.08200"),
        ],
        now=T0,
    )
    # 100 JPY + (0.002 USD × 1000 = 2 USD × 150 = 300 JPY)
    assert snapshot.open_stop_risk.amount == Decimal(400)
    assert snapshot.open_stop_risk.currency is Currency.JPY
