"""同時 signal 裁定のモデル（設計書 v2.1 §25–27、ADR-029）。

裁定を行う service は portfolio 層（`portfolio/arbitrator.py`）にあり、storage と
live runner は本モデルだけを読む。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading.domain.exposure import OpenPositionExposure
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal

ACCEPTED = "ACCEPTED"
REJECTED_EXPIRED = "REJECTED_EXPIRED"
REJECTED_TRADING_DISABLED = "REJECTED_TRADING_DISABLED"
REJECTED_REDUNDANT_FACTOR_EXPOSURE = "REJECTED_REDUNDANT_FACTOR_EXPOSURE"
REJECTED_TRIANGLE_CAP = "REJECTED_TRIANGLE_CAP"


class CandidateSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: UUID
    strategy_id: str
    symbol: str
    position_direction: PositionDirection
    expected_edge_r: Decimal
    confidence: Decimal
    stop_distance_pips: Decimal
    generated_at: datetime
    expires_at: datetime

    @classmethod
    def from_signal(cls, signal: StrategySignal) -> CandidateSignal:
        # signal はその horizon より長く有効ではない。
        return cls(
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            position_direction=signal.desired_direction,
            expected_edge_r=signal.expected_edge_r,
            confidence=Decimal(str(signal.conviction)),
            stop_distance_pips=signal.stop_distance_pips,
            generated_at=signal.generated_at,
            expires_at=signal.generated_at
            + timedelta(seconds=signal.expected_horizon_seconds),
        )


class ArbitrationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal: CandidateSignal
    # 受理されたとき book に加わる exposure。sized intent の数量（±target_quantity、
    # 未 size なら 0）・entry 価格（LONG=ask / SHORT=bid）・stop を provider が詰める。
    exposure: OpenPositionExposure
    trading_enabled: bool = True


class ArbitrationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    arbitration_id: UUID
    signal_id: UUID
    accepted: bool
    reason_code: str
    # validity で落ちた候補は rank / priority を持たない。
    rank: int | None
    priority: Decimal | None
    detail: str | None = None
    decided_at: datetime
    # 受理候補を Risk が grade する book（既存 + 先に受理した候補）。永続化しない。
    book_before: tuple[OpenPositionExposure, ...] = ()


class ArbitrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    # priority 順（rank 昇順）。
    accepted: tuple[ArbitrationDecision, ...]
    # rank 昇順、rank 無し（validity 却下）は末尾。同順位は tiebreak 順。
    rejected: tuple[ArbitrationDecision, ...]
