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

Candles are folded from the stored quotes rather than read from market_bars,
which holds the live series and never corrects a candle folded across a gap
that a later backfill repaired. That costs one pass over the archive, so the
run takes minutes rather than seconds.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TextIO

from trading.backtest.research import broker_label_to_known
from trading.data.market.bars import BarBuilder
from trading.data.policy.scoring import EVENT_TYPES, SCORING_VERSION
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick
from trading.intelligence import features as f

# The thesis, the grouping and every label here are about the yen: a BOJ
# minus Fed divergence says nothing about another pair.
SYMBOL = "USDJPY"
# Horizons are trading days, so the series has to be the daily one: on any
# other the same numbers would silently mean hours.
TIMEFRAME = "1d"
HORIZONS = (5, 10, 20)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The steps between consecutive candles of an unbroken series: a day inside
# the week, three across a weekend.
TRADING_DAY_STEPS = (timedelta(days=1), timedelta(days=3))
# Wider than any closure this market takes (it shuts for New Year and
# Christmas, not for a working week), so a step this size is missing data.
HOLE_MINIMUM = timedelta(days=5)
# Broker labels run ahead of our clock (ADR-005), so the read's end bound
# allows for the anchor.
BROKER_CLOCK_MARGIN = timedelta(days=1)
BOOTSTRAP_SAMPLES = 2_000
BOOTSTRAP_LEVEL = 0.90
BOOTSTRAP_SEED = 20260828

BOTH_LEGS = "BOJ>0 & Fed<0"
BOJ_LEG = "BOJ>0 & Fed>=0"
FED_LEG = "BOJ<=0 & Fed<0"
NEITHER = "neither leg"
GROUPS = (BOTH_LEGS, BOJ_LEG, FED_LEG, NEITHER)


def fold_daily(ticks: Iterator[Tick], symbol: str, progress: TextIO | None) -> list[Bar]:
    """Daily candles folded from the stored quotes, not read from market_bars.

    market_bars holds the LIVE series: each candle from the quotes that had
    arrived when it closed, never corrected afterwards (bar_service says so
    itself, and ON CONFLICT is what enforces it). A gap repaired by a later
    tick backfill leaves the candle that was folded across it wrong for good,
    and the share of the series built that way only grows. A study reading
    them would carry those candles into its returns and excursions.

    The archive is the durable series, so this folds from it the way a replay
    does. It costs one pass over tens of millions of quotes; the measurement
    is worth more than the minutes.
    """
    builder = BarBuilder(symbol, TIMEFRAME)
    bars: list[Bar] = []
    month: tuple[int, int] | None = None
    for count, tick in enumerate(ticks, start=1):
        bar = builder.on_tick(tick)
        if bar is not None:
            bars.append(bar)
        if progress is not None and (tick.time.year, tick.time.month) != month:
            month = (tick.time.year, tick.time.month)
            print(
                f"{month[0]}-{month[1]:02d} {count:>12,} ticks  "
                f"{len(bars):>5} candles",
                file=progress,
                flush=True,
            )
    return bars


