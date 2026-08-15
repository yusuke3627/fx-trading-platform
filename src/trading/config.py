"""Configuration loading.

config/base.yaml is merged with one environment overlay (backtest / demo /
shadow / micro_live / production). Strategy timeframes, instruments and risk
thresholds are configuration, versioned in YAML — not code or spec text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trading.risk.engine import RiskConfig
from trading.strategy.base import StrategyConfig

ENVIRONMENTS = ("backtest", "demo", "shadow", "micro_live", "production")


class BrokerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected_account_mode: str = "HEDGING"
    magic_number: int = 0
    deviation_points: int = 10


class MarketConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary_instruments: list[str] = Field(default_factory=lambda: ["USDJPY"])
    quote_max_age_seconds: float = 5.0
    # Zero would turn collection into an unthrottled fetch/commit loop, and a
    # negative value only fails once time.sleep() is reached — after the
    # collector has already started, so the host just restarts it in a loop.
    tick_poll_interval_seconds: float = Field(default=0.2, gt=0, allow_inf_nan=False)


class StorageConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # DSN comes from the environment, never from a committed file.
    dsn_env: str = "TRADING_DB_DSN"


class EventRiskWindowSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    pre_hours: int = 24
    post_hours: int = 12
    scalp: str = "HALT"
    intraday: str = "REDUCED"
    swing: str = "REDUCED"


class InterventionRiskSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = "0"
    weights: dict[str, float] = Field(default_factory=dict)


class IntelligenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    llm_enabled: bool = False
    intervention_risk: InterventionRiskSettings = InterventionRiskSettings()


class SimulatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: str = "normal"
    seed: int = 42
    latency_ms: float = 150.0


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: str
    broker: BrokerConfig = BrokerConfig()
    market: MarketConfig = MarketConfig()
    storage: StorageConfig = StorageConfig()
    risk: RiskConfig = RiskConfig()
    event_risk: dict[str, EventRiskWindowSettings] = Field(default_factory=dict)
    intelligence: IntelligenceConfig = IntelligenceConfig()
    simulator: SimulatorConfig = SimulatorConfig()
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(environment: str, config_dir: Path | str = "config") -> AppConfig:
    if environment not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {environment!r}; expected one of {ENVIRONMENTS}")
    config_dir = Path(config_dir)

    base = yaml.safe_load((config_dir / "base.yaml").read_text()) or {}
    overlay = yaml.safe_load((config_dir / f"{environment}.yaml").read_text()) or {}
    raw = _deep_merge(base, overlay)
    raw["environment"] = environment

    # Fill strategy_id from mapping keys so YAML stays non-repetitive.
    strategies = {}
    for strategy_id, entry in (raw.get("strategies") or {}).items():
        strategies[strategy_id] = {"strategy_id": strategy_id, **(entry or {})}
    raw["strategies"] = strategies

    return AppConfig.model_validate(raw)
