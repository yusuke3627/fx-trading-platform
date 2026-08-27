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
anchor (ADR-014): the stored received_at is the INGESTION instant, which
for a backfilled archive lies far in the tick's future and would collapse
the whole period onto one replay instant.

Each strategy declares the lead-in its slowest indicator window needs
(`Strategy.warmup`); the runner reads that much history ahead of --from so
state is populated, and evaluations start at --from itself.

Ticks are streamed from the database in one pass — reconstruction, the
manifest digest and the engine ride the same iterator — so memory is bounded
by the engine's tick-retention window, not the period length; months-long
periods are a matter of runtime, not RAM.

Such a run reports its position on stderr, one line per replay day, carrying
the feature values the fundamental gates read at that point; stdout stays the
manifest alone, so piping it to a file keeps the progress on the terminal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO
from uuid import uuid4
from zoneinfo import ZoneInfo

from trading.backtest.costs import STRESS_SCENARIOS
from trading.backtest.data import TickDigest
from trading.backtest.engine import ENGINE_VERSION, BacktestEngine
from trading.backtest.report import write_report
from trading.backtest.rollover import swap_dataset_fingerprint
from trading.backtest.run import git_state, synthetic_usdjpy_spec
from trading.config import load_config
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
    table.

    The DST transition hours themselves never name a tradable instant under
    this anchor: New York switches at 02:00 Sunday, inside the FX weekend
    close (Friday 17:00 - Sunday 17:00 New York). A label inside the repeated
    or skipped hour therefore contradicts the anchor assumption, and folding
    it onto either occurrence could hand the replay a future price an hour
    early — so it is refused instead of guessed.
    """
    naive_ny = label.astimezone(UTC).replace(tzinfo=None) - server_ahead_of_ny
    first = naive_ny.replace(tzinfo=NEW_YORK)
    if first.utcoffset() != naive_ny.replace(tzinfo=NEW_YORK, fold=1).utcoffset():
        raise ValueError(
            f"broker label {label.isoformat()} falls in a New York DST "
            "transition hour, which the New York-close anchor places inside "
            "the weekend close; the dataset contradicts the configured anchor"
        )
    return first.astimezone(UTC)


def reconstructed_tick(tick: Tick, server_ahead_of_ny: timedelta) -> Tick:
    """Rewrite one tick's known time from its broker label (ADR-014).

    received_at records when the row was INGESTED — for the polling collector
    that is the tick's real arrival, but for a backfilled archive it is the
    backfill run's wall clock, shared by the whole window. A replay ordered on
    it would deliver an entire archived day at one instant, after every stored
    macro row has become visible. The broker timestamp is the honest per-tick
    instant both paths share, so the replay axis is derived from it.
    """
    return tick.model_copy(
        update={"received_at": broker_label_to_known(tick.time, server_ahead_of_ny)}
    )


def reconstructed(ticks: Sequence[Tick], server_ahead_of_ny: timedelta) -> list[Tick]:
    """reconstructed_tick over a materialized dataset."""
    return [reconstructed_tick(t, server_ahead_of_ny) for t in ticks]


def broker_label(value: str) -> datetime:
    """An ISO timestamp naming a broker wall-clock label (+00:00 only).

    The label axis is the server's wall clock STAMPED as UTC (ADR-005).
    An input carrying any other offset would be normalized onto real UTC and
    silently name a different label — `2026-08-18T00:00:00+03:00` for the
    summer broker midnight would read three hours of the wrong range — so
    only +00:00/Z inputs are accepted.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError(
            f"{value!r} must carry a +00:00/Z offset: broker labels are "
            "wall-clock values stamped UTC (ADR-005), and any other offset "
            "silently shifts the requested range"
        )
    return parsed


# The open-market time an edge gap may hold before it counts as missing
# data: quiet feed edges around the weekly open/close, not a trading day.
EDGE_GAP_TOLERANCE_SECONDS = 3600.0


def open_market_seconds(start: datetime, end: datetime) -> float:
    """Label-axis open-market time inside [start, end).

    Under the New York-close anchor the label weekend — Saturday and Sunday
    dates on the broker's wall clock — is exactly the FX closure, so weekday
    label time is the time the market was quoting. Holidays are not
    modelled: a gap over a closed weekday counts as missing data and is
    refused, which errs on the honest side.
    """
    total = 0.0
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = min(end, next_midnight)
        if cursor.weekday() < 5:
            total += (day_end - cursor).total_seconds()
        cursor = day_end
    return total


def _ensure_head_covered(first: Tick, read_from: datetime) -> None:
    if open_market_seconds(read_from, first.time) > EDGE_GAP_TOLERANCE_SECONDS:
        raise SystemExit(
            f"stored history begins at {first.time}, market-open time "
            f"after the requested warm-up start {read_from}; the opening "
            "evaluations would run on starved indicator state — backfill "
            "earlier history or move --from later"
        )


def _ensure_tail_covered(last: Tick, end: datetime) -> None:
    if open_market_seconds(last.time, end) > EDGE_GAP_TOLERANCE_SECONDS:
        raise SystemExit(
            f"stored history ends at {last.time}, market-open time "
            f"before the requested period end {end}; the report would claim "
            "the full period — backfill the tail or move --to earlier"
        )


