"""Research backtest over recorded ticks.

    python -m trading.backtest.research --symbol USDJPY \
        --strategy post_event_failed_breakout \
        --from 2026-08-18T00:00:00+00:00 --to 2026-08-23T00:00:00+00:00

The default environment is `backtest`: it enables the risk gate for the
simulator (demo/shadow configs keep trading_enabled false, which would
reject every OPEN and grade a strategy at zero fills).

Runs one registered strategy over a period of the stored tick series, with
the feature timeline stepping through the stored macro/policy/intervention
rows exactly as live refreshes would have seen them. Bars are rebuilt from
ticks inside the engine (ADR-006), so the run needs no market_bars rows.

--from/--to are BROKER-clock bounds — the axis event_time is stored on
(ADR-005), the same one the collector's backfill takes. Each tick's known
time is reconstructed as event_time minus the configured broker offset
(ADR-007): the stored received_at is the INGESTION instant, which for a
backfilled archive lies far in the tick's future and would collapse the
whole period onto one replay instant.

The engine materializes the period in memory (ticks and the equity curve),
so a run is a period of days to weeks, not years; longer studies run as
consecutive periods.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from trading.backtest.costs import STRESS_SCENARIOS
from trading.backtest.data import dataset_hash
from trading.backtest.engine import ENGINE_VERSION, BacktestEngine
from trading.backtest.report import write_report
from trading.backtest.run import git_state, synthetic_usdjpy_spec
from trading.config import load_config
from trading.data.cli import aware_utc
from trading.data.features import ReplayFeatureTimeline, StoredFeatureSource
from trading.data.policy.risk_windows import central_bank_calendar
from trading.domain.market import Tick
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.strategy.registry import STRATEGIES


def reconstructed(ticks: list[Tick], offset: timedelta) -> list[Tick]:
    """Rewrite each tick's known time to event_time minus the broker offset.

    received_at records when the row was INGESTED — for the polling collector
    that is the tick's real arrival, but for a backfilled archive it is the
    backfill run's wall clock, shared by the whole window. A replay ordered on
    it would deliver an entire archived day at one instant, after every stored
    macro row has become visible. The broker timestamp is the honest per-tick
    instant both paths share, so the replay axis is derived from it (ADR-007).
    """
    return [t.model_copy(update={"received_at": t.time - offset}) for t in ticks]


def main() -> None:
    parser = argparse.ArgumentParser(description="research backtest over recorded ticks")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    parser.add_argument(
        "--from",
        dest="start",
        type=aware_utc,
        required=True,
        help="period start, broker-clock (the axis event_time is stored on)",
    )
    parser.add_argument(
        "--to",
        dest="end",
        type=aware_utc,
        required=True,
        help="period end (exclusive), broker-clock",
    )
    parser.add_argument("--scenario", default=None, choices=sorted(STRESS_SCENARIOS))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", default="reports")
    args = parser.parse_args()

    if args.start >= args.end:
        parser.error("--from must be earlier than --to")

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    strategy_config = config.strategies.get(args.strategy)
    if strategy_config is None:
        raise SystemExit(f"config for env {args.env!r} has no strategy {args.strategy!r}")
    if symbol not in strategy_config.instruments:
        # The strategy evaluates its configured instruments, not the loaded
        # series; a mismatch would replay one symbol while the strategy waits
        # for bars of another and silently produces nothing.
        raise SystemExit(
            f"{args.strategy!r} is configured for {strategy_config.instruments}, "
            f"not {symbol!r}"
        )

    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresMacroObservationRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(dsn)
    stored = PostgresMarketTickRepository(conn).between(symbol, args.start, args.end)
    if not stored:
        raise SystemExit(
            f"no stored ticks for {symbol} in [{args.start}, {args.end}); "
            "collect or backfill the period first"
        )

    offset = timedelta(hours=config.market.broker_utc_offset_hours)
    ticks = reconstructed(list(stored), offset)

    source = StoredFeatureSource(
        PostgresMacroObservationRepository(conn),
        PostgresEventRepository(conn),
        InterventionRiskConfig(
            version=config.intelligence.intervention_risk.version,
            weights=config.intelligence.intervention_risk.weights,
        ),
        InMemoryFeatureStore(),
    )
    known_start, known_end = args.start - offset, args.end - offset
    timeline = ReplayFeatureTimeline(
        source, source.change_instants(known_start, known_end)
    )

    seed = args.seed if args.seed is not None else config.simulator.seed
    scenario = args.scenario or config.simulator.scenario
    costs = replace(STRESS_SCENARIOS[scenario], latency_ms=config.simulator.latency_ms)
    strategy_class = STRATEGIES[args.strategy]

    engine = BacktestEngine(
        risk_config=config.risk,
        # Stands in until broker specs are persisted alongside the ticks; the
        # values are the vertical slice's USD/JPY dataset input.
        spec=synthetic_usdjpy_spec(symbol),
        costs=costs,
        seed=seed,
        strategy_factory=strategy_class,
        strategy_config=strategy_config,
        event_risk=central_bank_calendar(config),
        features=timeline,
    )
    result = engine.run(ticks)

    manifest = {
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        **git_state(),
        "environment": args.env,
        "symbol": symbol,
        "strategy_id": strategy_class.strategy_id,
        "strategy_version": strategy_class.strategy_version,
        "engine_version": ENGINE_VERSION,
        "scenario": scenario,
        "seed": seed,
        "tick_count": len(ticks),
        "period_from": args.start.isoformat(),
        "period_to": args.end.isoformat(),
        "broker_utc_offset_hours": config.market.broker_utc_offset_hours,
        "dataset_hash": dataset_hash(ticks),
        # Ticks alone do not identify the dataset: the stored PIT rows decide
        # what the gates saw, and re-collection changes them under the same
        # tick series.
        "feature_dataset_hash": source.dataset_fingerprint(known_start, known_end),
        "config_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        "python_version": sys.version.split()[0],
    }
    run_dir = write_report(result, manifest, Path(args.out))

    print(json.dumps({"run_dir": str(run_dir), **result.metrics}, indent=2))


if __name__ == "__main__":
    main()
