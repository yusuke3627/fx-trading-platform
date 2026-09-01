from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict

from trading.strategy.parameters import ParamGroup, ResolvedStrategyParameters


class SpreadGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_spread_to_atr: float | None = None
    absolute_max_spread_pips: Decimal | None = None

    # 現行 ceiling 1.5 pips ÷ USDJPY 1m ATR 3 pips 相当を normalized で近似した
    # 仮置き。昇格 Gate で校正する。
    DEFAULT_MAX_SPREAD_TO_ATR: ClassVar[float] = 0.5

    @classmethod
    def from_params(cls, params: ResolvedStrategyParameters) -> SpreadGate:
        group = cast(ParamGroup, params.param("spread_gate", {}))
        absolute = params.param("absolute_max_spread_pips", None)
        return cls(
            max_spread_to_atr=float(
                group.get("max_spread_to_atr", cls.DEFAULT_MAX_SPREAD_TO_ATR)
            ),
            absolute_max_spread_pips=(
                Decimal(str(absolute)) if absolute is not None else None
            ),
        )

    def allows(self, *, spread: Decimal, atr: float, pip_size: Decimal) -> bool:
        if self.max_spread_to_atr is not None and (
            atr <= 0 or float(spread) > self.max_spread_to_atr * atr
        ):
            return False
        return (
            self.absolute_max_spread_pips is None
            or spread <= self.absolute_max_spread_pips * pip_size
        )
