"""Argument types shared by the collector entry points."""
from __future__ import annotations

import argparse
import math


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
