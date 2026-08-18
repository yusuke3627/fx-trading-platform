"""Intervention data ingest CLI.

Runs the three layers in one pass: MOF daily history, MOF monthly totals and
the curated recognition timeline. Event ids are deterministic, so re-running
only inserts what is new.

Usage:

    python -m trading.data.intervention.collector --env demo
    python -m trading.data.intervention.collector --env demo \
        --monthly-since 1991-04-01   # full monthly backfill (one-time)
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from trading.backtest.clock import SystemClock
from trading.data.intervention.episodes import (
    DEFAULT_EPISODES_PATH,
    event_from_recognition,
    load_episodes,
)
from trading.data.intervention.mof import MOFDailyCollector, MOFMonthlyCollector
from trading.data.macro.http import HttpTransport

# Routine runs only need the publications the vintage window can still be
# waiting on; history is a one-time --monthly-since backfill.
DEFAULT_MONTHLY_LOOKBACK = timedelta(days=90)


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Intervention data ingest")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--monthly-since", type=date.fromisoformat, default=None)
    parser.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES_PATH)
    args = parser.parse_args()

    config = load_config(args.env)
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    clock = SystemClock()
    transport = HttpTransport()
    monthly_since = args.monthly_since or (clock.now().date() - DEFAULT_MONTHLY_LOOKBACK)

    # Imported here so the module stays usable (and unit-testable) without the
    # db extra installed; psycopg is only needed once a connection is opened.
    from trading.storage.postgres import PostgresEventRepository, connect

    repository = PostgresEventRepository(connect(dsn))

    daily = MOFDailyCollector(transport, clock=clock).collect()
    monthly = MOFMonthlyCollector(transport, clock=clock).collect(
        published_since=monthly_since
    )
    recognitions = [
        event_from_recognition(entry, clock) for entry in load_episodes(args.episodes)
    ]

    for batch_raw in (*daily.raw_events, *monthly.raw_events):
        repository.insert(batch_raw)
    stored = sum(
        1
        for event in (*daily.events, *monthly.events, *recognitions)
        if repository.insert_new(event)
    )
    parsed = len(daily.events) + len(monthly.events) + len(recognitions)
    print(f"intervention: parsed {parsed} events, stored {stored} new")


if __name__ == "__main__":
    main()
