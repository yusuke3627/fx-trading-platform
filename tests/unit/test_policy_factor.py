"""POLICY factor: 中銀声明スコアを通貨横断の尺度のまま扱う（ADR-021）。

架空の会合スコアを使う。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import FakeEventRepository, usdjpy_spec
from trading.data.factor_series import (
    DEFAULT_FACTOR_INPUTS,
    POLICY_LOOKBACK,
    PolicyScoreFactorSeries,
)
from trading.data.policy.scoring import EVENT_TYPES, SCORE_MAX, SCORE_MIN
from trading.domain.event import EventEnvelope
from trading.domain.money import Currency
from trading.intelligence.currency import (
    ChainedFactorSeries,
    CurrencyFactor,
    CurrencyScoreConfig,
    CurrencyStateService,
    MappingFactorSeries,
)
from trading.intelligence.normalization import bounded_score, normalize_series

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def score_event(bank: str, score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=EVENT_TYPES[bank],
        source=f"{bank}_OFFICIAL",
        payload={"score": score, "scoring_version": "policy_shift_v1"},
        retrieved_at=known_at,
        known_at=known_at,
    )


def meetings(bank: str, scores: list[float]) -> list[EventEnvelope]:
    """6 週間おきの会合。最後が NOW の 3 日前。"""
    last = NOW - timedelta(days=3)
    return [
        score_event(bank, score, last - timedelta(weeks=6 * (len(scores) - 1 - index)))
        for index, score in enumerate(scores)
    ]


def policy_source(events: list[EventEnvelope]) -> PolicyScoreFactorSeries:
    return PolicyScoreFactorSeries(FakeEventRepository(events))


# ---------------------------------------------------------------------------
# 供給元
# ---------------------------------------------------------------------------


def test_the_fed_score_feeds_usd_and_the_boj_score_feeds_jpy() -> None:
    source = policy_source([*meetings("FED", [1.5]), *meetings("BOJ", [-0.5])])

    usd = source.series(Currency.USD, CurrencyFactor.POLICY, NOW)
    jpy = source.series(Currency.JPY, CurrencyFactor.POLICY, NOW)

    assert [value for _, value in usd] == [1.5]
    assert [value for _, value in jpy] == [-0.5]


def test_currencies_without_a_scored_central_bank_are_absent() -> None:
    source = policy_source(meetings("FED", [1.5]))

    # BOE / ECB は採点対象外（scoring.EVENT_TYPES に無い）。
    assert source.series(Currency.GBP, CurrencyFactor.POLICY, NOW) == ()
    assert source.series(Currency.EUR, CurrencyFactor.POLICY, NOW) == ()


def test_the_source_answers_only_the_policy_factor() -> None:
    source = policy_source(meetings("FED", [1.5]))

    assert source.series(Currency.USD, CurrencyFactor.RATES, NOW) == ()


def test_a_meeting_beyond_the_lookback_is_not_read() -> None:
    stale = score_event("FED", 2.0, NOW - POLICY_LOOKBACK - timedelta(days=1))
    source = policy_source([stale, *meetings("FED", [0.5])])

    assert [value for _, value in source.series(
        Currency.USD, CurrencyFactor.POLICY, NOW
    )] == [0.5]


# ---------------------------------------------------------------------------
# 尺度
# ---------------------------------------------------------------------------


def test_the_score_is_divided_by_its_bound_not_z_scored() -> None:
    series = [(NOW - timedelta(days=3), 1.5)]

    assert bounded_score(series, NOW, SCORE_MAX).value == Decimal("0.75")


def test_opposite_central_banks_stay_opposite() -> None:
    # 全会合で利上げしている中銀と、全会合で利下げしている中銀。
    hawkish = meetings("FED", [2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    dovish = meetings("BOJ", [-2.0, -2.0, -2.0, -2.0, -2.0, -2.0])
    source = policy_source([*hawkish, *dovish])
    service = CurrencyStateService(source)

    usd = service.state(Currency.USD, NOW)
    jpy = service.state(Currency.JPY, NOW)

    assert usd.score(CurrencyFactor.POLICY) == Decimal(1)
    assert jpy.score(CurrencyFactor.POLICY) == Decimal(-1)
    # 通貨ごとに自分の履歴で z を取ると、どちらも「その中銀にしては普通」の
    # 中立へ潰れて最大の乖離が消える。
    assert normalize_series(
        [(event.known_at, 2.0) for event in hawkish], NOW
    ) is None


def test_one_statement_is_enough_to_read_a_stance() -> None:
    # 会合は年 8 回。正規化の min_observations（20）を満たすには 2.5 年かかる。
    service = CurrencyStateService(policy_source(meetings("FED", [1.0])))

    assert service.state(Currency.USD, NOW).score(CurrencyFactor.POLICY) == Decimal("0.5")


def test_a_score_beyond_the_bound_is_clipped() -> None:
    series = [(NOW - timedelta(days=3), SCORE_MAX * 3)]

    assert bounded_score(series, NOW, SCORE_MAX).value == Decimal(1)
    assert bounded_score(
        [(NOW - timedelta(days=3), SCORE_MIN * 3)], NOW, SCORE_MAX
    ).value == Decimal(-1)


def test_the_latest_visible_statement_wins() -> None:
    service = CurrencyStateService(policy_source(meetings("FED", [-2.0, 0.0, 1.0])))

    assert service.state(Currency.USD, NOW).score(CurrencyFactor.POLICY) == Decimal("0.5")


def test_a_statement_known_after_now_is_not_visible() -> None:
    published = meetings("FED", [1.0])
    future = score_event("FED", -2.0, NOW + timedelta(days=1))
    service = CurrencyStateService(policy_source([*published, future]))

    assert service.state(Currency.USD, NOW).score(CurrencyFactor.POLICY) == Decimal("0.5")


def test_no_statement_leaves_the_factor_absent_not_neutral() -> None:
    service = CurrencyStateService(policy_source([]))

    assert service.state(Currency.USD, NOW).score(CurrencyFactor.POLICY) is None


def test_the_configured_bound_matches_the_scoring_scale() -> None:
    # intelligence 層は data 層を import しないので、値の一致はここで見る。
    assert CurrencyScoreConfig().bounded_factors[CurrencyFactor.POLICY] == SCORE_MAX
    assert SCORE_MIN == -SCORE_MAX


def test_a_bound_that_is_not_positive_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        CurrencyScoreConfig(bounded_factors={CurrencyFactor.POLICY: 0.0})


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------


def test_the_chain_takes_the_first_source_that_answers() -> None:
    macro = MappingFactorSeries(
        {(Currency.USD, CurrencyFactor.RATES): [(NOW - timedelta(days=1), 4.2)]}
    )
    chained = ChainedFactorSeries(macro, policy_source(meetings("FED", [1.0])))

    assert [v for _, v in chained.series(Currency.USD, CurrencyFactor.RATES, NOW)] == [4.2]
    assert [v for _, v in chained.series(Currency.USD, CurrencyFactor.POLICY, NOW)] == [1.0]
    assert chained.series(Currency.GBP, CurrencyFactor.POLICY, NOW) == ()


def test_a_hawkish_fed_against_a_dovish_boj_lifts_usdjpy() -> None:
    source = policy_source([*meetings("FED", [2.0]), *meetings("BOJ", [-2.0])])
    service = CurrencyStateService(source)

    pair = service.pair_state(usdjpy_spec(), NOW)

    # USD が最大タカ派、JPY が最大ハト派。POLICY 以外は欠測なので
    # directional は POLICY だけで決まる。
    assert pair.directional_score == Decimal(2)


def test_the_macro_mapping_no_longer_supplies_policy() -> None:
    policy_inputs = {
        currency
        for (currency, factor) in DEFAULT_FACTOR_INPUTS
        if factor is CurrencyFactor.POLICY
    }

    # 政策金利の水準を POLICY に置くと、据え置き期間は窓が同値で埋まって
    # 正規化が語れなくなる（#84）。金利パスは RATES の 2 年点が持つ。
    assert policy_inputs == set()
