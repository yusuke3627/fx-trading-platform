"""Argument types shared by the collector and research entry points."""
from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime


def aware_utc(value: str) -> datetime:
    """An ISO timestamp with an explicit timezone, normalized to UTC.

    A naive input is rejected rather than assumed: MT5 reads a naive datetime
    as local time, which would silently shift a requested range.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no timezone; a naive timestamp would be read as "
            "local time and shift the requested range"
        )
    return parsed.astimezone(UTC)


def poll_interval(value: str) -> float:
    """A positive, finite number of seconds between passes.

    Zero or negative would run the loop without pause, and nothing downstream
    would object: the broker call, the visibility query and the insert are all
    valid on their own. The failure only shows up as a process hammering the
    terminal and the database, so it is rejected where it enters.
    """
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a positive interval; a zero or negative value "
            "would poll without pause or fail only once the loop sleeps"
        )
    return seconds
