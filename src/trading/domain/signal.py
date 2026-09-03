"""Strategy signal.

A signal expresses a desired position direction with conviction and a stop
distance. Final quantity is never decided by the strategy.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.position import PositionDirection


class StrategySignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: UUID

    strategy_id: str
    strategy_version: str

    symbol: str

    desired_direction: PositionDirection
    conviction: float = Field(ge=0.0, le=1.0)
    # 期待 edge（R 倍数）。strategy が推定を持つまでは 1R = 中立で、
    # 裁定の priority は confidence だけで決まる。
    expected_edge_r: Decimal = Field(default=Decimal(1), gt=0)

    expected_horizon_seconds: int

    stop_distance_pips: Decimal

    reason_codes: list[str] = Field(default_factory=list)

    generated_at: datetime
