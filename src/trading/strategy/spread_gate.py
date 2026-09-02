from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from trading.strategy.parameters import ResolvedStrategyParameters


class SpreadGateSettings(BaseModel):
    """`spread_gate` パラメータ群の型。設定境界（StrategyParameters）で確定する。

    ここで確定しないと `spread_gate: disabled` のような設定が読み込みを通り、
    最初の市場イベントで strategy が型エラーで止まる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_spread_to_atr: float | None = Field(default=None, gt=0)

    @field_validator("max_spread_to_atr", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        # bool は int のサブクラスで、float へは 1.0 に黙って変換される。
        if isinstance(value, bool):
            raise PydanticCustomError(
                "bool_not_number", "max_spread_to_atr must be a number, not a bool"
            )
        return value


# `absolute_max_spread_pips` は instrument 層の直下に置く（group の外）ので、
# 値単体の型として持つ。bool は Decimal 化で拒否される。
SpreadCeiling = Annotated[Decimal, Field(gt=0)]
_CEILING = TypeAdapter(SpreadCeiling)


def spread_ceiling(value: object) -> Decimal:
    return _CEILING.validate_python(value)


class SpreadGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_spread_to_atr: float | None = None
    absolute_max_spread_pips: Decimal | None = None

    # 現行 ceiling 1.5 pips ÷ USDJPY 1m ATR 3 pips 相当を normalized で近似した
    # 仮置き。昇格 Gate で校正する。
    DEFAULT_MAX_SPREAD_TO_ATR: ClassVar[float] = 0.5

    @classmethod
    def from_params(cls, params: ResolvedStrategyParameters) -> SpreadGate:
        settings = SpreadGateSettings.model_validate(params.param("spread_gate", {}))
        ceiling = params.param("absolute_max_spread_pips", None)
        return cls(
            max_spread_to_atr=(
                cls.DEFAULT_MAX_SPREAD_TO_ATR
                if settings.max_spread_to_atr is None
                else settings.max_spread_to_atr
            ),
            absolute_max_spread_pips=None if ceiling is None else spread_ceiling(ceiling),
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
