"""合成CLI検証専用。明示的に PYTHONPATH へ追加したプロセスだけに適用する。"""
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType

import trading.config
from tests.support import FakeEventRepository, FakeObservationRepository, FakeTickRepository
from trading.backtest.data import synthetic_ticks
from trading.backtest.engine import ScriptedStrategy
from trading.backtest.run import synthetic_usdjpy_spec
from trading.config import load_config
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.strategy.base import StrategyConfig
from trading.strategy.registry import STRATEGIES


class EnsembleProbe(ScriptedStrategy):
    strategy_id = "ensemble_probe"

    def __init__(self) -> None:
        super().__init__({30: PositionDirection.LONG, 180: PositionDirection.SHORT},
                         stop_distance_pips=Decimal(10))


config = load_config("backtest")
config = config.model_copy(update={
    "risk": config.risk.model_copy(update={"event_mode_default": EventRiskMode.NORMAL}),
    "strategies": {EnsembleProbe.strategy_id: StrategyConfig(
        strategy_id=EnsembleProbe.strategy_id, enabled=True, instruments=["USDJPY"],
    )},
})
STRATEGIES[EnsembleProbe.strategy_id] = EnsembleProbe
trading.config.load_config = lambda env: config
start = datetime(2026, 1, 5, 10, tzinfo=UTC)
ticks = synthetic_ticks(spec=synthetic_usdjpy_spec("USDJPY"), start=start, count=600, seed=91)


def connect(dsn: str) -> None:
    if dsn != "synthetic-fixture-no-network":
        raise ValueError("offline fixture only accepts its synthetic DSN")


storage = ModuleType("trading.storage.postgres")
storage.connect = connect
storage.PostgresMarketTickRepository = lambda conn: FakeTickRepository(ticks)
storage.PostgresEventRepository = lambda conn: FakeEventRepository()
storage.PostgresMacroObservationRepository = lambda conn: FakeObservationRepository()
storage.PostgresSwapSnapshotRepository = lambda conn: type(
    "EmptySwap", (), {"known_before": lambda self, symbol, end: []},
)()
sys.modules["trading.storage.postgres"] = storage