def current_version(decisions: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    """Only the meetings scored by the algorithm this build computes.

    A re-tuned scorer re-ingests past meetings as new events rather than
    rewriting them, so one meeting can sit in the store under several
    versions sharing a known_at. The features read the current version
    (StoredFeatureSource does the same filtering), so counting every version
    here would enter the same decision once per version and weight it by how
    often the scorer has been re-tuned.
    """
    return [
        decision
        for decision in decisions
        if decision.payload.get("scoring_version") == SCORING_VERSION
    ]


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

    A decision older than the series has no entry. The policy archive reaches
    back further than the prices do, and taking the first close of the series
    would enter one of them months or years after the announcement while
    labelling the window as its reaction.
    """
    if not bars or known_at < broker_label_to_known(bars[0].start, anchor):
        return None
    for index, bar in enumerate(bars):
        if broker_label_to_known(bar.close_time, anchor) > known_at:
            return index
    return None


def irregular_steps(bars: Sequence[Bar]) -> list[tuple[Bar, Bar]]:
    """Consecutive candles further apart than an unbroken week runs.

    The broker's labels put a week's quotes on Monday to Friday, so candles
    are one day apart inside the week and three across a weekend. A wider
    step is either a day the market took off or one the archive is missing,
    and the series alone cannot tell them apart — which is why every one of
    them is reported rather than assumed.
    """
    return [
        (before, after)
        for before, after in pairwise(bars)
        if (after.start - before.start) not in TRADING_DAY_STEPS
    ]


def gaps(bars: Sequence[Bar]) -> list[tuple[Bar, Bar]]:
    """The steps too wide to be the market closing: data the archive lacks.

    A horizon counted in candles spans one silently — five bars across the
    archive's 2026 hole would be a ten-week move reported as a week — so
    windows crossing these are not measured at all.

    A market closure is left alone instead. The horizons are trading days,
    and a day the market did not trade is not one of them, so a window either
    side of New Year measures exactly what it claims. Dropping those windows
    too would cost a third of the twenty-day sample to fix nothing.
    """
    return [
        (before, after)
        for before, after in irregular_steps(bars)
        if after.start - before.start >= HOLE_MINIMUM
    ]


def collapse_same_entry(observations: Sequence[Observation]) -> list[Observation]:
    """One observation per entry candle, carrying the latest state.

    Both banks can publish before the same close — 2024-07-31 is one such day
    — and that close prices in both. Keeping the earlier decision would pair
    the entry with a policy state that was already superseded when the price
    printed.
    """
    latest: dict[int, Observation] = {}
    for observation in observations:
        held = latest.get(observation.entry_index)
        if held is None or observation.at > held.at:
            latest[observation.entry_index] = observation
    return sorted(latest.values(), key=lambda o: o.entry_index)


def window_outcome(
    bars: Sequence[Bar], entry: int, horizon: int
) -> tuple[float, float, float] | None:
    """Log return, worst adverse and best favourable excursion for a SHORT.

    Negative is yen appreciation, which is the direction the policy thesis
    predicts.

    A window that jumps a hole in the series is not measured: its bars are a
    horizon apart in count but not in time.
    """
    exit_index = entry + horizon
    if exit_index >= len(bars):
        return None
    if gaps(bars[entry : exit_index + 1]):
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

    Decisions the series does not reach this far past are not observations at
    this horizon — but they remain observations at the shorter ones, which is
    why the horizons are filtered here rather than when they are built.
    """
    kept: list[Observation] = []
    free_from = -1
    with_horizon = (o for o in observations if horizon in o.returns)
    for observation in sorted(with_horizon, key=lambda o: o.entry_index):
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


def measured_span(
    observations: Sequence[Observation], bars: Sequence[Bar], horizon: int
) -> Sequence[Bar]:
    """The stretch of the series the decisions actually reach over.

    The price archive can be longer than the policy one at either end, and a
    baseline taken over the whole of it would compare the signal against
    years the signal was never measured in.
    """
    if not observations:
        return ()
    first = min(o.entry_index for o in observations)
    last = max(o.entry_index for o in observations)
    return bars[first : last + horizon + 1]


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
        (
            "unconditional covers the stretch the decisions reach over, not "
            "the whole series"
        ),
        "",
    ]
    holes = gaps(bars)
    for before, after in irregular_steps(bars):
        skipped = (after.start - before.start).days - 1
        kind = (
            "missing data, windows crossing it are not measured"
            if (before, after) in holes
            else "market closed, windows spanning it are still trading days"
        )
        lines.append(
            f"{before.start.date()} -> {after.start.date()} "
            f"({skipped}d): {kind}"
        )
    lines.append("")

    for horizon in HORIZONS:
        seed = BOOTSTRAP_SEED + horizon
        lines.append(f"horizon {horizon} trading days (non-overlapping)")
        lines.append(
            f"  {'':<22} {'n':>6} {'mean':>8} {'median':>8} {'hit':>6} "
            f"{'adverse':>7} {'favour':>7}  CI90"
        )
        # Thinned across every row rather than inside each: a BOJ and a Fed
        # decision days apart fall in different groups but share nearly all
        # of their forward window, and thinning per row would report one
        # price move as evidence for two of them. So no price window appears
        # twice anywhere in the table — at the cost that the earlier decision
        # of such a pair is the one kept.
        kept = thin(observations, horizon)
        quiet = [o for o in kept if not o.intervention]
        for group in GROUPS:
            lines.append(
                _row(
                    group,
                    summarize([o for o in quiet if o.group == group], horizon, seed),
                )
            )
        span = measured_span(kept, bars, horizon)
        lines.append(_row("unconditional", unconditional(span, horizon, seed)))
        lines.append(
            _row(
                "intervention active",
                summarize([o for o in kept if o.intervention], horizon, seed),
            )
        )
        slope = divergence_slope(quiet, horizon)
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
    parser.add_argument("--symbol", default=SYMBOL)
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol
    if symbol != SYMBOL:
        # Another pair's candles would be grouped by a BOJ minus Fed
        # divergence and reported as yen appreciation.
        raise SystemExit(f"this study is about {SYMBOL}, not {symbol}")
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
    events_repository = PostgresEventRepository(conn)
    now = datetime.now(UTC)
    bars = fold_daily(
        PostgresMarketTickRepository(conn).stream_between(
            symbol, EPOCH, now + BROKER_CLOCK_MARGIN
        ),
        symbol,
        sys.stderr,
    )
    if not bars:
        raise SystemExit(f"no stored quotes for {symbol}")

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
        current_version(
            [
                event
                for event_type in EVENT_TYPES.values()
                for event in events_repository.known_before(now, event_type)
            ]
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
        # A decision the series does not reach twenty bars past is still an
        # observation at five: dropping it from every horizon would thin the
        # most recent meetings out of the short ones for no reason.
        outcomes = {
            horizon: outcome
            for horizon in HORIZONS
            if (outcome := window_outcome(bars, entry, horizon)) is not None
        }
        if not outcomes:
            continue
        observations.append(
            Observation(
                at=decision.known_at,
                entry_index=entry,
                group=classify(boj, fed),
                divergence=boj - fed,
                intervention=values.get(f.INTERVENTION_RISK) is not None,
                returns={h: outcome[0] for h, outcome in outcomes.items()},
                adverse={h: outcome[1] for h, outcome in outcomes.items()},
                favorable={h: outcome[2] for h, outcome in outcomes.items()},
            )
        )

    print(report(collapse_same_entry(observations), bars))


if __name__ == "__main__":
    main()
