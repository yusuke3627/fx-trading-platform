"""通貨 state が strategy まで届く配線（ADR-022）。

架空の観測・会合スコアを使う。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_type_hints
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository, usdjpy_spec
from trading.data.features import StoredFeatureSource
from trading.data.macro.registry import (
    US_CPI_HEADLINE_SA,
    US_TREASURY_2Y_YIELD,
    US_UNEMPLOYMENT_RATE_SA,
)
from trading.data.policy.scoring import EVENT_TYPES, SCORING_VERSION
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.money import Currency
from trading.intelligence.currency import (
    CurrencyFactor,
    CurrencyScoreConfig,
    CurrencyStateStore,
    CurrencyStateView,
)
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def observation(series: str, day: datetime, value: str, unit: str) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series=series,
        observation_period=day.date().isoformat(),
        value=Decimal(value),
        unit=unit,
        source="TEST",
        retrieved_at=day,
        known_at=day,
    )


def daily_yields(count: int) -> list[EconomicObservation]:
    """RATES factor の窓を満たすだけの日次利回り。"""
    return [
        observation(
            US_TREASURY_2Y_YIELD,
            NOW - timedelta(days=count - index),
            f"{3.5 + (index % 7) * 0.05:.4f}",
            "percent",
        )
        for index in range(count)
    ]


def fed_score(score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=EVENT_TYPES["FED"],
        source="FED_OFFICIAL",
        payload={"score": score, "scoring_version": SCORING_VERSION},
        retrieved_at=known_at,
        known_at=known_at,
    )


def source(
    observations: list[EconomicObservation] = (),
    events: list[EventEnvelope] = (),
) -> StoredFeatureSource:
    return StoredFeatureSource(
        FakeObservationRepository(observations),
        FakeEventRepository(events),
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
    )


def test_refresh_fills_the_currency_store_the_strategy_holds() -> None:
    feed = source(daily_yields(90), [fed_score(1.0, NOW - timedelta(days=3))])
    store = feed.currency_states

    feed.refresh(NOW)

    usd = store.get(Currency.USD)
    assert usd is not None
    assert usd.score(CurrencyFactor.POLICY) == Decimal("0.5")
    assert usd.score(CurrencyFactor.RATES) is not None


def test_a_currency_with_no_observation_is_absent_not_neutral() -> None:
    feed = source(daily_yields(90))

    feed.refresh(NOW)

    # 何も見えていない通貨を 0 で置くと「方向感が無い」と区別できなくなる。
    assert feed.currency_states.get(Currency.GBP) is None


def test_a_refresh_that_loses_its_inputs_empties_the_store() -> None:
    observations = daily_yields(90)
    feed = source(observations)
    feed.refresh(NOW)
    assert feed.currency_states.get(Currency.USD) is not None

    # 収集が止まって窓から全て外れた後の refresh。
    feed.refresh(NOW + timedelta(days=400))

    assert feed.currency_states.get(Currency.USD) is None


def test_the_pair_needs_both_legs() -> None:
    feed = source(daily_yields(90))
    feed.refresh(NOW)
    store = feed.currency_states

    # USD だけが揃っていて JPY が無い。差が取れないので None。
    assert store.get(Currency.USD) is not None
    assert store.get(Currency.JPY) is None
    assert store.pair(usdjpy_spec()) is None


def test_the_pair_is_the_difference_of_the_legs() -> None:
    store = CurrencyStateStore(CurrencyScoreConfig())
    feed = source(
        daily_yields(90),
        [fed_score(2.0, NOW - timedelta(days=3))],
    )
    feed.refresh(NOW)
    usd = feed.currency_states.get(Currency.USD)
    # JPY 側を手で置いて両 leg を揃える。
    jpy = usd.model_copy(
        update={"currency": Currency.JPY, "directional_score": Decimal("-0.5")}
    )
    states = {Currency.USD: usd, Currency.JPY: jpy}
    store.replace(states)

    pair = store.pair(usdjpy_spec())

    assert pair is not None
    assert pair.directional_score == usd.directional_score - Decimal("-0.5")


def test_retime_recalculates_confidence_and_known_at_on_read() -> None:
    feed = source(events=[fed_score(1.0, NOW - timedelta(days=3))])
    feed.refresh(NOW)
    store = feed.currency_states
    first = store.get(Currency.USD)

    later = NOW + timedelta(days=11)
    store.retime(later)
    second = store.get(Currency.USD)

    assert first is not None and second is not None
    assert second.confidence < first.confidence
    assert second.known_at == later


def test_store_without_retime_returns_the_stored_state() -> None:
    feed = source(events=[fed_score(1.0, NOW - timedelta(days=3))])
    feed.refresh(NOW)
    state = feed.currency_states.get(Currency.USD)
    store = CurrencyStateStore()
    store.replace({Currency.USD: state})

    assert store.get(Currency.USD) is state


def test_pair_confidence_follows_retime() -> None:
    feed = source(events=[fed_score(1.0, NOW - timedelta(days=3))])
    feed.refresh(NOW)
    usd = feed.currency_states.get(Currency.USD)
    jpy = usd.model_copy(
        update={"currency": Currency.JPY, "directional_score": Decimal("-0.5")}
    )
    store = CurrencyStateStore()
    store.replace({Currency.USD: usd, Currency.JPY: jpy})
    store.retime(NOW)
    first = store.pair(usdjpy_spec())

    store.retime(NOW + timedelta(days=11))
    second = store.pair(usdjpy_spec())

    assert first is not None and second is not None
    assert second.confidence < first.confidence
    assert second.known_at == NOW + timedelta(days=11)


def test_the_strategy_holds_the_read_only_view_not_the_store() -> None:
    from trading.strategy.base import StrategyContext

    # 複数 strategy が同じ store を共有する。更新 API が strategy から
    # 見えると、先に評価された strategy が後続の state を消せてしまう。
    assert get_type_hints(StrategyContext)["currency_states"] is CurrencyStateView
    assert not hasattr(CurrencyStateView, "replace")


def test_the_frozen_source_feeds_the_same_store() -> None:
    feed = source(daily_yields(90))
    frozen = feed.frozen(NOW - timedelta(days=30), NOW)

    frozen.refresh(NOW)

    # strategy が参照で持っているのは元の store。凍結側が別の器を作ると
    # refresh が届かない。
    assert frozen.currency_states is feed.currency_states
    assert feed.currency_states.get(Currency.USD) is not None


def test_the_frozen_load_spans_every_series_the_factors_read() -> None:
    windows = source().observation_windows()

    # 月次系列は正規化の窓（60 本）+ 前年同月比の 1 年ぶんが要る。
    assert windows[US_CPI_HEADLINE_SA].days > 365 * 6
    assert windows[US_UNEMPLOYMENT_RATE_SA].days > 365 * 5
    # US2Y は feature と RATES factor の両方が読む。広い方で凍結する。
    assert windows[US_TREASURY_2Y_YIELD] >= timedelta(days=90)


def test_rows_outside_every_window_do_not_reach_the_replay() -> None:
    start, end = NOW - timedelta(days=30), NOW
    window = source().observation_windows()[US_TREASURY_2Y_YIELD]
    inside = observation(
        US_TREASURY_2Y_YIELD, start - window + timedelta(days=1), "3.5", "percent"
    )
    outside = observation(
        US_TREASURY_2Y_YIELD, start - window - timedelta(days=1), "3.5", "percent"
    )

    instants = source([inside, outside]).change_instants(start, end)

    assert instants == [inside.known_at]
