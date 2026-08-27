"""MacroFactorSeries: PIT の vintage 連鎖から factor の観測列を作る。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tests.support import FakeObservationRepository
from trading.data.factor_series import (
    DEFAULT_FACTOR_INPUTS,
    FactorInput,
    MacroFactorSeries,
    SeriesTransform,
)
from trading.data.macro.registry import (
    UK_CPI_HEADLINE_YOY_NSA,
    US_CPI_HEADLINE_SA,
    US_TREASURY_2Y_YIELD,
    US_UNEMPLOYMENT_RATE_SA,
)
from trading.domain.economic import EconomicObservation
from trading.domain.money import Currency
from trading.intelligence.currency import CurrencyFactor
from trading.intelligence.normalization import NormalizationConfig, normalize_series

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
CONFIG = NormalizationConfig()

# 連鎖の最終観測期間。初報は月末の 40 日後に届くので、2026-07 の初報が
# NOW の直前に入る。
LAST_MONTH = 2026 * 12 + 6

RATES_ONLY = {(Currency.USD, CurrencyFactor.RATES): FactorInput(US_TREASURY_2Y_YIELD, 1)}


def month_label(month_index: int) -> str:
    return f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"


def first_print_at(month_index: int) -> datetime:
    year, month = month_index // 12, month_index % 12 + 1
    return datetime(year, month, 1, tzinfo=UTC) + timedelta(days=40)


def observation(
    series: str,
    period: str,
    value: str,
    known_at: datetime,
    unit: str = "index",
) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series=series,
        observation_period=period,
        value=Decimal(value),
        unit=unit,
        source="TEST",
        retrieved_at=known_at,
        known_at=known_at,
    )


def monthly_chain(
    series: str, values: list[str], unit: str = "index"
) -> list[EconomicObservation]:
    """初報だけの月次連鎖。最後の値が LAST_MONTH の観測になる。"""
    start = LAST_MONTH - len(values) + 1
    return [
        observation(
            series, month_label(start + offset), value, first_print_at(start + offset), unit
        )
        for offset, value in enumerate(values)
    ]


def index_at(annual_rates: list[Decimal]) -> list[str]:
    """月次指数の水準列。各月は 12 か月前のちょうど `annual_rates[年]` 倍。

    前年同月比が年ごとにきっかりその率になるので、変換後の系列を厳密な
    値で語れる。最初の 1 年は季節性のある出発点。
    """
    levels: list[Decimal] = []
    for index in range(len(annual_rates) * 12):
        if index < 12:
            levels.append(Decimal(100 + index))
        else:
            levels.append(levels[index - 12] * annual_rates[index // 12])
    return [str(level) for level in levels]


def source(observations, inputs=DEFAULT_FACTOR_INPUTS) -> MacroFactorSeries:
    return MacroFactorSeries(FakeObservationRepository(observations), CONFIG, inputs)


class RecordingObservations(FakeObservationRepository):
    def __init__(self, observations: list[EconomicObservation] | tuple = ()) -> None:
        super().__init__(observations)
        self.calls: list[tuple[str, datetime, datetime]] = []

    def known_before(
        self, series: str, t: datetime, since: datetime
    ) -> list[EconomicObservation]:
        self.calls.append((series, t, since))
        return super().known_before(series, t, since)


def test_level_series_keeps_the_published_value() -> None:
    rows = monthly_chain(US_TREASURY_2Y_YIELD, ["4.10", "4.25"], unit="percent")

    series = source(rows, RATES_ONLY).series(Currency.USD, CurrencyFactor.RATES, NOW)

    assert [value for _, value in series] == [4.10, 4.25]


def test_unemployment_is_inverted_so_higher_means_a_stronger_currency() -> None:
    rows = monthly_chain(US_UNEMPLOYMENT_RATE_SA, ["3.8", "4.4"], unit="percent")

    series = source(rows).series(Currency.USD, CurrencyFactor.GROWTH, NOW)

    # 失業率の上昇は通貨にとって弱材料。符号を揃えないと base - quote の
    # 減算で GROWTH だけ逆向きに効く。
    assert [value for _, value in series] == [-3.8, -4.4]


def test_index_becomes_year_over_year_percent() -> None:
    values = [f"{100 + index:.4f}" for index in range(12)]
    values.append("105.0000")
    rows = monthly_chain(US_CPI_HEADLINE_SA, values)

    series = source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW)

    # 相手のいない最初の 12 か月は落ち、13 か月目だけが残る。
    assert len(series) == 1
    at, value = series[0]
    assert value == 5.0
    assert at == rows[-1].known_at


def test_year_over_year_pairs_against_the_first_print_of_a_year_earlier() -> None:
    values = [f"{100 + index:.4f}" for index in range(24)]
    rows = monthly_chain(US_CPI_HEADLINE_SA, values)

    series = source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW)

    # 12 か月目以降だけが値を持ち、known_at は新しい側の初報。
    assert len(series) == 12
    assert series[0][0] == rows[12].known_at


def test_a_revision_does_not_replace_the_first_print() -> None:
    rows = monthly_chain(US_TREASURY_2Y_YIELD, ["4.10", "4.25"], unit="percent")
    revision = observation(
        US_TREASURY_2Y_YIELD,
        rows[0].observation_period,
        "9.99",
        rows[-1].known_at + timedelta(days=1),
        unit="percent",
    )

    series = source([*rows, revision], RATES_ONLY).series(
        Currency.USD, CurrencyFactor.RATES, NOW
    )

    # 改定を採ると、古い期間の値が known_at 最新の点になり、正規化が
    # それを「直近値」として z を取る。
    assert [value for _, value in series] == [4.10, 4.25]


def test_an_observation_known_after_now_is_not_visible() -> None:
    rows = monthly_chain(US_TREASURY_2Y_YIELD, ["4.10"], unit="percent")
    future = observation(
        US_TREASURY_2Y_YIELD,
        month_label(LAST_MONTH + 1),
        "5.00",
        NOW + timedelta(days=1),
        unit="percent",
    )

    series = source([*rows, future], RATES_ONLY).series(
        Currency.USD, CurrencyFactor.RATES, NOW
    )

    assert [value for _, value in series] == [4.10]


def test_a_factor_without_a_series_is_absent_not_neutral() -> None:
    assert source(()).series(Currency.JPY, CurrencyFactor.INFLATION, NOW) == ()


def test_year_over_year_skips_a_period_whose_base_is_missing() -> None:
    values = [f"{100 + index:.4f}" for index in range(13)]
    rows = monthly_chain(US_CPI_HEADLINE_SA, values)
    without_base = rows[1:]

    series = source(without_base).series(Currency.USD, CurrencyFactor.INFLATION, NOW)

    assert series == []


def test_year_over_year_skips_a_zero_base_instead_of_dividing() -> None:
    values = ["0.0000", *[f"{100 + index:.4f}" for index in range(1, 13)]]
    rows = monthly_chain(US_CPI_HEADLINE_SA, values)

    series = source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW)

    assert series == []


def test_the_read_window_reaches_a_year_past_the_normalization_window() -> None:
    repository = RecordingObservations(())
    MacroFactorSeries(repository, CONFIG).series(
        Currency.USD, CurrencyFactor.INFLATION, NOW
    )

    _, _, since = repository.calls[0]
    # 月次 60 本の窓 + 前年同月比の 12 本 + 余裕 12 本。
    assert (NOW - since).days / 30.44 >= CONFIG.window + 12


def test_the_read_window_scales_with_the_configured_window() -> None:
    repository = RecordingObservations(())
    MacroFactorSeries(repository, NormalizationConfig(window=120)).series(
        Currency.USD, CurrencyFactor.INFLATION, NOW
    )

    _, _, since = repository.calls[0]
    assert (NOW - since).days / 30.44 >= 120 + 12


def test_a_constant_year_over_year_has_nothing_to_say() -> None:
    # 8 年ぶん、毎年きっちり 2% で伸びる指数。インフレの「ニュース」は無い。
    rows = monthly_chain(US_CPI_HEADLINE_SA, index_at([Decimal("1.02")] * 8))

    raw = normalize_series(
        [(row.known_at, float(row.value)) for row in rows], NOW, CONFIG
    )
    transformed = normalize_series(
        source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW), NOW, CONFIG
    )

    # 生の指数は直近値が常に窓の最大側に来るので、動きが無くても強気に読める。
    assert raw is not None
    assert raw.value > 0
    # 前年同月比は一定なので散らばりが無く、「語れない」= coverage 不足。
    # 中立に潰さず confidence を下げるのが CurrencyState 側の扱い。
    assert transformed is None


def test_disinflation_reads_negative_only_after_the_transform() -> None:
    # 2% 前後で 6 年、直近 1 年だけ 0.5% へ減速する。水準は伸び続ける。
    rates = [
        Decimal("1.02"),
        Decimal("1.02"),
        Decimal("1.025"),
        Decimal("1.018"),
        Decimal("1.022"),
        Decimal("1.021"),
        Decimal("1.019"),
        Decimal("1.005"),
    ]
    rows = monthly_chain(US_CPI_HEADLINE_SA, index_at(rates))

    raw = normalize_series(
        [(row.known_at, float(row.value)) for row in rows], NOW, CONFIG
    )
    transformed = normalize_series(
        source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW), NOW, CONFIG
    )

    # 指数の水準は下がらないので、生の系列では減速局面すら買い材料になる。
    assert raw is not None
    assert raw.value > 0
    assert transformed is not None
    assert transformed.value < Decimal("-0.5")


def test_year_over_year_reacts_to_an_acceleration_in_the_last_year() -> None:
    # 2% 前後で 6 年、直近 1 年だけ 6% へ加速する。
    rates = [
        Decimal("1.02"),
        Decimal("1.02"),
        Decimal("1.025"),
        Decimal("1.018"),
        Decimal("1.022"),
        Decimal("1.021"),
        Decimal("1.019"),
        Decimal("1.06"),
    ]
    rows = monthly_chain(US_CPI_HEADLINE_SA, index_at(rates))

    score = normalize_series(
        source(rows).series(Currency.USD, CurrencyFactor.INFLATION, NOW), NOW, CONFIG
    )

    assert score is not None
    assert score.value > Decimal("0.5")
    assert score.observations == CONFIG.window


def test_gbp_inflation_reads_the_published_year_over_year_series() -> None:
    # 英 CPI は公表時点で前年同月比。変換せず水準として渡す。
    rows = monthly_chain(UK_CPI_HEADLINE_YOY_NSA, ["2.1", "3.4"], unit="percent")

    series = source(rows).series(Currency.GBP, CurrencyFactor.INFLATION, NOW)

    assert [value for _, value in series] == [2.1, 3.4]


def test_default_inputs_use_one_series_per_factor_across_currencies() -> None:
    inflation = {
        currency: factor_input.series
        for (currency, factor), factor_input in DEFAULT_FACTOR_INPUTS.items()
        if factor is CurrencyFactor.INFLATION
    }

    # 各通貨のインフレは 1 本ずつ、かつ全て「前年同月比の %」へ揃っている。
    assert set(inflation) == {Currency.USD, Currency.GBP, Currency.EUR}
    assert (
        DEFAULT_FACTOR_INPUTS[(Currency.USD, CurrencyFactor.INFLATION)].transform
        is SeriesTransform.YEAR_OVER_YEAR
    )
