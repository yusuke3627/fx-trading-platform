"""Economic release collection CLI.

One source per run; each run archives the raw responses (events table) and
stores parsed observations (macro_observations), where the vintage uniqueness
key makes re-runs pick up only what is new.

Usage:

    python -m trading.data.macro.collector --env demo --source alfred
    python -m trading.data.macro.collector --env demo --source alfred \
        --series us_cpi_headline_sa --observation-start 2015-01-01
    python -m trading.data.macro.collector --env demo --source bls
    python -m trading.data.macro.collector --env demo --source bea
    python -m trading.data.macro.collector --env demo --source census
    python -m trading.data.macro.collector --env demo --source boe
    python -m trading.data.macro.collector --env demo --source boe_ois
    python -m trading.data.macro.collector --env demo --source ons
    python -m trading.data.macro.collector --env demo --source ecb
    python -m trading.data.macro.collector --env demo --source eurostat
"""
from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from datetime import date

from trading.backtest.clock import SystemClock
from trading.data.macro import alfred, bea, bls, boe, boe_yield_curve, census, ecb, eurostat, ons
from trading.data.macro.base import CollectionBatch
from trading.data.macro.http import HttpTransport
from trading.storage.repository import EventRepository, MacroObservationRepository

SOURCES = ("alfred", "bls", "bea", "census", "boe", "boe_ois", "ons", "ecb", "eurostat")


def _require_key(env_name: str) -> str:
    key = os.environ.get(env_name)
    if not key:
        raise SystemExit(f"{env_name} is not set")
    return key


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Economic release collector")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--source", required=True, choices=SOURCES)
    parser.add_argument(
        "--series",
        action="append",
        default=None,
        help="canonical series name (repeatable); default: all the source supports",
    )
    parser.add_argument(
        "--observation-start",
        type=date.fromisoformat,
        default=None,
        help="ALFRED only: limit vintage history to observations from this date",
    )
    args = parser.parse_args()

    if args.series and args.source in ("bea", "census", "boe", "boe_ois"):
        parser.error(f"--series is not supported for {args.source} (single-series source)")
    if args.observation_start and args.source != "alfred":
        parser.error("--observation-start applies to --source alfred only")

    config = load_config(args.env)
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    # Imported here so the module stays usable (and unit-testable) without the
    # db extra installed; psycopg is only needed once a connection is opened.
    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresMacroObservationRepository,
        connect,
    )

    # Connect before fetching, and store batch by batch: a full ALFRED vintage
    # history is a long pull, and work stored before a mid-run failure is kept
    # (the vintage uniqueness key makes the re-run skip it).
    conn = connect(dsn)
    observation_repo = PostgresMacroObservationRepository(conn)
    event_repo = PostgresEventRepository(conn)

    clock = SystemClock()
    transport = HttpTransport()
    keys = config.macro_data
    # Forward collectors span last year and this year so a January run still
    # returns data and revision pickups reach back a full cycle.
    current_year = clock.now().year
    years = [current_year - 1, current_year]

    def batches() -> Iterator[CollectionBatch]:
        if args.source == "alfred":
            collector = alfred.AlfredCollector(
                transport, _require_key(keys.fred_api_key_env), clock=clock
            )
            for name in args.series or list(alfred.SERIES_IDS):
                yield collector.collect(name, observation_start=args.observation_start)
        elif args.source == "bls":
            bls_collector = bls.BLSCollector(
                transport, os.environ.get(keys.bls_api_key_env), clock=clock
            )
            names = args.series or list(bls.SERIES_IDS)
            yield bls_collector.collect(names, years)
        elif args.source == "bea":
            bea_collector = bea.BEACollector(
                transport, _require_key(keys.bea_api_key_env), clock=clock
            )
            yield bea_collector.collect(years)
        elif args.source == "census":
            census_collector = census.CensusCollector(
                transport, _require_key(keys.census_api_key_env), clock=clock
            )
            yield census_collector.collect(years)
        # 以下の5ソースは API キー不要（transport の User-Agent のみ必要）。
        elif args.source == "boe":
            yield boe.BOECollector(transport, clock=clock).collect(years)
        elif args.source == "boe_ois":
            yield boe_yield_curve.BOEYieldCurveCollector(
                transport, clock=clock
            ).collect(years)
        elif args.source == "ons":
            ons_collector = ons.ONSCollector(transport, clock=clock)
            for name in args.series or list(ons.SERIES):
                yield ons_collector.collect(name, years)
        elif args.source == "ecb":
            ecb_collector = ecb.ECBCollector(transport, clock=clock)
            for name in args.series or list(ecb.SERIES_KEYS):
                yield ecb_collector.collect(name, years)
        else:
            eurostat_collector = eurostat.EurostatCollector(transport, clock=clock)
            for name in args.series or list(eurostat.SERIES):
                yield eurostat_collector.collect(name, years)

    parsed = 0
    stored = 0
    for batch in batches():
        parsed += len(batch.observations)
        stored += _store(batch, observation_repo, event_repo)
    print(f"{args.source}: parsed {parsed} observations, stored {stored} new")


def _store(
    batch: CollectionBatch,
    observation_repo: MacroObservationRepository,
    event_repo: EventRepository,
) -> int:
    for event in batch.raw_events:
        event_repo.insert(event)
    return observation_repo.insert_many(batch.observations)


if __name__ == "__main__":
    main()
