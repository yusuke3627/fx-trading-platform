"""Synthetic deterministic market data for the vertical-slice backtest.

Real bid/ask tick archives are collected over time (OANDA's historical tick
feed is volume-gated); until they exist, the pipeline is proven against a
seeded random walk: same seed -> byte-identical dataset -> reproducible run.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from decimal import Decimal

from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Tick


def synthetic_ticks(
    *,
    spec: InstrumentSpec,
    start: datetime,
    count: int,
    seed: int,
    initial_mid: Decimal = Decimal("159.000"),
    spread_pips: Decimal = Decimal("0.6"),
    step_pips: Decimal = Decimal("0.4"),
    interval_seconds: int = 1,
) -> list[Tick]:
    """Seeded random-walk bid/ask series at a fixed interval."""
    if start.tzinfo is None:
        raise ValueError("synthetic_ticks requires an aware start datetime")
    rng = random.Random(seed)
    quantum = Decimal(10) ** -spec.digits
    half_spread = (spread_pips * spec.pip_size / 2).quantize(quantum)
    ticks: list[Tick] = []
    mid = initial_mid
    for i in range(count):
        move = Decimal(str(round(rng.uniform(-1.0, 1.0), 3))) * step_pips * spec.pip_size
        mid = (mid + move).quantize(quantum)
        ticks.append(
            Tick(
                symbol=spec.symbol,
                bid=mid - half_spread,
                ask=mid + half_spread,
                time=start + timedelta(seconds=i * interval_seconds),
            )
        )
    return ticks


class TickDigest:
    """Streaming counterpart of dataset_hash, with a tick counter.

    A streamed research run sees each tick exactly once, so the manifest's
    dataset identity and size are accumulated as the ticks pass instead of
    demanding the materialized list dataset_hash takes.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.count = 0

    def update(self, tick: Tick) -> None:
        received = tick.received_at.isoformat() if tick.received_at else ""
        self._digest.update(
            f"{tick.symbol}|{tick.time.isoformat()}|{received}"
            f"|{tick.bid}|{tick.ask}\n".encode()
        )
        self.count += 1

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def dataset_hash(ticks: list[Tick]) -> str:
    """Content hash of a tick dataset for the run manifest."""
    digest = TickDigest()
    for t in ticks:
        digest.update(t)
    return digest.hexdigest()
