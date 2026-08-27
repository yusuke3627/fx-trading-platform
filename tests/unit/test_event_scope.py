"""Currency-scoped event risk（設計書 34.4、ADR-017）。

イベントは影響通貨とその伝播 policy を持ち、ペアは leg に届く window だけ
で止まる。FOMC は GLOBAL_CRITICAL として全ペアへ届く。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.support import eurusd_spec, gbpjpy_spec, gbpusd_spec, usdjpy_spec
from tests.unit.test_policy_risk_windows import SETTINGS, meeting
from trading.data.policy.risk_windows import central_bank_windows
from trading.domain.money import Currency
from trading.domain.risk import EventRiskMode
from trading.risk.event_risk import (
    EventPropagationPolicy,
    EventRiskCalendar,
    EventRiskWindow,
)
from trading.strategy.base import StrategyHorizon

T0 = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
COVERS = (T0 - timedelta(days=30), T0 + timedelta(days=30))
SCALP = StrategyHorizon.SCALP


def window(
    currencies: set[Currency],
    propagation: EventPropagationPolicy = EventPropagationPolicy.DIRECT_LEGS,
    at: datetime = T0,
) -> EventRiskWindow:
    return EventRiskWindow(
        name="test_event",
        first_event_at=at,
        last_event_at=at,
        pre_hours=48,
        post_hours=24,
        actions={SCALP: EventRiskMode.HALT},
        affected_currencies=frozenset(currencies),
        propagation=propagation,
    )


def test_ecb_direct_event_does_not_stop_usdjpy():
    calendar = EventRiskCalendar([window({Currency.EUR})], COVERS)

    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, T0) is EventRiskMode.NORMAL
    assert calendar.mode_for_instrument(eurusd_spec(), SCALP, T0) is EventRiskMode.HALT


def test_global_critical_reaches_pairs_without_a_direct_leg():
    # FOMC は GBPJPY（USD leg なし）にも synthetic cross 経由で届く。
    calendar = EventRiskCalendar(
        [window({Currency.USD}, EventPropagationPolicy.GLOBAL_CRITICAL)], COVERS
    )

    for spec in (usdjpy_spec(), eurusd_spec(), gbpusd_spec(), gbpjpy_spec()):
        assert calendar.mode_for_instrument(spec, SCALP, T0) is EventRiskMode.HALT


def test_dependency_graph_scaffold_falls_back_to_global_reach():
    # DEPENDENCY_GRAPH は導出未実装の scaffold。実装まで保守側（全ペア）。
    calendar = EventRiskCalendar(
        [window({Currency.USD}, EventPropagationPolicy.DEPENDENCY_GRAPH)], COVERS
    )

    assert calendar.mode_for_instrument(gbpjpy_spec(), SCALP, T0) is EventRiskMode.HALT


def test_unscoped_window_applies_to_every_pair():
    # scope 未指定の window（従来形式）は fail-close で全ペアに適用。
    calendar = EventRiskCalendar([window(set())], COVERS)

    assert calendar.mode_for_instrument(eurusd_spec(), SCALP, T0) is EventRiskMode.HALT


def test_boj_affects_usdjpy_and_gbpjpy_only():
    (boj,) = central_bank_windows([meeting("BOJ", T0)], [], SETTINGS)
    calendar = EventRiskCalendar([boj], COVERS)

    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, T0) is EventRiskMode.HALT
    assert calendar.mode_for_instrument(gbpjpy_spec(), SCALP, T0) is EventRiskMode.HALT
    assert calendar.mode_for_instrument(gbpusd_spec(), SCALP, T0) is EventRiskMode.NORMAL
    assert calendar.mode_for_instrument(eurusd_spec(), SCALP, T0) is EventRiskMode.NORMAL


def test_boe_affects_gbpusd_and_gbpjpy_only():
    (boe,) = central_bank_windows([meeting("BOE", T0)], [], SETTINGS)
    calendar = EventRiskCalendar([boe], COVERS)

    assert calendar.mode_for_instrument(gbpusd_spec(), SCALP, T0) is EventRiskMode.HALT
    assert calendar.mode_for_instrument(gbpjpy_spec(), SCALP, T0) is EventRiskMode.HALT
    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, T0) is EventRiskMode.NORMAL


def test_fed_meeting_is_global_critical():
    (fed,) = central_bank_windows([meeting("FED", T0)], [], SETTINGS)
    calendar = EventRiskCalendar([fed], COVERS)

    assert calendar.mode_for_instrument(gbpjpy_spec(), SCALP, T0) is EventRiskMode.HALT


def test_adjacent_cross_bank_windows_are_pair_local():
    # BOJ@T0 と BOE@T0+2d: GBPJPY（両 leg）は切れ目なく gate される一方、
    # USDJPY は BOE 単独の時間帯（BOJ window の post 終了後）では止まらない。
    boe_at = T0 + timedelta(days=2)
    windows = central_bank_windows(
        [meeting("BOJ", T0), meeting("BOE", boe_at)], [], SETTINGS
    )
    calendar = EventRiskCalendar(windows, COVERS)

    # BOJ window: [T0-48h, T0+24h] / BOE window: [T0, T0+72h] — 重なる。
    for offset_hours in range(-48, 73, 6):
        at = T0 + timedelta(hours=offset_hours)
        assert calendar.mode_for_instrument(gbpjpy_spec(), SCALP, at) is (
            EventRiskMode.HALT
        ), f"calm gap for GBPJPY at {at}"
    boe_only = T0 + timedelta(hours=30)
    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, boe_only) is (
        EventRiskMode.NORMAL
    )
    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, T0) is EventRiskMode.HALT


def test_scoping_keeps_coverage_semantics():
    # 絞り込みは window の適用可否であって、暦の coverage を狭めない。
    calendar = EventRiskCalendar([window({Currency.EUR})], COVERS)

    outside = COVERS[1] + timedelta(days=1)
    assert calendar.mode_for_instrument(usdjpy_spec(), SCALP, outside) is None
