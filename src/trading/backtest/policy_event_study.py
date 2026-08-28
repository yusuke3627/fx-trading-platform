"""Does the policy signal predict USD/JPY at all?

    python -m trading.backtest.policy_event_study --env backtest

The swing strategy pairs a fundamental gate (BOJ hawkish AND Fed dovish AND
intervention risk) with a technical entry. Over the stored archive the gate
opened for about fifty days in twenty-five months and the entry never fired,
so nothing has yet been measured about the prior the whole design rests on:
that a policy divergence is followed by a lower USD/JPY. Tuning the entry
before that is optimisation against a state variable of unknown value.

This measures the prior alone, with no technical condition anywhere.

**The observation is a meeting, not a day.** A score is a step that holds
until the next meeting, so a run of two hundred days carrying it is not two
hundred signals — the information arrived a handful of times. Counting days
would inflate a few episodes into a significant-looking sample. Every
observation here is one published decision.

**Windows do not overlap.** Two meetings three weeks apart share nineteen of
their twenty forward days, and averaging both counts the same price move
twice. Each horizon keeps a greedy non-overlapping subset, which is what lets
the spread across observations mean anything.

**The groups partition.** "BOJ hawkish" that includes the both-legs cases
cannot separate the legs. A meeting lands in exactly one of: both legs, BOJ
leg alone, Fed leg alone, neither.

Sample size is the binding constraint and no amount of method fixes it:
thirty-odd meetings, of which any one horizon keeps a dozen. Read the sign,
the effect size and whether the result survives dropping an episode — a p
value computed here would be theatre.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading.backtest.research import broker_label_to_known
from trading.data.policy.scoring import EVENT_TYPES
from trading.domain.market import Bar
from trading.intelligence import features as f

HORIZONS = (5, 10, 20)
# Daily bars of the whole archive fit well inside this; a minute series does
# not, and reading its tail would study a different window than the one asked
# for.
BAR_LIMIT = 100_000
BOOTSTRAP_SAMPLES = 2_000
BOOTSTRAP_LEVEL = 0.90
BOOTSTRAP_SEED = 20260828

BOTH_LEGS = "BOJ>0 & Fed<0"
BOJ_LEG = "BOJ>0 & Fed>=0"
FED_LEG = "BOJ<=0 & Fed<0"
NEITHER = "neither leg"
GROUPS = (BOTH_LEGS, BOJ_LEG, FED_LEG, NEITHER)


def classify(boj: float, fed: float) -> str:
    """Which single group a meeting's resulting state belongs to.

    Exclusive by construction: overlapping groups cannot tell a leg's own
    contribution from the pair's.
    """
    if boj > 0 and fed < 0:
        return BOTH_LEGS
    if boj > 0:
        return BOJ_LEG
    if fed < 0:
        return FED_LEG
    return NEITHER


@dataclass(frozen=True)
class Observation:
    """One published decision, and what the price did after it."""

    at: datetime
    entry_index: int
    group: str
    divergence: float
    intervention: bool
    # Horizon (in bars) -> log return, and the worst/best the window reached
    # against and for a short. A mean return says nothing about whether a
    # stop would have been taken out first.
    returns: dict[int, float]
    adverse: dict[int, float]
    favorable: dict[int, float]


@dataclass(frozen=True)
class Stats:
    count: int
    mean: float
    median: float
    hit_rate: float
    adverse: float
    favorable: float
    low: float
    high: float


def entry_bar(bars: Sequence[Bar], known_at: datetime, anchor: timedelta) -> int | None:
    """The first candle that CLOSED after the decision was known.

    Its close is the earliest daily price the decision could have been acted
    on: the news was already public when it printed. The candle usually
    spans the announcement, which does not matter — nothing before its close
    is entered at.

    Bars are bucketed on the broker's labels and the decision carries a real
    UTC instant (ADR-005), so the two are compared through the same
    reconstruction a replay uses (ADR-014).
    """
    for index, bar in enumerate(bars):
        if broker_label_to_known(bar.close_time, anchor) > known_at:
            return index
    return None


def window_outcome(
    bars: Sequence[Bar], entry: int, horizon: int
) -> tuple[float, float, float] | None:
    """Log return, worst adverse and best favourable excursion for a SHORT.

    Negative is yen appreciation, which is the direction the policy thesis
    predicts.
    """
    exit_index = entry + horizon
    if exit_index >= len(bars):
        return None
    open_price = float(bars[entry].close)
    window = bars[entry + 1 : exit_index + 1]
    ret = math.log(float(bars[exit_index].close) / open_price)
    adverse = math.log(max(float(b.high) for b in window) / open_price)
    favorable = math.log(min(float(b.low) for b in window) / open_price)
    return ret, adverse, favorable


def thin(observations: Sequence[Observation], horizon: int) -> list[Observation]:
    """The earliest observations whose forward windows do not overlap.

    Two meetings inside one horizon share almost all of their forward days;
    keeping both would average the same price move twice and make a handful
    of episodes look like an independent sample.
    """
    kept: list[Observation] = []
    free_from = -1
    for observation in sorted(observations, key=lambda o: o.entry_index):
        if observation.entry_index >= free_from:
            kept.append(observation)
            free_from = observation.entry_index + horizon
    return kept


def bootstrap_interval(
    values: Sequence[float], seed: int, level: float = BOOTSTRAP_LEVEL
) -> tuple[float, float]:
    """Percentile interval for the mean, resampling the observations."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values)))
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    tail = (1.0 - level) / 2.0
    low = means[int(tail * (len(means) - 1))]
    high = means[int((1.0 - tail) * (len(means) - 1))]
    return (low, high)


