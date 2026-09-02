from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trading.strategy.spread_gate import SpreadGateSettings, spread_ceiling

ParamValue = float | int | str | bool
ParamGroup = dict[str, ParamValue]


class StrategyParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: dict[str, ParamValue | ParamGroup] = Field(default_factory=dict)
    instruments: dict[str, dict[str, ParamValue | ParamGroup]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _wrap_flat_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict) and not ({"defaults", "instruments"} & data.keys()):
            return {"defaults": data}
        return data

    @model_validator(mode="after")
    def _settle_typed_parameters(self) -> StrategyParameters:
        """型の決まったパラメータは設定境界で確定する。

        パラメータは任意のキーを通す器なので、ここで確定しないと不正な値が
        strategy の最初の市場イベントまで生き残る。defaults と instrument
        override は別々に検証する — override は部分指定で、マージ後にしか
        揃わないキーがあるため。
        """
        layers = {"defaults": self.defaults} | {
            f"instruments.{symbol}": layer for symbol, layer in self.instruments.items()
        }
        for where, layer in layers.items():
            try:
                if "spread_gate" in layer:
                    SpreadGateSettings.model_validate(layer["spread_gate"])
                if "absolute_max_spread_pips" in layer:
                    spread_ceiling(layer["absolute_max_spread_pips"])
            except ValidationError as exc:
                raise ValueError(f"{where}: {exc}") from exc
        return self


class ResolvedStrategyParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    values: dict[str, ParamValue | ParamGroup]

    def param(self, name: str, default: object) -> object:
        return self.values.get(name, default)

    @property
    def session_profile(self) -> str | None:
        value = self.values.get("session_profile")
        return value if isinstance(value, str) else None


class StrategyParameterResolver:
    def __init__(self, parameters: StrategyParameters) -> None:
        self._parameters = parameters

    def resolve(self, symbol: str) -> ResolvedStrategyParameters:
        values = {
            **self._parameters.defaults,
            **self._parameters.instruments.get(symbol, {}),
        }
        return ResolvedStrategyParameters(symbol=symbol, values=values)
