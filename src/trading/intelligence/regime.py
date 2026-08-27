"""Regime service: market-environment labels composed from features.

通貨別 regime と global regime は併存する（設計書 v2.1 §13、ADR-018）。
"USD が hawkish" は通貨の性質だが、"global risk-off" はどの通貨にも同時に
掛かる状態で、片方をもう片方へ畳むと 4 通貨で意味が壊れる。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict

from trading.domain.money import Currency
from trading.intelligence import features as f
from trading.intelligence.features import FeatureStore


class RegimeLabel(StrEnum):
    USD_POLICY_HAWKISH = "USD_POLICY_HAWKISH"
    JPY_POLICY_HAWKISH = "JPY_POLICY_HAWKISH"
    GLOBAL_RISK_OFF = "GLOBAL_RISK_OFF"
    GLOBAL_LIQUIDITY_STRESS = "GLOBAL_LIQUIDITY_STRESS"
    VOLATILITY_HIGH = "VOLATILITY_HIGH"
    INTERVENTION_RISK_HIGH = "INTERVENTION_RISK_HIGH"


class RegimeService(Protocol):
    def active(self) -> AbstractSet[str]: ...


RegimeRule = Callable[[FeatureStore], bool]


def _gt(name: str, threshold: float) -> RegimeRule:
    def rule(store: FeatureStore) -> bool:
        value = store.get(name)
        return value is not None and value > threshold

    return rule


def default_rules(thresholds: dict[str, float] | None = None) -> dict[RegimeLabel, RegimeRule]:
    t = {
        "usd_hawkish_min": 0.0,
        "jpy_hawkish_min": 0.0,
        "intervention_risk_high": 0.6,
        **(thresholds or {}),
    }

    return {
        RegimeLabel.USD_POLICY_HAWKISH: _gt(f.FED_POLICY_SHIFT_SCORE, t["usd_hawkish_min"]),
        RegimeLabel.JPY_POLICY_HAWKISH: _gt(f.BOJ_POLICY_SHIFT_SCORE, t["jpy_hawkish_min"]),
        RegimeLabel.INTERVENTION_RISK_HIGH: _gt(f.INTERVENTION_RISK, t["intervention_risk_high"]),
    }


class RuleBasedRegimeService:
    def __init__(
        self,
        store: FeatureStore,
        rules: dict[RegimeLabel, RegimeRule] | None = None,
    ) -> None:
        self._store = store
        self._rules = rules if rules is not None else default_rules()

    def active(self) -> frozenset[str]:
        return frozenset(label for label, rule in self._rules.items() if rule(self._store))


# frozen=True が止めるのはフィールドの再代入だけで、公開した dict 自体は
# 書き換えられる。read-only snapshot という契約を型で守るために包む。
ImmutableRegimeMap = Annotated[
    Mapping[Currency, frozenset[RegimeLabel]],
    AfterValidator(lambda value: MappingProxyType(dict(value))),
]


class CurrencyRegimeSnapshot(BaseModel):
    """ある時点の regime 全体像（通貨別 + global）。

    strategy には read-only のこの snapshot を渡す（設計書 §13）。一つの
    読み手が書き換えて後続の判断を動かせないよう、通貨マップも不変。
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    by_currency: ImmutableRegimeMap
    global_regimes: frozenset[RegimeLabel]
    known_at: datetime

    def active(self, currency: Currency) -> frozenset[RegimeLabel]:
        """その通貨に掛かる regime。global は全通貨に掛かる。"""
        return self.by_currency.get(currency, frozenset()) | self.global_regimes


def default_currency_rules(
    thresholds: dict[str, float] | None = None,
) -> dict[Currency, dict[RegimeLabel, RegimeRule]]:
    """通貨別ルール。供給されている feature を持つ通貨だけを定義する。

    GBP / EUR は policy score の系列が M2A（#59）の Gate 待ちで、供給が
    無いままルールだけ置いても永久に発火しない死んだ分岐になる。データが
    繋がった時点で BOE / ECB の score を同じ形で足す。
    """
    t = {
        "usd_hawkish_min": 0.0,
        "jpy_hawkish_min": 0.0,
        "intervention_risk_high": 0.6,
        **(thresholds or {}),
    }
    return {
        Currency.USD: {
            RegimeLabel.USD_POLICY_HAWKISH: _gt(
                f.FED_POLICY_SHIFT_SCORE, t["usd_hawkish_min"]
            )
        },
        Currency.JPY: {
            RegimeLabel.JPY_POLICY_HAWKISH: _gt(
                f.BOJ_POLICY_SHIFT_SCORE, t["jpy_hawkish_min"]
            ),
            # 介入リスクは日本の為替介入から算出される JPY の状態
            # （設計書 §12.3）。global に置くと EURUSD のような無関係な
            # ペアまで抑制する。JPY を含むペア（USDJPY / GBPJPY）だけが
            # これを受け取る。
            RegimeLabel.INTERVENTION_RISK_HIGH: _gt(
                f.INTERVENTION_RISK, t["intervention_risk_high"]
            ),
        },
    }


class RuleBasedCurrencyRegimeService:
    """FeatureStore の現在値から通貨別 / global の regime を判定する。

    store は refresh のたびに中身が入れ替わる（features.py）ので、判定は
    呼ばれた時点の値に対して行う — snapshot を保持して使い回さない。
    """

    def __init__(
        self,
        store: FeatureStore,
        currency_rules: Mapping[Currency, Mapping[RegimeLabel, RegimeRule]] | None = None,
        global_rules: Mapping[RegimeLabel, RegimeRule] | None = None,
    ) -> None:
        self._store = store
        self._currency_rules = (
            dict(currency_rules) if currency_rules is not None else default_currency_rules()
        )
        # global regime（GLOBAL_RISK_OFF / GLOBAL_LIQUIDITY_STRESS）を出す
        # feature はまだ供給されていない。既定を空にしておき、系列が
        # 繋がった時点でルールを渡す。
        self._global_rules = dict(global_rules or {})

    def snapshot(self, now: datetime) -> CurrencyRegimeSnapshot:
        return CurrencyRegimeSnapshot(
            by_currency={
                currency: frozenset(
                    label for label, rule in rules.items() if rule(self._store)
                )
                for currency, rules in self._currency_rules.items()
            },
            global_regimes=frozenset(
                label for label, rule in self._global_rules.items() if rule(self._store)
            ),
            known_at=now,
        )

    def active(self, currency: Currency, now: datetime) -> frozenset[RegimeLabel]:
        return self.snapshot(now).active(currency)