def summarize(observations: Sequence[Observation], horizon: int, seed: int) -> Stats:
    returns = [o.returns[horizon] for o in observations]
    if not returns:
        return Stats(0, *([float("nan")] * 7))
    low, high = bootstrap_interval(returns, seed)
    return Stats(
        count=len(returns),
        mean=statistics.fmean(returns),
        median=statistics.median(returns),
        hit_rate=sum(1 for r in returns if r < 0) / len(returns),
        adverse=statistics.fmean([o.adverse[horizon] for o in observations]),
        favorable=statistics.fmean([o.favorable[horizon] for o in observations]),
        low=low,
        high=high,
    )


def unconditional(bars: Sequence[Bar], horizon: int, seed: int) -> Stats:
    """The same measurement on every non-overlapping window in the series.

    Without it a negative mean reads as a signal when it may be the drift of
    the period the archive happens to cover.
    """
    outcomes = [
        window_outcome(bars, entry, horizon)
        for entry in range(0, len(bars) - horizon, horizon)
    ]
    observations = [
        Observation(
            at=bars[0].start,
            entry_index=index,
            group=NEITHER,
            divergence=0.0,
            intervention=False,
            returns={horizon: outcome[0]},
            adverse={horizon: outcome[1]},
            favorable={horizon: outcome[2]},
        )
        for index, outcome in enumerate(outcomes)
        if outcome is not None
    ]
    return summarize(observations, horizon, seed)


def divergence_slope(observations: Sequence[Observation], horizon: int) -> float:
    """Least squares slope of the forward return on BOJ minus Fed.

    The thesis says a wider divergence is followed by a lower USD/JPY, so it
    predicts a negative slope. Reported as an effect size; with this many
    observations a standard error would promise a precision that is not there.
    """
    xs = [o.divergence for o in observations]
    ys = [o.returns[horizon] for o in observations]
    if len(xs) < 2:
        return float("nan")
    mean_x = statistics.fmean(xs)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return float("nan")
    mean_y = statistics.fmean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return covariance / variance


def _row(label: str, stats: Stats) -> str:
    if stats.count == 0:
        return f"  {label:<22}      0"
    return (
        f"  {label:<22} {stats.count:>6} "
        f"{stats.mean * 100:>8.2f} {stats.median * 100:>8.2f} "
        f"{stats.hit_rate * 100:>6.0f} "
        f"{stats.adverse * 100:>7.2f} {stats.favorable * 100:>7.2f}  "
        f"[{stats.low * 100:>6.2f},{stats.high * 100:>6.2f}]"
    )


