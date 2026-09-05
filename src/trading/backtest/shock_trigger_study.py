"""実時間で検出できる下落ショック後の USD/JPY 継続性を測る。

    python -m trading.backtest.shock_trigger_study --env backtest --symbol USDJPY

保存 tick を 5 分足へ畳み、各足が閉じるまでに得られた価格だけで下落
ショックを検出する。介入窓、政策決定窓、それ以外を分け、bid 終値同士の
gross と ask 決済の net を同じ標本で比較する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import NamedTuple, TextIO

from trading.backtest.data import TickDigest
from trading.backtest.intervention_event_study import (
    EVENT_TYPE,
    JST,
    SHOCK_WINDOW,
    Episode,
    Horizon,
    load_episodes_from_events,
)
from trading.backtest.policy_event_study import (
    BOOTSTRAP_SEED,
    BROKER_CLOCK_MARGIN,
    EPOCH,
    SYMBOL,
    Stats,
    _row,
    bootstrap_interval,
    current_version,
    unconditional,
    window_outcome,
)
from trading.backtest.research import broker_label_to_known
from trading.backtest.run import git_state
from trading.data.market.bars import BarBuilder, bucket_start
from trading.data.market.dukascopy import known_to_broker_label
from trading.data.policy.scoring import EVENT_TYPES
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick

TIMEFRAME = "5m"
LOOKBACKS = (48, 96, 288)
THRESHOLDS = (3.0, 4.0, 5.0)
POLICY_WINDOW = timedelta(hours=24)

LAYER_INTERVENTION = "A"
LAYER_POLICY = "B"
LAYER_OTHER = "C"
LAYERS = (LAYER_INTERVENTION, LAYER_POLICY, LAYER_OTHER)
LAYER_FIRST = "A-first"

GROSS = "gross"
NET = "net"

H2 = "H2 支持（介入固有）"
H3 = "H3 支持（ショック一般）"
H2_STRONG = "H2 強化（通常ショックは逆張り）"
NOT_TRADABLE = "統計現象だが非取引可能"
REJECTED = "H2/H3 棄却"
VERDICT_HORIZONS = ("1h", "4h")


class Spec(NamedTuple):
    lookback: int
    threshold: float


PRIMARY = Spec(lookback=96, threshold=4.0)
HORIZONS = (
    Horizon("15m", TIMEFRAME, 3),
    Horizon("1h", TIMEFRAME, 12),
    Horizon("4h", TIMEFRAME, 48),
)
FORWARD_BARS = HORIZONS[-1].bars


class InterventionWindow(NamedTuple):
    start: datetime
    end: datetime
    first: bool


class PolicyWindow(NamedTuple):
    start: datetime
    end: datetime


@dataclass(frozen=True)
class QuoteBar:
    bar: Bar
    ask_close: Decimal


@dataclass(frozen=True)
class Provenance:
    tick_count: int
    first_tick: datetime | None
    last_tick: datetime | None
    dataset_hash: str


@dataclass(frozen=True)
class Trigger:
    entry: int
    z: float
    ret: float
    layer: str
    first: bool
    overlap: bool
    spread: Decimal
    returns: dict[str, dict[str, float]]
    adverse: dict[str, float]
    favorable: dict[str, float]


@dataclass(frozen=True)
class CellResult:
    spec: Spec
    triggers: list[Trigger]
    suppressed: int
    invalid: int


def event_fingerprint(events: Sequence[EventEnvelope]) -> str:
    """判定に使ったイベント内容の指紋。件数が同じでも時刻や payload の変更を検出する。"""
    normalized = sorted(
        (
            str(event.event_id),
            event.event_type,
            event.known_at.astimezone(UTC).isoformat(),
            json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
        )
        for event in events
    )
    digest = hashlib.sha256()
    for fields in normalized:
        digest.update("\x1f".join(fields).encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


def fold_quote_bars(
    ticks: Iterator[Tick], symbol: str, progress: TextIO | None
) -> tuple[list[QuoteBar], Provenance]:
    """保存 tick を一度だけ走査し、bid 足と各足の最終 ask を組み立てる。"""
    builder = BarBuilder(symbol, TIMEFRAME)
    digest = TickDigest()
    bars: list[QuoteBar] = []
    first_tick: datetime | None = None
    last_tick: datetime | None = None
    month: tuple[int, int] | None = None
    open_start: datetime | None = None
    last_time: datetime | None = None
    ask: Decimal | None = None

    for count, tick in enumerate(ticks, start=1):
        digest.update(tick)
        if first_tick is None:
            first_tick = tick.time
        last_tick = tick.time

        bar = builder.on_tick(tick)
        if bar is not None:
            bars.append(QuoteBar(bar=bar, ask_close=ask))
            open_start = None

        start = bucket_start(tick.time, TIMEFRAME)
        if open_start is None:
            open_start = start
            last_time = tick.time
            ask = tick.ask
        elif start == open_start and tick.time >= last_time:
            last_time = tick.time
            ask = tick.ask

        current_month = (tick.time.year, tick.time.month)
        if progress is not None and current_month != month:
            month = current_month
            print(
                f"{month[0]}-{month[1]:02d} {count:>12,} ticks  "
                f"{len(bars):>7,} candles",
                file=progress,
                flush=True,
            )

    return bars, Provenance(
        tick_count=digest.count,
        first_tick=first_tick,
        last_tick=last_tick,
        dataset_hash=digest.hexdigest(),
    )


def log_returns(bars: Sequence[QuoteBar]) -> list[float | None]:
    values: list[float | None] = [None]
    for before, after in pairwise(bars):
        if before.bar.close_time != after.bar.start:
            values.append(None)
            continue
        values.append(math.log(float(after.bar.close) / float(before.bar.close)))
    return values


def z_scores(returns: Sequence[float | None], lookback: int) -> list[float | None]:
    window: deque[float] = deque(maxlen=lookback)
    scores: list[float | None] = []
    for current in returns:
        score: float | None = None
        if current is not None and len(window) == lookback:
            mean = sum(window) / lookback
            deviation = math.sqrt(
                sum((value - mean) ** 2 for value in window) / lookback
            )
            if deviation != 0:
                score = (current - mean) / deviation
        scores.append(score)
        if current is not None:
            window.append(current)
    return scores


def intervention_windows(episodes: Sequence[Episode]) -> list[InterventionWindow]:
    return [
        InterventionWindow(
            start=(start := datetime.combine(episode.action_date, time(0), tzinfo=UTC)),
            end=start + SHOCK_WINDOW,
            first=episode.cluster == episode.action_date,
        )
        for episode in episodes
    ]


def policy_windows(
    decisions: Sequence[EventEnvelope], anchor: timedelta
) -> list[PolicyWindow]:
    return [
        PolicyWindow(
            start=(label := known_to_broker_label(decision.known_at, anchor)),
            end=label + POLICY_WINDOW,
        )
        for decision in decisions
    ]


def classify_layer(
    bar: Bar,
    interventions: Sequence[InterventionWindow],
    policies: Sequence[PolicyWindow],
) -> tuple[str, bool, bool]:
    in_a = any(window.start <= bar.start < window.end for window in interventions)
    first = any(
        window.first and window.start <= bar.start < window.end
        for window in interventions
    )
    in_b = any(
        window.start < bar.close_time and bar.start < window.end for window in policies
    )
    layer = LAYER_INTERVENTION if in_a else LAYER_POLICY if in_b else LAYER_OTHER
    return layer, first if in_a else False, in_a and in_b


def detect(
    bars: Sequence[QuoteBar],
    bid_bars: Sequence[Bar],
    returns: Sequence[float | None],
    z: Sequence[float | None],
    spec: Spec,
    interventions: Sequence[InterventionWindow],
    policies: Sequence[PolicyWindow],
) -> CellResult:
    triggers: list[Trigger] = []
    suppressed = 0
    invalid = 0
    last: int | None = None

    for entry, score in enumerate(z):
        current_return = returns[entry]
        if (
            score is None
            or current_return is None
            or score >= -spec.threshold
            or current_return >= 0
        ):
            continue
        if last is not None and entry <= last + FORWARD_BARS:
            suppressed += 1
            continue
        last = entry
        if entry + FORWARD_BARS >= len(bars) or any(
            value is None for value in returns[entry + 1 : entry + FORWARD_BARS + 1]
        ):
            invalid += 1
            continue

        gross: dict[str, float] = {}
        net: dict[str, float] = {}
        adverse: dict[str, float] = {}
        favorable: dict[str, float] = {}
        entry_bid = bars[entry].bar.close
        for horizon in HORIZONS:
            outcome = window_outcome(bid_bars, entry, horizon.bars)
            gross[horizon.label] = outcome[0]
            adverse[horizon.label] = outcome[1]
            favorable[horizon.label] = outcome[2]
            net[horizon.label] = math.log(
                float(bars[entry + horizon.bars].ask_close) / float(entry_bid)
            )

        layer, first, overlap = classify_layer(
            bars[entry].bar, interventions, policies
        )
        triggers.append(
            Trigger(
                entry=entry,
                z=score,
                ret=current_return,
                layer=layer,
                first=first,
                overlap=overlap,
                spread=bars[entry].ask_close - entry_bid,
                returns={GROSS: gross, NET: net},
                adverse=adverse,
                favorable=favorable,
            )
        )

    return CellResult(
        spec=spec,
        triggers=triggers,
        suppressed=suppressed,
        invalid=invalid,
    )


def _matches_layer(trigger: Trigger, layer_filter: str) -> bool:
    if layer_filter == LAYER_FIRST:
        return trigger.layer == LAYER_INTERVENTION and trigger.first
    return trigger.layer == layer_filter


def cell_stats(
    triggers: Sequence[Trigger],
    layer_filter: str,
    kind: str,
    horizon: Horizon,
    seed: int,
) -> Stats:
    selected = [trigger for trigger in triggers if _matches_layer(trigger, layer_filter)]
    if not selected:
        return Stats(0, *([float("nan")] * 7))
    values = [trigger.returns[kind][horizon.label] for trigger in selected]
    low, high = bootstrap_interval(values, seed)
    return Stats(
        count=len(selected),
        mean=statistics.fmean(values),
        median=statistics.median(values),
        hit_rate=sum(value < 0 for value in values) / len(values),
        adverse=statistics.fmean(trigger.adverse[horizon.label] for trigger in selected),
        favorable=statistics.fmean(
            trigger.favorable[horizon.label] for trigger in selected
        ),
        low=low,
        high=high,
    )


def _baseline(cell: CellResult, bid_bars: Sequence[Bar], horizon: Horizon, seed: int) -> Stats:
    if not cell.triggers:
        return Stats(0, *([float("nan")] * 7))
    first = min(trigger.entry for trigger in cell.triggers)
    last = max(trigger.entry for trigger in cell.triggers)
    span = bid_bars[first : last + horizon.bars + 1]
    return unconditional(span, horizon.bars, seed)


def negative_excluding_zero(stats: Stats) -> bool:
    return stats.high < 0


def positive_excluding_zero(stats: Stats) -> bool:
    return stats.low > 0


def includes_zero(stats: Stats) -> bool:
    return stats.low <= 0 <= stats.high


def judge(a_net: Stats, c_net: Stats, a_gross: Stats) -> str | None:
    """固定した判定表を上から順に一つのホライズンへ適用する。"""
    if negative_excluding_zero(a_net) and includes_zero(c_net):
        return H2
    if negative_excluding_zero(a_net) and negative_excluding_zero(c_net):
        return H3
    if negative_excluding_zero(a_net) and positive_excluding_zero(c_net):
        return H2_STRONG
    if negative_excluding_zero(a_gross) and includes_zero(a_net):
        return NOT_TRADABLE
    return None


def verdict(cell: CellResult, seed: int) -> tuple[str, str | None]:
    """1h、4h の順で最初に該当した固定判定を返す。"""
    for index, horizon in enumerate(HORIZONS):
        if horizon.label not in VERDICT_HORIZONS:
            continue
        result = judge(
            cell_stats(cell.triggers, LAYER_INTERVENTION, NET, horizon, seed + index),
            cell_stats(cell.triggers, LAYER_OTHER, NET, horizon, seed + index),
            cell_stats(
                cell.triggers, LAYER_INTERVENTION, GROSS, horizon, seed + index
            ),
        )
        if result is not None:
            return result, horizon.label
    return REJECTED, None


def _grid_stat(stats: Stats) -> str:
    if stats.count == 0:
        return "      - [     -,     -]"
    return f"{stats.mean * 100:>7.2f} [{stats.low * 100:>6.2f},{stats.high * 100:>6.2f}]"


def _grid_lines(cells: Sequence[CellResult], seed: int) -> list[str]:
    horizon = next(item for item in HORIZONS if item.label == "1h")
    horizon_seed = seed + HORIZONS.index(horizon)
    lines = [
        "grid summary (net 1h mean and CI90 by layer)",
        (
            "   N  K  triggers  suppr  invalid     A  A-first     B     C   A&B   "
            "A mean [CI90]           B mean [CI90]           C mean [CI90]"
        ),
    ]
    for cell in cells:
        counts = {
            layer: sum(_matches_layer(trigger, layer) for trigger in cell.triggers)
            for layer in (*LAYERS, LAYER_FIRST)
        }
        stats = {
            layer: cell_stats(cell.triggers, layer, NET, horizon, horizon_seed)
            for layer in LAYERS
        }
        overlap = sum(trigger.overlap for trigger in cell.triggers)
        lines.append(
            f"{cell.spec.lookback:>4} {cell.spec.threshold:>2.0f} "
            f"{len(cell.triggers):>9} {cell.suppressed:>6} {cell.invalid:>8} "
            f"{counts[LAYER_INTERVENTION]:>5} {counts[LAYER_FIRST]:>8} "
            f"{counts[LAYER_POLICY]:>5} {counts[LAYER_OTHER]:>5} {overlap:>5}   "
            f"{_grid_stat(stats[LAYER_INTERVENTION])}   "
            f"{_grid_stat(stats[LAYER_POLICY])}   {_grid_stat(stats[LAYER_OTHER])}"
        )
    return lines


def _summary_lines(cell: CellResult, bid_bars: Sequence[Bar], seed: int) -> list[str]:
    lines = [f"primary N={PRIMARY.lookback} K={PRIMARY.threshold:g}"]
    for index, horizon in enumerate(HORIZONS):
        horizon_seed = seed + index
        lines.extend(
            [
                f"horizon {horizon.label} ({horizon.bars} x {TIMEFRAME})",
                (
                    f"  {'':<22} {'n':>6} {'mean':>8} {'median':>8} {'hit':>6} "
                    f"{'adverse':>7} {'favour':>7}  CI90"
                ),
            ]
        )
        for layer in (LAYER_INTERVENTION, LAYER_FIRST, LAYER_POLICY, LAYER_OTHER):
            lines.append(
                _row(
                    f"{layer} net",
                    cell_stats(cell.triggers, layer, NET, horizon, horizon_seed),
                )
            )
            lines.append(
                _row(
                    f"{layer} gross",
                    cell_stats(cell.triggers, layer, GROSS, horizon, horizon_seed),
                )
            )
        lines.extend(
            [
                _row("unconditional", _baseline(cell, bid_bars, horizon, horizon_seed)),
                "",
            ]
        )
    return lines


def _spread_value(triggers: Sequence[Trigger], layer: str) -> str:
    spreads = [trigger.spread for trigger in triggers if _matches_layer(trigger, layer)]
    if not spreads:
        return "-"
    return f"{sum(spreads, Decimal(0)) / len(spreads):f}"


def _trigger_lines(
    cell: CellResult,
    bars: Sequence[QuoteBar],
    episodes: Sequence[Episode],
    anchor: timedelta,
) -> list[str]:
    lines = [
        f"primary N={PRIMARY.lookback} K={PRIMARY.threshold:g} layer A triggers",
        (
            "bar start (label) | JST | episode | cluster | z | r % | spread | "
            "net 1h % | net 4h %"
        ),
    ]
    for trigger in cell.triggers:
        if trigger.layer != LAYER_INTERVENTION:
            continue
        bar = bars[trigger.entry].bar
        matching = [
            episode
            for episode in episodes
            if datetime.combine(episode.action_date, time(0), tzinfo=UTC)
            <= bar.start
            < datetime.combine(episode.action_date, time(0), tzinfo=UTC) + SHOCK_WINDOW
        ]
        episode = max(matching, key=lambda item: item.action_date)
        cluster = (
            "anchor"
            if episode.action_date == episode.cluster
            else f"overlap {episode.cluster.isoformat()}"
        )
        known = broker_label_to_known(bar.start, anchor)
        lines.append(
            f"{bar.start.isoformat()} | {known.astimezone(JST).isoformat()} | "
            f"{episode.action_date} | {cluster} | {trigger.z:.3f} | "
            f"{trigger.ret * 100:.3f} | {trigger.spread:f} | "
            f"{trigger.returns[NET]['1h'] * 100:.3f} | "
            f"{trigger.returns[NET]['4h'] * 100:.3f}"
        )
    return lines


def report(
    cells: Sequence[CellResult],
    bars: Sequence[QuoteBar],
    bid_bars: Sequence[Bar],
    episodes: Sequence[Episode],
    decisions: Sequence[EventEnvelope],
    events_hash: str,
    provenance: Provenance,
    git: dict,
    anchor: timedelta,
    seed: int,
) -> str:
    """再現情報、全グリッド、主仕様の集計と判定を固定幅テキストにする。"""
    primary = next(cell for cell in cells if cell.spec == PRIMARY)
    git_line = f"git_commit={git['git_commit']} git_dirty={git['git_dirty']}"
    if "git_diff_sha256" in git:
        git_line += f" git_diff_sha256={git['git_diff_sha256']}"
    first = provenance.first_tick.isoformat() if provenance.first_tick else "-"
    last = provenance.last_tick.isoformat() if provenance.last_tick else "-"
    cluster_count = sum(episode.cluster == episode.action_date for episode in episodes)
    lines = [
        "shock trigger study",
        git_line,
        (
            f"ticks={provenance.tick_count} first={first} last={last} "
            f"dataset_hash={provenance.dataset_hash}"
        ),
        (
            f"5m bars={len(bars)} intervention episodes={len(episodes)} "
            f"({cluster_count} cluster anchors) policy decisions={len(decisions)} "
            f"events_hash={events_hash}"
        ),
        (
            f"grid: N in {{{', '.join(str(n) for n in LOOKBACKS)}}} x "
            f"K in {{{', '.join(f'{k:g}' for k in THRESHOLDS)}}}; "
            f"primary N={PRIMARY.lookback} K={PRIMARY.threshold:g}; "
            f"horizons {'/'.join(h.label for h in HORIZONS)}; seed={seed}"
        ),
        (
            "trigger: z = (r - mean) / pstdev over the last N valid 5m log "
            "returns, fires when z < -K and r < 0"
        ),
        (
            "negative = yen appreciation = short USD/JPY wins; gross = bid close -> "
            "bid close; net = bid close -> ask close (short round trip)"
        ),
        (
            "adverse/favour are gross excursions (also shown on net rows); spread = "
            "ask - bid at entry, yen"
        ),
        (
            "layers: A = within 36h of an intervention action_date 00:00 (label), "
            "A-first = cluster-first episodes, B = within 24h of a BOJ/FED decision, "
            "C = other; A wins over B"
        ),
        (
            "unconditional covers the stretch the primary triggers reach over "
            "(gross, existing helper)"
        ),
        "",
        *_grid_lines(cells, seed),
        "",
        *_summary_lines(primary, bid_bars, seed),
        (
            "spread at entry (yen): "
            f"A {_spread_value(primary.triggers, LAYER_INTERVENTION)} "
            f"A-first {_spread_value(primary.triggers, LAYER_FIRST)} "
            f"B {_spread_value(primary.triggers, LAYER_POLICY)} "
            f"C {_spread_value(primary.triggers, LAYER_OTHER)}"
        ),
        "",
        *_trigger_lines(primary, bars, episodes, anchor),
        "",
    ]
    decision, horizon = verdict(primary, seed)
    verdict_line = (
        f"verdict (primary N={PRIMARY.lookback} K={PRIMARY.threshold:g}, net): {decision}"
    )
    if horizon is not None:
        verdict_line += f" [decided at {horizon}]"
    lines.append(verdict_line)
    return "\n".join(lines)


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Shock trigger event study")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol
    if symbol != SYMBOL:
        raise SystemExit(f"this study is about {SYMBOL}, not {symbol}")
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(dsn)
    now = datetime.now(UTC)
    bars, provenance = fold_quote_bars(
        PostgresMarketTickRepository(conn).stream_between(
            symbol, EPOCH, now + BROKER_CLOCK_MARGIN
        ),
        symbol,
        sys.stderr,
    )
    if not bars:
        raise SystemExit(f"no stored quotes for {symbol}")

    events = PostgresEventRepository(conn)
    intervention_events = events.known_before(now, EVENT_TYPE)
    episodes = load_episodes_from_events(intervention_events)
    if not episodes:
        raise SystemExit(
            "no INTERVENTION_REPORTED events — run "
            "trading.data.intervention.collector first"
        )
    decisions = current_version(
        [
            event
            for event_type in EVENT_TYPES.values()
            for event in events.known_before(now, event_type)
        ]
    )
    events_hash = event_fingerprint([*intervention_events, *decisions])
    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)
    bid_bars = [quote.bar for quote in bars]
    returns = log_returns(bars)
    interventions = intervention_windows(episodes)
    policies = policy_windows(decisions, anchor)
    cells: list[CellResult] = []
    for lookback in LOOKBACKS:
        scores = z_scores(returns, lookback)
        for threshold in THRESHOLDS:
            cells.append(
                detect(
                    bars,
                    bid_bars,
                    returns,
                    scores,
                    Spec(lookback, threshold),
                    interventions,
                    policies,
                )
            )
    print(
        report(
            cells,
            bars,
            bid_bars,
            episodes,
            decisions,
            events_hash,
            provenance,
            git_state(),
            anchor,
            args.seed,
        )
    )


if __name__ == "__main__":
    main()
