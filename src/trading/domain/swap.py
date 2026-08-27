"""Swap/rollover の PIT broker cost data（ADR-016）。

overnight carry は broker が symbol property として返す値（long/short の
swap、曜日別 rollover 倍率）だけを truth source にする。「水曜が必ず
triple」のような市場慣行をコードにハードコードしない。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading.domain.instrument import InstrumentSpec
from trading.domain.position import PositionDirection

# MT5 SYMBOL_SWAP_MODE の enum 値（broker 返却の生値をそのまま保存する）。
SWAP_MODE_DISABLED = 0
SWAP_MODE_POINTS = 1

# MQL5 ENUM_DAY_OF_WEEK: 0=Sunday .. 6=Saturday。
_MQL_SUNDAY = 0
_MQL_SATURDAY = 6


class UnsupportedSwapModeError(ValueError):
    """carry 計算が実装されていない swap_mode。黙って 0 にせず落とす。"""


class SwapSnapshot(BaseModel):
    """MT5 symbol properties の swap 部分の 1 観測。

    known_at = 取得時刻の forward snapshot（観測であり backfill 不可）。
    per-day 倍率（swap_sunday..swap_saturday）は terminal のビルドによって
    公開されないことがあるため None を許し、その場合は swap_rollover3days
    から倍率を導く。
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID
    symbol: str
    swap_mode: int
    swap_long: Decimal
    swap_short: Decimal
    # 3日分 swap を課す曜日（MQL5 ENUM_DAY_OF_WEEK）。
    swap_rollover3days: int

    # MT5 API 上は double（broker が 0.5 や 1.5 を返し得る）。丸めない。
    swap_sunday: Decimal | None = None
    swap_monday: Decimal | None = None
    swap_tuesday: Decimal | None = None
    swap_wednesday: Decimal | None = None
    swap_thursday: Decimal | None = None
    swap_friday: Decimal | None = None
    swap_saturday: Decimal | None = None

    payload_hash: str | None = None
    retrieved_at: datetime
    known_at: datetime

    def rollover_multiplier(self, day: date) -> Decimal:
        """`day`（broker server 日付）の rollover で課される日数倍率。

        broker が per-day 倍率を返していればそれが truth source。返して
        いない日は swap_rollover3days の曜日を 3 倍、週末（市場クローズで
        rollover が発生しない）を 0、他を 1 とする。
        """
        mql_dow = (day.weekday() + 1) % 7
        per_day = (
            self.swap_sunday,
            self.swap_monday,
            self.swap_tuesday,
            self.swap_wednesday,
            self.swap_thursday,
            self.swap_friday,
            self.swap_saturday,
        )[mql_dow]
        if per_day is not None:
            return per_day
        if mql_dow in (_MQL_SUNDAY, _MQL_SATURDAY):
            return Decimal(0)
        return Decimal(3) if mql_dow == self.swap_rollover3days else Decimal(1)


def carry_amount(
    snapshot: SwapSnapshot,
    *,
    spec: InstrumentSpec,
    direction: PositionDirection,
    quantity: Decimal,
    day: date,
) -> Decimal:
    """1 回の rollover で発生する carry（quote 通貨建て、符号は broker 値のまま）。

    POINTS モードのみ実装: swap 値は「1 lot・1 泊あたりの points」で、
    1 lot あたりの 1 point の価値は contract_size × 10^-digits（quote 通貨）。
    lot 数 = quantity / contract_size なので contract_size は約分され、
    carry = points × point_size × quantity。他モードの broker に当たった
    場合は黙って誤額を計上せず UnsupportedSwapModeError で落とす。
    """
    multiplier = snapshot.rollover_multiplier(day)
    if multiplier == 0 or snapshot.swap_mode == SWAP_MODE_DISABLED:
        return Decimal(0)
    if snapshot.swap_mode != SWAP_MODE_POINTS:
        raise UnsupportedSwapModeError(
            f"swap_mode={snapshot.swap_mode} for {snapshot.symbol} has no carry "
            "model; only POINTS (1) is implemented (ADR-016)"
        )
    points = (
        snapshot.swap_long
        if direction is PositionDirection.LONG
        else snapshot.swap_short
    )
    point_size = Decimal(1).scaleb(-spec.digits)
    return points * point_size * quantity * multiplier
