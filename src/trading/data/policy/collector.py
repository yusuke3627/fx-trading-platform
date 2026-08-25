"""Policy meeting ingest CLI.

Loads the curated meeting facts, scores them mechanically and appends the
resulting events to the PIT store. Event ids are deterministic per
(bank, meeting, scoring version), so re-running after editing the yaml only
inserts what is new.

Usage:

    python -m trading.data.policy.collector --env demo
    python -m trading.data.policy.collector --env demo \
        --meetings config/policy_meetings.yaml
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from trading.backtest.clock import SystemClock
from trading.data.policy.meetings import DEFAULT_MEETINGS_PATH, load_meetings, load_schedule
from trading.data.policy.scoring import event_from_meeting


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Policy meeting ingest")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--meetings", type=Path, default=DEFAULT_MEETINGS_PATH)
    args = parser.parse_args()

    config = load_config(args.env)
    clock = SystemClock()

    meetings = load_meetings(args.meetings)
    # Loading the schedule: section validates it too — results transcribed
    # onto a schedule entry instead of moved into meetings: fail this daily
    # run loudly instead of leaving the meeting silently unscored forever.
    schedule = load_schedule(args.meetings)
    # The other way to lose a meeting is to never transcribe it at all: once
    # its publication bound has passed, the results exist and belong in
    # meetings:, so a stale schedule entry is a failure, not a note.
    stale = [s for s in schedule if s.latest_published_at < clock.now()]
    if stale:
        overdue = ", ".join(f"{s.bank} {s.decision_date}" for s in stale)
        raise SystemExit(f"schedule entries past publication, results not transcribed: {overdue}")

    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")
    events = [event_from_meeting(meeting, clock) for meeting in meetings]

    # Imported here so the module stays usable (and unit-testable) without the
    # db extra installed; psycopg is only needed once a connection is opened.
    from trading.storage.postgres import PostgresEventRepository, connect

    repository = PostgresEventRepository(connect(dsn))
    stored = sum(1 for event in events if repository.insert_new(event))
    print(f"policy: {len(meetings)} meetings, stored {stored} new events")


if __name__ == "__main__":
    main()
