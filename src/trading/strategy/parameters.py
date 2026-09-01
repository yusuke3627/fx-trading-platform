from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
