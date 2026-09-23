"""ホットパス変更前後で共用する固定入力と、全結果の損失なし正準化。"""
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from trading.backtest.costs import CostModel
from trading.backtest.data import synthetic_ticks
from trading.backtest.engine import BacktestEngine
from trading.backtest.run import synthetic_usdjpy_spec
from trading.data.market.dukascopy import decode_bi5, known_to_broker_label
from trading.domain.market import Tick
from trading.domain.risk import EventRiskMode
from trading.risk.engine import RiskConfig
from trading.strategy.base import StrategyConfig, TimeframeMap
from trading.strategy.registry import STRATEGIES

ANCHOR = timedelta(hours=7)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "replay_hot_path"
STRATEGY_IDS = ("failed_spike_reversal", "range_edge_reversal")


def input_ticks(dataset: str) -> list[Tick]:
    if dataset == "synthetic":
        return synthetic_ticks(
            spec=synthetic_usdjpy_spec("USDJPY"),
            start=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            count=12_000,
            seed=73,
            interval_seconds=0.3,
        )
    ticks = []
    for name in ("20241107T19Z", "20241107T20Z"):
        hour = datetime.strptime(name, "%Y%m%dT%HZ").replace(tzinfo=UTC)
        decoded = decode_bi5(
            (FIXTURES / f"{name}.bi5").read_bytes(), "USDJPY", hour,
            datetime(2026, 9, 20, tzinfo=UTC),
        )
        ticks.extend(
            tick.model_copy(update={"time": known_to_broker_label(tick.time, ANCHOR)})
            for tick in decoded
        )
    return ticks


def strategy_config(strategy_id: str) -> StrategyConfig:
    # 短い fixture でも売買・保有期限・複数時間足を通す研究用の固定設定。
    return StrategyConfig(
        strategy_id=strategy_id, enabled=True, instruments=["USDJPY"],
        timeframes=TimeframeMap(entry="1m", regime="5m"),
        parameters={
            "spike_atr_multiple": 0.7, "spike_window_seconds": 60,
            "atr_period": 3, "long_side_enabled": True,
            "expected_horizon_seconds": 30, "horizon_exit_enabled": True,
            "range_lookback_bars": 6, "ema_period": 3, "range_slope_lookback": 2,
            "range_slope_max_atr": 100, "range_width_min_atr": 0.5,
            "entry_band_fraction": 1, "min_reward_to_risk": 0,
            "session_end_buffer_seconds": 0,
            "absolute_max_spread_pips": "10",
        },
    )


def make_engine(strategy_id: str, evaluate_from: datetime) -> BacktestEngine:
    return BacktestEngine(
        risk_config=RiskConfig(
            trading_enabled=True, event_mode_default=EventRiskMode.NORMAL,
            max_units_per_symbol={"USDJPY": 10_000},
            absolute_max_spread_pips={"USDJPY": Decimal(10)},
            max_risk_per_trade_pct=Decimal(1),
            portfolio_stop_risk_budget_pct=Decimal(10),
            max_currency_net_exposure_pct=Decimal(1000),
            daily_loss_halt_pct=Decimal(100), rolling_24h_loss_halt_pct=Decimal(100),
            high_water_mark_drawdown_halt_pct=Decimal(100),
        ),
        spec=synthetic_usdjpy_spec("USDJPY"), costs=CostModel(), seed=42,
        strategy_factory=STRATEGIES[strategy_id], strategy_config=strategy_config(strategy_id),
        evaluate_from=evaluate_from,
    )


def canonical(value):
    if is_dataclass(value):
        return {field.name: canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, BaseModel):
        return canonical(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, Enum):
        return canonical(value.value)
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    return value
