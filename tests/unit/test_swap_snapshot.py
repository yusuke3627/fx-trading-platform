"""Swap snapshot と rollover carry（設計書 34.5B、ADR-016）。

All values are fictional test data shaped like MT5 symbol properties.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import usdjpy_spec
from trading.backtest.rollover import (
    SwapTimeline,
    ended_server_day,
    next_rollover_boundary,
)
from trading.data.swap.collector import SWAP_SNAPSHOT_RAW, build_snapshot
from trading.domain.position import PositionDirection
from trading.domain.swap import (
    SWAP_MODE_DISABLED,
    SWAP_MODE_POINTS,
    SwapSnapshot,
    UnsupportedSwapModeError,
    carry_amount,
)

RETRIEVED = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)

# 2026-08-10 は月曜。以降のテストはこの週の曜日を使う。
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
WEDNESDAY = date(2026, 8, 12)
THURSDAY = date(2026, 8, 13)
SATURDAY = date(2026, 8, 15)
SUNDAY = date(2026, 8, 16)


def snapshot(**overrides) -> SwapSnapshot:
    values = {
        "snapshot_id": uuid4(),
        "symbol": "USDJPY",
        "swap_mode": SWAP_MODE_POINTS,
        "swap_long": Decimal("-2.2"),
        "swap_short": Decimal("0.4"),
        # MQL5 ENUM_DAY_OF_WEEK: 3 = Wednesday
        "swap_rollover3days": 3,
        "retrieved_at": RETRIEVED,
        "known_at": RETRIEVED,
    }
    values.update(overrides)
    return SwapSnapshot(**values)


# ---------------------------------------------------------------------------
# 曜日倍率
# ---------------------------------------------------------------------------


def test_per_day_multiplier_uses_broker_fields():
    # broker が木曜 triple を返すなら、それが truth source。
    s = snapshot(
        swap_sunday=0,
        swap_monday=1,
        swap_tuesday=1,
        swap_wednesday=1,
        swap_thursday=3,
        swap_friday=1,
        swap_saturday=0,
    )
    assert s.rollover_multiplier(THURSDAY) == 3
    assert s.rollover_multiplier(WEDNESDAY) == 1
    assert s.rollover_multiplier(SATURDAY) == 0


def test_fallback_uses_returned_rollover_day_not_hardcoded_wednesday():
    # per-day フィールドの無い terminal では swap_rollover3days（ここでは
    # 木曜=4）から導く。水曜をハードコードしていれば落ちる。
    s = snapshot(swap_rollover3days=4)
    assert s.rollover_multiplier(THURSDAY) == 3
    assert s.rollover_multiplier(WEDNESDAY) == 1
    assert s.rollover_multiplier(SATURDAY) == 0
    assert s.rollover_multiplier(SUNDAY) == 0


# ---------------------------------------------------------------------------
# carry 金額
# ---------------------------------------------------------------------------


def test_carry_long_short_direction_and_points_math():
    spec = usdjpy_spec()  # digits=3 -> point 0.001
    s = snapshot()
    long_carry = carry_amount(
        s,
        spec=spec,
        direction=PositionDirection.LONG,
        quantity=Decimal(5000),
        day=WEDNESDAY,
    )
    # -2.2 points × 0.001 × 5000 units × 3日分 = -33 JPY
    assert long_carry == Decimal(-33)
    short_carry = carry_amount(
        s,
        spec=spec,
        direction=PositionDirection.SHORT,
        quantity=Decimal(5000),
        day=TUESDAY,
    )
    assert short_carry == Decimal(2)


def test_unsupported_swap_mode_fails_loud_on_charged_day():
    s = snapshot(swap_mode=5)
    with pytest.raises(UnsupportedSwapModeError):
        carry_amount(
            s,
            spec=usdjpy_spec(),
            direction=PositionDirection.LONG,
            quantity=Decimal(1000),
            day=MONDAY,
        )
    # 倍率 0 の日は課金が発生しないので mode を裁かない。
    assert (
        carry_amount(
            s,
            spec=usdjpy_spec(),
            direction=PositionDirection.LONG,
            quantity=Decimal(1000),
            day=SATURDAY,
        )
        == 0
    )


def test_disabled_swap_mode_is_zero():
    s = snapshot(swap_mode=SWAP_MODE_DISABLED)
    assert (
        carry_amount(
            s,
            spec=usdjpy_spec(),
            direction=PositionDirection.LONG,
            quantity=Decimal(1000),
            day=MONDAY,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# rollover boundary
# ---------------------------------------------------------------------------


def test_boundary_is_server_midnight_and_follows_ny_dst():
    # server = NY+7h → boundary は NY 17:00。夏 (EDT) は 21:00Z。
    assert next_rollover_boundary(
        datetime(2026, 8, 10, 12, 0, tzinfo=UTC), 7.0
    ) == datetime(2026, 8, 10, 21, 0, tzinfo=UTC)
    # 冬 (EST) は 22:00Z。
    assert next_rollover_boundary(
        datetime(2026, 1, 14, 12, 0, tzinfo=UTC), 7.0
    ) == datetime(2026, 1, 14, 22, 0, tzinfo=UTC)
    # 「厳密に後」: boundary ちょうどからは翌日を返す。
    assert next_rollover_boundary(
        datetime(2026, 8, 10, 21, 0, tzinfo=UTC), 7.0
    ) == datetime(2026, 8, 11, 21, 0, tzinfo=UTC)


def test_ended_server_day_is_ny_date_at_boundary():
    assert ended_server_day(datetime(2026, 8, 12, 21, 0, tzinfo=UTC)) == WEDNESDAY


def test_timeline_latest_known_before_and_symbol_filter():
    early = snapshot(known_at=datetime(2026, 8, 10, 6, 0, tzinfo=UTC))
    late = snapshot(
        known_at=datetime(2026, 8, 12, 6, 0, tzinfo=UTC), swap_long=Decimal("-9.9")
    )
    other = snapshot(symbol="EURUSD", known_at=datetime(2026, 8, 9, 6, 0, tzinfo=UTC))
    timeline = SwapTimeline([late, early, other], "USDJPY")

    assert timeline.latest_known_before(
        datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    ) is early
    assert timeline.latest_known_before(
        datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    ) is late
    assert timeline.latest_known_before(
        datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    ) is None


# ---------------------------------------------------------------------------
# collector build
# ---------------------------------------------------------------------------

_InfoWithDays = namedtuple(
    "_InfoWithDays",
    "swap_mode swap_long swap_short swap_rollover3days "
    "swap_sunday swap_monday swap_tuesday swap_wednesday "
    "swap_thursday swap_friday swap_saturday session_open",
)
_InfoWithoutDays = namedtuple(
    "_InfoWithoutDays", "swap_mode swap_long swap_short swap_rollover3days"
)


def test_build_snapshot_maps_fields_and_archives_raw_payload():
    info = _InfoWithDays(1, -2.2, 0.4, 3, 0, 1, 1, 3, 1, 1, 0, float("inf"))
    parsed, event = build_snapshot("USDJPY", info, retrieved_at=RETRIEVED)

    assert parsed.swap_mode == SWAP_MODE_POINTS
    assert parsed.swap_long == Decimal("-2.2")
    assert parsed.swap_short == Decimal("0.4")
    assert parsed.swap_wednesday == 3
    assert parsed.known_at == RETRIEVED
    assert event.event_type == SWAP_SNAPSHOT_RAW
    # raw payload は swap 以外のフィールドも全量保存し、非有限 float は
    # JSONB が拒否するため文字列で保全する。
    assert event.payload["session_open"] == "inf"
    assert event.payload["swap_long"] == -2.2
    assert event.payload_hash == parsed.payload_hash


def test_build_snapshot_without_per_day_fields_leaves_them_absent():
    info = _InfoWithoutDays(1, -2.2, 0.4, 3)
    parsed, _ = build_snapshot("USDJPY", info, retrieved_at=RETRIEVED)
    assert parsed.swap_wednesday is None
    # per-day が無くても fallback（swap_rollover3days）で倍率は導ける。
    assert parsed.rollover_multiplier(WEDNESDAY) == 3
