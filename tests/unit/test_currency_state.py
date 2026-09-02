"""CurrencyState / PairState / 通貨別 regime（設計書 §12–13、34.5A）。

All values are fictional test data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import eurusd_spec, usdjpy_spec
from trading.domain.money import Currency
from trading.intelligence.currency import (
    CurrencyFactor,
    CurrencyScoreConfig,
    CurrencyStateService,
    MappingFactorSeries,
)
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.normalization import NormalizationConfig
from trading.intelligence.regime import (
    RegimeLabel,
    RuleBasedCurrencyRegimeService,
)

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
# 系列の最新観測（T0 + 5日）の直後。freshness 減点の掛からない基準時刻で、
# coverage / conflict の効果だけを見られるようにする。
NOW = T0 + timedelta(days=5, hours=1)
CONFIG = CurrencyScoreConfig(
    normalization=NormalizationConfig(window=20, min_observations=5)
)


# POLICY は通貨横断で校正済みの尺度に載るので正規化を掛けない（ADR-021）。
# 「同じ系列なら同じ値」を前提にする検証は残りの factor で行う。
NORMALIZED_FACTORS = tuple(f for f in CurrencyFactor if f is not CurrencyFactor.POLICY)


def rising(start: datetime = T0) -> list[tuple[datetime, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 1.6])]


def falling(start: datetime = T0) -> list[tuple[datetime, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 0.4])]


def service(series: dict, config: CurrencyScoreConfig = CONFIG) -> CurrencyStateService:
    return CurrencyStateService(MappingFactorSeries(series), config)


# ---------------------------------------------------------------------------
# CurrencyState
# ---------------------------------------------------------------------------


def test_missing_factors_lower_confidence_not_the_score():
    # 設計書 §12.2A: coverage 不足で score を膨らませず confidence を下げる。
    # POLICY だけは尺度が違い（正規化せず上限で割る）、同じ系列を与えても値が
    # 揃わない。ここで見たいのは「factor が増えても値が薄まらない」ことなので、
    # 同じ尺度の factor だけで比べる。
    config = CurrencyScoreConfig(
        weights={factor: 1.0 for factor in NORMALIZED_FACTORS},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    one_factor = service({(Currency.USD, CurrencyFactor.GROWTH): rising()}, config)
    all_factors = service(
        {(Currency.USD, factor): rising() for factor in NORMALIZED_FACTORS}, config
    )

    sparse = one_factor.state(Currency.USD, NOW)
    dense = all_factors.state(Currency.USD, NOW)

    assert sparse.directional_score == dense.directional_score
    assert sparse.confidence < dense.confidence
    assert dense.confidence == Decimal(1)


def test_absent_factor_scores_stay_none_not_zero():
    state = service({(Currency.USD, CurrencyFactor.GROWTH): rising()}).state(
        Currency.USD, NOW
    )

    assert state.score(CurrencyFactor.GROWTH) is not None
    assert state.score(CurrencyFactor.INFLATION) is None


def test_no_data_yields_neutral_score_with_zero_confidence():
    state = service({}).state(Currency.GBP, NOW)

    assert state.directional_score == 0
    assert state.confidence == 0
    assert all(state.score(factor) is None for factor in CurrencyFactor)


def test_stale_observations_decay_confidence():
    fresh = service({(Currency.USD, CurrencyFactor.GROWTH): rising()})
    old = service({(Currency.USD, CurrencyFactor.GROWTH): rising(T0 - timedelta(days=8))})

    fresh_state = fresh.state(Currency.USD, T0 + timedelta(days=5, hours=1))
    stale_state = old.state(Currency.USD, T0 + timedelta(days=5, hours=1))

    assert stale_state.directional_score == fresh_state.directional_score
    assert stale_state.confidence < fresh_state.confidence


def test_monthly_cadence_stays_fresh_until_the_next_observation():
    config = CurrencyScoreConfig(
        weights={CurrencyFactor.INFLATION: 1.0},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    rows = [
        (T0 + timedelta(days=30 * index), value)
        for index, value in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 1.4])
    ]
    latest = rows[-1][0]

    state = service(
        {(Currency.USD, CurrencyFactor.INFLATION): rows}, config
    ).state(Currency.USD, latest + timedelta(days=20))

    assert state.confidence == Decimal(1)


def test_monthly_cadence_decays_after_one_interval_and_reaches_zero_at_three():
    config = CurrencyScoreConfig(
        weights={CurrencyFactor.INFLATION: 1.0},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    rows = [
        (T0 + timedelta(days=30 * index), value)
        for index, value in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 1.4])
    ]
    latest = rows[-1][0]
    states = service({(Currency.USD, CurrencyFactor.INFLATION): rows}, config)

    halfway = states.state(Currency.USD, latest + timedelta(days=60))
    expired = states.state(Currency.USD, latest + timedelta(days=90))

    assert halfway.confidence == Decimal("0.500000")
    assert expired.confidence == 0


def test_policy_cadence_stays_fresh_while_waiting_for_the_next_meeting():
    config = CurrencyScoreConfig(weights={CurrencyFactor.POLICY: 1.0})
    rows = [(T0, -1.0), (T0 + timedelta(days=42), 1.0)]

    state = service({(Currency.USD, CurrencyFactor.POLICY): rows}, config).state(
        Currency.USD, rows[-1][0] + timedelta(days=30)
    )

    assert state.confidence == Decimal(1)


def test_daily_cadence_uses_the_forty_eight_hour_floor():
    config = CurrencyScoreConfig(
        weights={CurrencyFactor.RATES: 1.0},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    rows = rising()
    latest = rows[-1][0]
    states = service({(Currency.USD, CurrencyFactor.RATES): rows}, config)

    weekend = states.state(Currency.USD, latest + timedelta(hours=48))
    expired = states.state(Currency.USD, latest + timedelta(hours=144))

    assert weekend.confidence == Decimal(1)
    assert expired.confidence == 0


def test_unknown_cadence_uses_the_fixed_fallback():
    config = CurrencyScoreConfig(weights={CurrencyFactor.POLICY: 1.0})
    states = service({(Currency.USD, CurrencyFactor.POLICY): [(T0, 1.0)]}, config)

    halfway = states.state(Currency.USD, T0 + timedelta(hours=192))
    expired = states.state(Currency.USD, T0 + timedelta(hours=336))

    assert halfway.confidence == Decimal("0.500000")
    assert expired.confidence == 0


def test_directional_score_weights_available_factors():
    weighted = CurrencyScoreConfig(
        weights={CurrencyFactor.GROWTH: 3.0, CurrencyFactor.RATES: 1.0},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    states = service(
        {
            (Currency.USD, CurrencyFactor.GROWTH): rising(),
            (Currency.USD, CurrencyFactor.RATES): falling(),
        },
        weighted,
    ).state(Currency.USD, NOW)

    policy = states.score(CurrencyFactor.GROWTH)
    rates = states.score(CurrencyFactor.RATES)
    assert policy is not None and rates is not None
    expected = (policy * 3 + rates) / 4
    assert states.directional_score == expected.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# PairState
# ---------------------------------------------------------------------------


def test_pair_score_is_base_minus_quote():
    states = service(
        {
            (Currency.USD, CurrencyFactor.GROWTH): rising(),
            (Currency.JPY, CurrencyFactor.GROWTH): falling(),
        }
    )

    pair = states.pair_state(usdjpy_spec(), NOW)

    assert pair.symbol == "USDJPY"
    assert pair.directional_score == (
        pair.base.directional_score - pair.quote.directional_score
    )
    # USD が強く JPY が弱いので USDJPY は上向き。
    assert pair.directional_score > 0


def test_pair_confidence_is_bounded_by_the_weaker_leg():
    # EUR 側にデータが無い EURUSD は、USD がいくら揃っていても信頼できない。
    states = service({(Currency.USD, factor): rising() for factor in CurrencyFactor})

    pair = states.pair_state(eurusd_spec(), NOW)

    assert pair.quote.confidence == Decimal(1)
    assert pair.base.confidence == 0
    assert pair.confidence == 0


def test_conflicting_legs_lower_pair_confidence():
    # 両 leg が同方向に強いと net は小さくなるが、それは大きな値どうしの
    # 引き算で、同じ差でも相対的な不確かさが大きい（設計書 §12.2）。
    aligned = service(
        {
            (Currency.USD, CurrencyFactor.GROWTH): rising(),
            (Currency.JPY, CurrencyFactor.GROWTH): rising(),
        }
    ).pair_state(usdjpy_spec(), NOW)
    opposed = service(
        {
            (Currency.USD, CurrencyFactor.GROWTH): rising(),
            (Currency.JPY, CurrencyFactor.GROWTH): falling(),
        }
    ).pair_state(usdjpy_spec(), NOW)

    assert aligned.base.confidence == opposed.base.confidence
    assert aligned.confidence < opposed.confidence


def test_pair_known_at_follows_the_later_leg():
    # directional_score は新しい方の leg の情報を含む。古い方を known_at に
    # すると、known_at 順の replay でその時点にはまだ無い情報が見える。
    states = service(
        {
            (Currency.USD, CurrencyFactor.GROWTH): rising(),
            (Currency.JPY, CurrencyFactor.GROWTH): rising(),
        }
    )
    base = states.state(Currency.USD, NOW)
    quote = states.state(Currency.JPY, NOW - timedelta(hours=3))

    pair = states.project(usdjpy_spec(), base, quote)

    assert pair.known_at == base.known_at
    assert pair.known_at > quote.known_at


# ---------------------------------------------------------------------------
# Currency regime
# ---------------------------------------------------------------------------


def test_currency_regimes_stay_scoped_to_their_currency():
    store = InMemoryFeatureStore()
    store.replace({"fed_policy_shift_score": 0.8, "intervention_risk": 0.9})

    snapshot = RuleBasedCurrencyRegimeService(store).snapshot(NOW)

    assert RegimeLabel.USD_POLICY_HAWKISH in snapshot.active(Currency.USD)
    assert RegimeLabel.USD_POLICY_HAWKISH not in snapshot.active(Currency.JPY)
    # 介入リスクは日本の介入から算出される JPY の状態。EUR/USD 側の
    # ペアまで抑制しない（設計書 §12.3）。
    assert RegimeLabel.INTERVENTION_RISK_HIGH in snapshot.active(Currency.JPY)
    assert RegimeLabel.INTERVENTION_RISK_HIGH not in snapshot.active(Currency.USD)
    assert RegimeLabel.INTERVENTION_RISK_HIGH not in snapshot.active(Currency.EUR)


def test_global_regimes_reach_every_currency():
    store = InMemoryFeatureStore()
    store.replace({"intervention_risk": 0.9})
    # global rule は feature が繋がるまで既定が空。二層構造そのものは
    # ルールを渡せば機能する。
    service = RuleBasedCurrencyRegimeService(
        store,
        global_rules={
            RegimeLabel.GLOBAL_RISK_OFF: lambda s: (s.get("intervention_risk") or 0) > 0.5
        },
    )

    snapshot = service.snapshot(NOW)

    for currency in (Currency.USD, Currency.JPY, Currency.GBP, Currency.EUR):
        assert RegimeLabel.GLOBAL_RISK_OFF in snapshot.active(currency)


def test_state_and_config_mappings_are_read_only():
    # frozen=True はフィールド再代入しか止めない。共有された state / config
    # の中身を書き換えられると、作成時に確定した directional_score や
    # 検証済みの重みと食い違う。
    state = service({(Currency.USD, CurrencyFactor.GROWTH): rising()}).state(
        Currency.USD, NOW
    )

    with pytest.raises(TypeError):
        state.factor_scores[CurrencyFactor.GROWTH] = Decimal(1)
    with pytest.raises(TypeError):
        CONFIG.weights[CurrencyFactor.GROWTH] = -1.0


def test_non_finite_weights_are_rejected():
    # NaN は合計 > 0 と非負のどちらの検証もすり抜けるので、設定境界で弾く。
    with pytest.raises(ValueError, match="finite"):
        CurrencyScoreConfig(weights={CurrencyFactor.GROWTH: float("nan")})


def test_overflowing_weight_sum_is_rejected():
    # 個別には有限でも合計が inf になる設定は、confidence の
    # Infinity / Infinity で落ちる。境界で拒否する。
    with pytest.raises(ValueError, match="finite"):
        CurrencyScoreConfig(
            weights={
                CurrencyFactor.GROWTH: 1e308,
                CurrencyFactor.RATES: 1e308,
            }
        )


def test_service_revalidates_a_config_copied_past_the_validator():
    # model_copy(update=...) は validator を通さず、未検証で可変な weights
    # が入る。サービスの入口で弾く。
    bypassed = CONFIG.model_copy(update={"weights": {}})

    with pytest.raises(ValueError, match="finite positive"):
        CurrencyStateService(MappingFactorSeries({}), bypassed)


def test_service_revalidates_nested_normalization_config():
    # ネストした pydantic モデルは既定では再検証されない。clip_sigma=0 を
    # 迂回させると normalize_series がゼロ除算になる。
    bypassed = CONFIG.normalization.model_copy(update={"clip_sigma": 0})

    with pytest.raises(ValueError, match="clip_sigma"):
        CurrencyStateService(
            MappingFactorSeries({}), CONFIG.model_copy(update={"normalization": bypassed})
        )


def test_snapshot_currency_map_is_read_only():
    # strategy へ渡す snapshot を一つの読み手が書き換えられない。
    snapshot = RuleBasedCurrencyRegimeService(InMemoryFeatureStore()).snapshot(NOW)

    with pytest.raises(TypeError):
        snapshot.by_currency[Currency.USD] = frozenset({RegimeLabel.USD_POLICY_HAWKISH})


def test_missing_features_activate_no_regime():
    snapshot = RuleBasedCurrencyRegimeService(InMemoryFeatureStore()).snapshot(NOW)

    assert snapshot.active(Currency.USD) == frozenset()
    assert snapshot.global_regimes == frozenset()


def test_regimes_ride_along_on_the_state():
    states = service({(Currency.USD, CurrencyFactor.GROWTH): rising()})

    state = states.state(
        Currency.USD,
        NOW,
        regimes=frozenset({RegimeLabel.USD_POLICY_HAWKISH}),
        intervention_risk=Decimal("0.7"),
    )

    assert state.regimes == frozenset({RegimeLabel.USD_POLICY_HAWKISH})
    assert state.intervention_risk == Decimal("0.7")
