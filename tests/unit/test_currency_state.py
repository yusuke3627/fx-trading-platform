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
    one_factor = service({(Currency.USD, CurrencyFactor.POLICY): rising()})
    all_factors = service(
        {(Currency.USD, factor): rising() for factor in CurrencyFactor}
    )

    sparse = one_factor.state(Currency.USD, NOW)
    dense = all_factors.state(Currency.USD, NOW)

    assert sparse.directional_score == dense.directional_score
    assert sparse.confidence < dense.confidence
    assert dense.confidence == Decimal(1)


def test_absent_factor_scores_stay_none_not_zero():
    state = service({(Currency.USD, CurrencyFactor.POLICY): rising()}).state(
        Currency.USD, NOW
    )

    assert state.score(CurrencyFactor.POLICY) is not None
    assert state.score(CurrencyFactor.GROWTH) is None


def test_no_data_yields_neutral_score_with_zero_confidence():
    state = service({}).state(Currency.GBP, NOW)

    assert state.directional_score == 0
    assert state.confidence == 0
    assert all(state.score(factor) is None for factor in CurrencyFactor)


def test_stale_observations_decay_confidence():
    fresh = service({(Currency.USD, CurrencyFactor.POLICY): rising()})
    old = service({(Currency.USD, CurrencyFactor.POLICY): rising(T0 - timedelta(days=8))})

    fresh_state = fresh.state(Currency.USD, T0 + timedelta(days=5, hours=1))
    stale_state = old.state(Currency.USD, T0 + timedelta(days=5, hours=1))

    assert stale_state.directional_score == fresh_state.directional_score
    assert stale_state.confidence < fresh_state.confidence


def test_directional_score_weights_available_factors():
    weighted = CurrencyScoreConfig(
        weights={CurrencyFactor.POLICY: 3.0, CurrencyFactor.RATES: 1.0},
        normalization=NormalizationConfig(window=20, min_observations=5),
    )
    states = service(
        {
            (Currency.USD, CurrencyFactor.POLICY): rising(),
            (Currency.USD, CurrencyFactor.RATES): falling(),
        },
        weighted,
    ).state(Currency.USD, NOW)

    policy = states.score(CurrencyFactor.POLICY)
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
            (Currency.USD, CurrencyFactor.POLICY): rising(),
            (Currency.JPY, CurrencyFactor.POLICY): falling(),
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
            (Currency.USD, CurrencyFactor.POLICY): rising(),
            (Currency.JPY, CurrencyFactor.POLICY): rising(),
        }
    ).pair_state(usdjpy_spec(), NOW)
    opposed = service(
        {
            (Currency.USD, CurrencyFactor.POLICY): rising(),
            (Currency.JPY, CurrencyFactor.POLICY): falling(),
        }
    ).pair_state(usdjpy_spec(), NOW)

    assert aligned.base.confidence == opposed.base.confidence
    assert aligned.confidence < opposed.confidence


def test_pair_known_at_follows_the_later_leg():
    # directional_score は新しい方の leg の情報を含む。古い方を known_at に
    # すると、known_at 順の replay でその時点にはまだ無い情報が見える。
    states = service(
        {
            (Currency.USD, CurrencyFactor.POLICY): rising(),
            (Currency.JPY, CurrencyFactor.POLICY): rising(),
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
    state = service({(Currency.USD, CurrencyFactor.POLICY): rising()}).state(
        Currency.USD, NOW
    )

    with pytest.raises(TypeError):
        state.factor_scores[CurrencyFactor.GROWTH] = Decimal(1)
    with pytest.raises(TypeError):
        CONFIG.weights[CurrencyFactor.POLICY] = -1.0


def test_non_finite_weights_are_rejected():
    # NaN は合計 > 0 と非負のどちらの検証もすり抜けるので、設定境界で弾く。
    with pytest.raises(ValueError, match="finite"):
        CurrencyScoreConfig(weights={CurrencyFactor.POLICY: float("nan")})


def test_overflowing_weight_sum_is_rejected():
    # 個別には有限でも合計が inf になる設定は、confidence の
    # Infinity / Infinity で落ちる。境界で拒否する。
    with pytest.raises(ValueError, match="finite"):
        CurrencyScoreConfig(
            weights={
                CurrencyFactor.POLICY: 1e308,
                CurrencyFactor.RATES: 1e308,
            }
        )


def test_service_revalidates_a_config_copied_past_the_validator():
    # model_copy(update=...) は validator を通さず、未検証で可変な weights
    # が入る。サービスの入口で弾く。
    bypassed = CONFIG.model_copy(update={"weights": {}})

    with pytest.raises(ValueError, match="finite positive"):
        CurrencyStateService(MappingFactorSeries({}), bypassed)


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
    states = service({(Currency.USD, CurrencyFactor.POLICY): rising()})

    state = states.state(
        Currency.USD,
        NOW,
        regimes=frozenset({RegimeLabel.USD_POLICY_HAWKISH}),
        intervention_risk=Decimal("0.7"),
    )

    assert state.regimes == frozenset({RegimeLabel.USD_POLICY_HAWKISH})
    assert state.intervention_risk == Decimal("0.7")