def ensure_period_covered(
    bounds: tuple[Tick, Tick] | None,
    read_from: datetime,
    start: datetime,
    end: datetime,
) -> None:
    """SystemExit unless the stored series can honestly serve the run.

    Each failure shape would otherwise finish and write a plausible-looking
    report: an empty read; a read holding only lead-in ticks (nothing
    evaluated); a history beginning after the requested warm-up start
    (opening evaluations on starved indicator state); and a history ending
    before --to (a partial period reported as the full one). Edge gaps are
    measured in open-market time, so a weekend at either edge passes while
    a missing trading day does not. A gap in the MIDDLE of the period is
    not detected here — the stored series is taken as the market record,
    and known server-side holes are the operator's period-selection concern.
    """
    if bounds is None:
        raise SystemExit(
            f"no stored ticks in [{read_from}, {end}); "
            "collect or backfill the period first"
        )
    first, last = bounds
    if last.time < start:
        raise SystemExit(
            f"no stored ticks inside the evaluation period [{start}, {end}); "
            "only warm-up ticks were found"
        )
    _ensure_head_covered(first, read_from)
    _ensure_tail_covered(last, end)


def covered_reconstructed_stream(
    ticks: Iterator[Tick],
    read_from: datetime,
    start: datetime,
    end: datetime,
    anchor: timedelta,
    digest: TickDigest,
) -> Iterator[Tick]:
    """The replay input: reconstruction, digest and coverage in one pass.

    The pre-flight bounds check reads a different snapshot than the pinned
    stream, so the authoritative coverage verdict is rendered on what was
    actually streamed: the head gap fails on the first tick (before hours
    are spent), the tail and emptiness at exhaustion.
    """
    first: Tick | None = None
    last: Tick | None = None
    for tick in ticks:
        if first is None:
            first = tick
            _ensure_head_covered(first, read_from)
        last = tick
        rewritten = reconstructed_tick(tick, anchor)
        digest.update(rewritten)
        yield rewritten
    ensure_period_covered(
        (first, last) if first is not None and last is not None else None,
        read_from,
        start,
        end,
    )


def with_progress(
    ticks: Iterator[Tick],
    store: InMemoryFeatureStore,
    out: TextIO,
) -> Iterator[Tick]:
    """One line per replay day, on the broker-label axis --from/--to use.

    A months-long run reads tens of millions of ticks over hours with nothing
    to show for it until the manifest, and an operator cannot tell a slow run
    from a stuck one. The feature values travel with the line because they are
    what the fundamental gates read: a run that ends at zero fills is usually
    a run whose gates never opened, and that is worth seeing while it happens
    rather than afterwards.

    Each line is emitted AFTER the engine has consumed the day's first tick,
    so the features are the ones that tick was evaluated against rather than
    the previous day's.
    """
    day: date | None = None
    for count, tick in enumerate(ticks, start=1):
        yield tick
        if tick.time.date() != day:
            day = tick.time.date()
            features = " ".join(
                f"{name}={value:g}" for name, value in sorted(store.values().items())
            )
            print(
                f"{day} {count:>12,} ticks  {features or '(no features)'}",
                file=out,
                flush=True,
            )


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
        type=broker_label,
        required=True,
        help="period start, broker-clock (the axis event_time is stored on)",
    )
    parser.add_argument(
        "--to",
        dest="end",
        type=broker_label,
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
        PostgresSwapSnapshotRepository,
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
    repository = PostgresMarketTickRepository(conn)
    # Fast feedback only: this reads its own snapshot, so the authoritative
    # coverage verdict is rendered inside the stream on the pinned set.
    ensure_period_covered(
        repository.bounds_between(symbol, read_from, args.end),
        read_from,
        args.start,
        args.end,
    )

    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)
    digest = TickDigest()

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

    # 期間終端までに見えていた snapshot を一括ロードし、boundary ごとの
    # latest-known 参照は engine 側の in-memory timeline が行う
    # （known_at <= boundary の PIT 判定込み、ADR-016）。
    swap_snapshots = PostgresSwapSnapshotRepository(conn).known_before(
        symbol, known_end
    )

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
        swap_snapshots=swap_snapshots,
        broker_server_ahead_of_ny_hours=config.market.broker_server_ahead_of_ny_hours,
    )
    # Reproduction inputs are captured before the replay: a long run must
    # record the code state it started under, not whatever the worktree
    # holds hours later when the manifest is written.
    repro = git_state()
    result = engine.run_stream(
        with_progress(
            covered_reconstructed_stream(
                repository.stream_between(symbol, read_from, args.end),
                read_from,
                args.start,
                args.end,
                anchor,
                digest,
            ),
            timeline.store,
            sys.stderr,
        )
    )

    manifest = {
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        **repro,
        "environment": args.env,
        "symbol": symbol,
        "strategy_id": strategy_class.strategy_id,
        "strategy_version": strategy_class.strategy_version,
        "engine_version": ENGINE_VERSION,
        "scenario": scenario,
        "seed": seed,
        "tick_count": digest.count,
        "period_from": args.start.isoformat(),
        "period_to": args.end.isoformat(),
        "warmup_days": warmup / timedelta(days=1),
        "broker_server_ahead_of_ny_hours": (
            config.market.broker_server_ahead_of_ny_hours
        ),
        "dataset_hash": digest.hexdigest(),
        # Ticks alone do not identify the dataset: the stored PIT rows decide
        # what the gates saw, and re-collection changes them under the same
        # tick series.
        "feature_dataset_hash": source.dataset_fingerprint(known_start, known_end),
        # Swap snapshot 列も結果を決める入力: 同じ tick / feature でも
        # snapshot の欠落・差異で carry が変わるため、消費した内容の
        # fingerprint を残す（ADR-016）。
        "swap_dataset_hash": swap_dataset_fingerprint(swap_snapshots),
        "config_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        "python_version": sys.version.split()[0],
    }
    run_dir = write_report(result, manifest, Path(args.out))

    print(json.dumps({"run_dir": str(run_dir), **result.metrics}, indent=2))


if __name__ == "__main__":
    main()
