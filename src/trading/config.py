"""Configuration loading.

config/base.yaml is merged with one environment overlay (backtest / demo /
shadow / micro_live / production). Strategy timeframes, instruments and risk
thresholds are configuration, versioned in YAML — not code or spec text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading.domain.risk import EventRiskMode
from trading.risk.engine import RiskConfig
from trading.strategy.base import StrategyConfig, StrategyHorizon
from trading.strategy.sessions import SessionProfile

ENVIRONMENTS = ("backtest", "demo", "shadow", "micro_live", "production")


class BrokerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected_account_mode: str = "HEDGING"
    magic_number: int = 0
    deviation_points: int = 10


class MarketConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    primary_instruments: list[str] = Field(default_factory=lambda: ["USDJPY"])
    # 口座通貨換算（AccountCurrencyConversionService）の承認 path を張る
    # broker symbol。取引銘柄とは独立に解決する — EURUSD を取引する場合でも
    # JPY 換算には USDJPY の spec と quote が要る。
    conversion_instruments: list[str] = Field(default_factory=lambda: ["USDJPY"])
    quote_max_age_seconds: float = 5.0
    # Zero would turn collection into an unthrottled fetch/commit loop, and a
    # negative value only fails once time.sleep() is reached — after the
    # collector has already started, so the host just restarts it in a loop.
    tick_poll_interval_seconds: float = Field(default=0.2, gt=0, allow_inf_nan=False)
    # How far the trade server's wall clock runs ahead of New York's,
    # year-round — the MT5 New York-close convention that puts the server at
    # UTC+3 during US DST and UTC+2 outside it. Used to reconstruct a
    # recorded tick's known time for research replays (ADR-014) and to place
    # the swap rollover boundary at server midnight (ADR-016); verify
    # against measured received_at once winter rows exist. Bounded to one
    # day ahead: zero/negative or a multi-day value would silently shift
    # every reconstructed instant rather than fail anywhere.
    broker_server_ahead_of_ny_hours: float = Field(
        default=7.0, gt=0, le=24, allow_inf_nan=False
    )


class InstrumentPolicy(BaseModel):
    """platform 対応と live 発注許可の分離（設計書 v2.1 §30）。

    platform_enabled: 収集・feature・shadow 評価の対象にするか。
    trading_enabled: 実際の発注を許すか（risk config の global switch と AND）。
    どちらも既定 False — instruments に載っていない symbol は fail-close。
    """

    model_config = ConfigDict(frozen=True)

    platform_enabled: bool = False
    trading_enabled: bool = False

    @model_validator(mode="after")
    def _trading_requires_platform(self) -> InstrumentPolicy:
        # platform 外の symbol へ発注を許す設定は矛盾（収集も評価もしない
        # pair を取引することになる）。設定境界で拒否する。
        if self.trading_enabled and not self.platform_enabled:
            raise ValueError("trading_enabled requires platform_enabled")
        return self


class StorageConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # DSN comes from the environment, never from a committed file.
    dsn_env: str = "TRADING_DB_DSN"


class MacroDataConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # API keys come from the environment (dsn_env pattern). ALFRED, BEA and
    # Census reject keyless requests; BLS only rate-limits them harder.
    fred_api_key_env: str = "FRED_API_KEY"
    bls_api_key_env: str = "BLS_API_KEY"
    bea_api_key_env: str = "BEA_API_KEY"
    census_api_key_env: str = "CENSUS_API_KEY"


class EventRiskWindowSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    # A negative bound inverts the window — its start lands after its end, so
    # it never activates and the configured halt quietly stops applying. Zero
    # is meaningful (guard only from the announcement onward, or only up to
    # it); negative is not.
    pre_hours: int = Field(default=24, ge=0)
    post_hours: int = Field(default=12, ge=0)
    # Typed rather than free strings: a mode nobody recognises would otherwise
    # reach EventRiskWindow.actions and grade as NORMAL — a halt configured and
    # then silently not applied.
    scalp: EventRiskMode = EventRiskMode.HALT
    intraday: EventRiskMode = EventRiskMode.REDUCED
    swing: EventRiskMode = EventRiskMode.REDUCED

    def actions(self) -> dict[StrategyHorizon, EventRiskMode]:
        """Per-horizon action, in the shape EventRiskWindow carries."""
        return {
            StrategyHorizon.SCALP: self.scalp,
            StrategyHorizon.INTRADAY: self.intraday,
            StrategyHorizon.SWING: self.swing,
        }


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
    instruments: dict[str, InstrumentPolicy] = Field(default_factory=dict)
    storage: StorageConfig = StorageConfig()
    macro_data: MacroDataConfig = MacroDataConfig()
    risk: RiskConfig = RiskConfig()
    event_risk: dict[str, EventRiskWindowSettings] = Field(default_factory=dict)
    intelligence: IntelligenceConfig = IntelligenceConfig()
    simulator: SimulatorConfig = SimulatorConfig()
    session_profiles: dict[str, SessionProfile] = Field(default_factory=dict)
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

    # Every file this system reads and writes is UTF-8, and saying so is not
    # optional: Python falls back to the platform's locale encoding, which on
    # the Japanese Windows trading host is cp932 and rejects the comments these
    # files carry.
    base = yaml.safe_load((config_dir / "base.yaml").read_text(encoding="utf-8")) or {}
    overlay = (
        yaml.safe_load((config_dir / f"{environment}.yaml").read_text(encoding="utf-8")) or {}
    )
    raw = _deep_merge(base, overlay)
    raw["environment"] = environment

    # Fill strategy_id from mapping keys so YAML stays non-repetitive.
    # session profile の catalogue は top-level に 1 つ。参照解決は
    # StrategyConfig 側で行うので、全 strategy へ同じものを渡す。
    profiles = raw.get("session_profiles") or {}
    strategies = {}
    for strategy_id, entry in (raw.get("strategies") or {}).items():
        strategies[strategy_id] = {
            "strategy_id": strategy_id,
            **(entry or {}),
            "session_profiles": profiles,
        }
    raw["strategies"] = strategies

    return AppConfig.model_validate(raw)