def report(observations: Sequence[Observation], bars: Sequence[Bar]) -> str:
    lines = [
        (
            f"{len(observations)} policy decisions with forward data, "
            f"{len(bars)} daily bars"
        ),
        (
            "return/median/adverse/favourable in %, negative = yen "
            "appreciation = short USD/JPY wins"
        ),
        "hit = share of windows that ended lower; CI = 90% bootstrap of the mean",
        "",
    ]
    quiet = [o for o in observations if not o.intervention]
    active = [o for o in observations if o.intervention]
    for horizon in HORIZONS:
        seed = BOOTSTRAP_SEED + horizon
        lines.append(f"horizon {horizon} bars (non-overlapping)")
        lines.append(
            f"  {'':<22} {'n':>6} {'mean':>8} {'median':>8} {'hit':>6} "
            f"{'adverse':>7} {'favour':>7}  CI90"
        )
        for group in GROUPS:
            kept = thin([o for o in quiet if o.group == group], horizon)
            lines.append(_row(group, summarize(kept, horizon, seed)))
        lines.append(_row("unconditional", unconditional(bars, horizon, seed)))
        kept_active = thin(active, horizon)
        lines.append(_row("intervention active", summarize(kept_active, horizon, seed)))
        slope = divergence_slope(thin(quiet, horizon), horizon)
        lines.append(f"  divergence slope: {slope * 100:>+7.3f} % per point")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    from trading.config import load_config
    from trading.data.features import StoredFeatureSource
    from trading.intelligence.features import InMemoryFeatureStore
    from trading.intelligence.intervention import InterventionRiskConfig

    parser = argparse.ArgumentParser(description="Policy signal event study")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default="1d")
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresMacroObservationRepository,
        PostgresMarketBarRepository,
        connect,
    )

    conn = connect(dsn)
    events_repository = PostgresEventRepository(conn)
    now = datetime.now(UTC)
    bars = list(
        PostgresMarketBarRepository(conn).known_before(
            symbol, args.timeframe, now, BAR_LIMIT
        )
    )
    if not bars:
        raise SystemExit(
            f"no {args.timeframe} bars stored for {symbol}: run the bar "
            "service with --backfill first"
        )
    if len(bars) == BAR_LIMIT:
        # Reading the tail of a longer series would silently study a window
        # that is not the one the operator asked about.
        raise SystemExit(
            f"{args.timeframe} has at least {BAR_LIMIT} bars, more than this "
            "reads at once; study a coarser timeframe"
        )

    source = StoredFeatureSource(
        PostgresMacroObservationRepository(conn),
        events_repository,
        InterventionRiskConfig(
            version=config.intelligence.intervention_risk.version,
            weights=config.intelligence.intervention_risk.weights,
        ),
        InMemoryFeatureStore(),
    )
    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)

    decisions = sorted(
        (
            event
            for event_type in EVENT_TYPES.values()
            for event in events_repository.known_before(now, event_type)
        ),
        key=lambda event: event.known_at,
    )

    observations: list[Observation] = []
    for decision in decisions:
        # The scores and the intervention risk are read through the same
        # source a replay reads, at the instant the decision became known.
        values = source.snapshot(decision.known_at)
        boj = values.get(f.BOJ_POLICY_SHIFT_SCORE)
        fed = values.get(f.FED_POLICY_SHIFT_SCORE)
        if boj is None or fed is None:
            continue
        entry = entry_bar(bars, decision.known_at, anchor)
        if entry is None:
            continue
        outcomes = {
            horizon: window_outcome(bars, entry, horizon) for horizon in HORIZONS
        }
        if any(outcome is None for outcome in outcomes.values()):
            continue
        observations.append(
            Observation(
                at=decision.known_at,
                entry_index=entry,
                group=classify(boj, fed),
                divergence=boj - fed,
                intervention=values.get(f.INTERVENTION_RISK) is not None,
                returns={h: outcomes[h][0] for h in HORIZONS},
                adverse={h: outcomes[h][1] for h in HORIZONS},
                favorable={h: outcomes[h][2] for h in HORIZONS},
            )
        )

    print(report(observations, bars))


if __name__ == "__main__":
    main()
