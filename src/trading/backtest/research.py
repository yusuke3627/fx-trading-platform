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
time is reconstructed from its broker label through the server's New York
anchor (ADR-007): the stored received_at is the INGESTION instant, which
for a backfilled archive lies far in the tick's future and would collapse
the whole period onto one replay instant.

Each strategy declares the lead-in its slowest indicator window needs
(`Strategy.warmup`); the runner reads that much history ahead of --from so
state is populated, and evaluations start at --from itself.

The engine materializes the period in memory (ticks and the equity curve),
so a run is a period of days to weeks, not years; longer studies run as
consecutive periods.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

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

NEW_YORK = ZoneInfo("America/New_York")


def broker_label_to_known(label: datetime, server_ahead_of_ny: timedelta) -> datetime:
    """A broker wall-clock label -> the real UTC instant it names.

    The server keeps New York close at its own midnight: its wall time is New
    York wall time plus a fixed anchor (7h) YEAR-ROUND, which lands at UTC+3
    during US DST and UTC+2 outside it. Subtracting the anchor and localizing
    in America/New_York therefore follows the DST switches without a season
    table. The repeated fall-back hour maps to its first occurrence (fold=0):
    that one broker-labelled hour a year is ambiguous in the recorded series
    itself, and no constant recovers it.
    """
    naive_ny = label.astimezone(UTC).replace(tzinfo=None) - server_ahead_of_ny
    return naive_ny.replace(tzinfo=NEW_YORK).astimezone(UTC)


def reconstructed(ticks: list[Tick], server_ahead_of_ny: timedelta) -> list[Tick]:
    """Rewrite each tick's known time from its broker label (ADR-007).

    received_at records when the row was INGESTED — for the polling collector
    that is the tick's real arrival, but for a backfilled archive it is the
    backfill run's wall clock, shared by the whole window. A replay ordered on
    it would deliver an entire archived day at one instant, after every stored
    macro row has become visible. The broker timestamp is the honest per-tick
    instant both paths share, so the replay axis is derived from it.
    """
    return [
        t.model_copy(
            update={"received_at": broker_label_to_known(t.time, server_ahead_of_ny)}
        )
        for t in ticks
    ]


def warmup_days(value: str) -> float:
    """A finite, non-negative number of lead-in days.

    A negative value would silently push read_from PAST --from, dropping the
    period's head from the replay while the manifest still records the full
    period as evaluated."""
    days = float(value)
    if not math.isfinite(days) or days < 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not a non-negative day count")
    return days


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
    parser.add_argument(
        "--warmup-days",
        type=warmup_days,
        default=None,
        help="lead-in read before --from to populate indicator state; "
        "defaults to the strategy's own declared warmup",
    )
    args = parser.parse_args()

    if args.start >= args.end:
        parser.error("--from must be earlier than --to")

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    if symbol != "USDJPY":
        # The only dataset spec wired is the vertical slice's USD/JPY one;
        # replaying another pair against its digits/pip/volume values would
        # misprice sizing and costs while finishing without complaint.
        raise SystemExit(
            f"only USDJPY has a dataset spec; {symbol!r} needs a persisted "
            "broker spec first"
        )
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

    strategy_class = STRATEGIES[args.strategy]
    warmup = (
        timedelta(days=args.warmup_days)
        if args.warmup_days is not None
        else strategy_class.warmup(strategy_config)
    )
    read_from = args.start - warmup

    conn = connect(dsn)
    stored = PostgresMarketTickRepository(conn).between(symbol, read_from, args.end)
    if not stored:
        raise SystemExit(
            f"no stored ticks for {symbol} in [{read_from}, {args.end}); "
            "collect or backfill the period first"
        )
    if stored[-1].time < args.start:
        # Only lead-in ticks exist: the run would warm up, evaluate nothing
        # and still write a plausible-looking flat report.
        raise SystemExit(
            f"no stored ticks for {symbol} inside the evaluation period "
            f"[{args.start}, {args.end}); only warm-up ticks were found"
        )

    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)
    ticks = reconstructed(list(stored), anchor)

    # One consistent load of the PIT rows: change schedule, every snapshot
    # during the replay and the manifest fingerprint answer from the same
    # rows even while a collector keeps inserting on this database.
    known_start = broker_label_to_known(read_from, anchor)
    known_end = broker_label_to_known(args.end, anchor)
    source = StoredFeatureSource(
        PostgresMacroObservationRepository(conn),
        PostgresEventRepository(conn),
        InterventionRiskConfig(
            version=config.intelligence.intervention_risk.version,
            weights=config.intelligence.intervention_risk.weights,
        ),
        InMemoryFeatureStore(),
    ).frozen(known_start, known_end)
    timeline = ReplayFeatureTimeline(
        source, source.change_instants(known_start, known_end)
    )

    seed = args.seed if args.seed is not None else config.simulator.seed
    scenario = args.scenario or config.simulator.scenario
    costs = replace(STRESS_SCENARIOS[scenario], latency_ms=config.simulator.latency_ms)

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
        # The lead-in only builds bar/indicator/feature state; orders and
        # metrics that matter start at the period's opening instant.
        evaluate_from=broker_label_to_known(args.start, anchor),
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
        "warmup_days": warmup / timedelta(days=1),
        "broker_server_ahead_of_ny_hours": (
            config.market.broker_server_ahead_of_ny_hours
        ),
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
