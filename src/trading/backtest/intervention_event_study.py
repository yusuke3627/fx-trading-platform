"""介入後の短期的な USD/JPY の非対称性と減衰を測る。

    python -m trading.backtest.intervention_event_study --env backtest

保存 tick を一度だけ走査して 5 分足と日足を同時に畳み、JPY_BUY の
INTERVENTION_REPORTED をショック足と報道時刻の二つでアンカーする。5 分足は
2022〜2026 年の約 35 万本で約 560 MB、tick の復号を含む実行時間は VPS で
30〜60 分を見込む。

ショック足は 36 時間の探索窓全体から事後選択し、窓が未完了または欠損を含む
エピソードは測定しない。ただし、測定する価格は選ばれた足の close 以降だけである。
「ショックが起きた条件で、その後に何が起きるか」を調べるもので、ショック
発生を予測する研究ではない。

5 分足の horizon は、休場と tick のない bucket を飛ばした「取引された足」
の本数で数える。日足は営業日で数える。クラスタは最初の日だけを非重複集計に
使い、現データでは 10 営業日の窓もクラスタアンカー同士で重ならない。
JPY_SELL は符号反転せず、対象から除外する。
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import statistics
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple, TextIO
from zoneinfo import ZoneInfo

from trading.backtest.policy_event_study import (
    BOOTSTRAP_SEED,
    BROKER_CLOCK_MARGIN,
    EPOCH,
    SYMBOL,
    Stats,
    _row,
    bootstrap_interval,
    gaps,
    irregular_steps,
    unconditional,
    window_outcome,
)
from trading.backtest.research import broker_label_to_known
from trading.data.intervention.episodes import EVENT_TYPE_PREFIX
from trading.data.market.bars import BarBuilder
from trading.data.market.dukascopy import known_to_broker_label
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick

EVENT_TYPE = f"{EVENT_TYPE_PREFIX}REPORTED"
SHOCK = "shock"
NEWS = "news"
KINDS = (SHOCK, NEWS)
TIMEFRAMES = ("5m", "1d")
SHOCK_WINDOW = timedelta(hours=36)
# broker ラベル軸の週末休場は最長 2 日で、月曜が祝日でも 3 日に収まる。
# これ以上ならアーカイブが報道時刻をまたいで欠損している。
NEWS_MAX_LAG = timedelta(days=3)
JST = ZoneInfo("Asia/Tokyo")


class Horizon(NamedTuple):
    label: str
    timeframe: str
    bars: int


HORIZONS: tuple[Horizon, ...] = (
    Horizon("15m", "5m", 3),
    Horizon("1h", "5m", 12),
    Horizon("4h", "5m", 48),
    Horizon("1d", "1d", 1),
    Horizon("2d", "1d", 2),
    Horizon("3d", "1d", 3),
    Horizon("5d", "1d", 5),
    Horizon("10d", "1d", 10),
)
INTRADAY_OFFSETS = tuple(range(3, 49, 3))
DAILY_OFFSETS = tuple(range(11))


@dataclass(frozen=True)
class Episode:
    action_date: date
    known_at: datetime
    cluster: date


@dataclass(frozen=True)
class Anchor:
    kind: str
    episode: Episode
    entry: int
    drop: float | None
    window_bars: int | None


@dataclass(frozen=True)
class Outcome:
    anchor: Anchor
    daily_entry: int | None
    returns: dict[str, float]
    adverse: dict[str, float]
    favorable: dict[str, float]
    profile_intraday: dict[int, float]
    profile_daily: dict[int, float]


def fold_bars(
    ticks: Iterator[Tick],
    symbol: str,
    timeframes: Sequence[str],
    progress: TextIO | None,
) -> dict[str, list[Bar]]:
    """保存 tick の一回の走査から複数 timeframe の閉じた足を作る。"""
    builders = {timeframe: BarBuilder(symbol, timeframe) for timeframe in timeframes}
    series: dict[str, list[Bar]] = {timeframe: [] for timeframe in timeframes}
    month: tuple[int, int] | None = None
    for count, tick in enumerate(ticks, start=1):
        for timeframe, builder in builders.items():
            if (bar := builder.on_tick(tick)) is not None:
                series[timeframe].append(bar)
        current_month = (tick.time.year, tick.time.month)
        if progress is not None and current_month != month:
            month = current_month
            counts = " / ".join(f"{len(series[timeframe]):>7,}" for timeframe in timeframes)
            print(
                f"{month[0]}-{month[1]:02d} {count:>12,} ticks  {counts} candles",
                file=progress,
                flush=True,
            )
    return series


def business_days_between(earlier: date, later: date) -> int:
    """(earlier, later] に含まれる平日数。祝日は数え分けない。"""
    return sum(
        1
        for offset in range(1, (later - earlier).days + 1)
        if (earlier + timedelta(days=offset)).weekday() < 5
    )


def cluster_anchors(dates: Sequence[date]) -> dict[date, date]:
    """直前の介入から 5 営業日以内のエピソードを連鎖させる。"""
    clusters: dict[date, date] = {}
    previous: date | None = None
    cluster: date | None = None
    for current in sorted(dates):
        if previous is None or business_days_between(previous, current) > 5:
            cluster = current
        clusters[current] = cluster
        previous = current
    return clusters


def load_episodes_from_events(events: Sequence[EventEnvelope]) -> list[Episode]:
    """保存済みの円買い介入報道を action_date 順のエピソードにする。"""
    selected = sorted(
        (
            (date.fromisoformat(event.payload["action_date"]), event.known_at)
            for event in events
            if event.event_type == EVENT_TYPE and event.payload.get("direction") == "JPY_BUY"
        ),
        key=lambda item: item[0],
    )
    clusters = cluster_anchors([action_date for action_date, _ in selected])
    return [
        Episode(action_date=action_date, known_at=known_at, cluster=clusters[action_date])
        for action_date, known_at in selected
    ]


def shock_anchors(
    bars: Sequence[Bar], episodes: Sequence[Episode]
) -> dict[date, Anchor | None]:
    """各 action_date の探索窓で close/open が最も低い 5 分足を選ぶ。

    暫定的な最小足を確定結果にしないため、窓が閉じるまでは選ばない。
    5 日以上の欠損を含む窓も、全体を観測できないため選ばない。
    """
    starts = [bar.start for bar in bars]
    found: dict[date, Anchor | None] = {}
    ordered = sorted(episodes, key=lambda episode: episode.action_date)
    for index, episode in enumerate(ordered):
        day_start = datetime.combine(episode.action_date, time(0), tzinfo=UTC)
        next_start = (
            datetime.combine(ordered[index + 1].action_date, time(0), tzinfo=UTC)
            if index + 1 < len(ordered)
            else day_start + SHOCK_WINDOW
        )
        window_end = min(day_start + SHOCK_WINDOW, next_start)
        left = bisect.bisect_left(starts, day_start)
        right = bisect.bisect_left(starts, window_end)
        if right == len(bars):
            found[episode.action_date] = None
            continue
        if left == right:
            found[episode.action_date] = None
            continue
        if gaps(bars[left : right + 1]):
            found[episode.action_date] = None
            continue
        entry = min(
            range(left, right),
            key=lambda bar_index: math.log(
                float(bars[bar_index].close) / float(bars[bar_index].open)
            ),
        )
        drop = math.log(float(bars[entry].close) / float(bars[entry].open))
        found[episode.action_date] = Anchor(
            kind=SHOCK,
            episode=episode,
            entry=entry,
            drop=drop,
            window_bars=right - left,
        )
    return found


def news_anchor(
    bars: Sequence[Bar], episode: Episode, server_ahead_of_ny: timedelta
) -> Anchor | None:
    """known_at をラベル軸へ移し、厳密に後で閉じた最初の 5 分足を採る。

    ただし報道からその close まで市場休場で説明できない時間が空くなら採らない。
    離れた値動きを介入反応として数えてしまうため。
    """
    label = known_to_broker_label(episode.known_at, server_ahead_of_ny)
    entry = bisect.bisect_right([bar.close_time for bar in bars], label)
    if entry == len(bars):
        return None
    if bars[entry].close_time - label >= NEWS_MAX_LAG:
        return None
    return Anchor(
        kind=NEWS,
        episode=episode,
        entry=entry,
        drop=None,
        window_bars=None,
    )


def daily_entry(bars_5m: Sequence[Bar], entry_5m: int, bars_1d: Sequence[Bar]) -> int | None:
    """アンカー足を含む取引日の日足を返す。"""
    entry = bisect.bisect_left(
        [bar.close_time for bar in bars_1d], bars_5m[entry_5m].close_time
    )
    return entry if entry < len(bars_1d) else None


def path(
    bars: Sequence[Bar], entry: int, offsets: Sequence[int], base: float
) -> dict[int, float]:
    """アンカー close から各 offset の close までの累積 log return。"""
    values: dict[int, float] = {}
    for offset in offsets:
        end = entry + offset
        if end < len(bars) and not gaps(bars[entry : end + 1]):
            values[offset] = math.log(float(bars[end].close) / base)
    return values


def build_outcome(anchor: Anchor, series: dict[str, list[Bar]]) -> Outcome:
    """一つのアンカーについて horizon と減衰経路を組み立てる。"""
    entry_1d = daily_entry(series["5m"], anchor.entry, series["1d"])
    measured: dict[str, tuple[float, float, float]] = {}
    for horizon in HORIZONS:
        entry = anchor.entry if horizon.timeframe == "5m" else entry_1d
        if entry is not None:
            result = window_outcome(series[horizon.timeframe], entry, horizon.bars)
            if result is not None:
                measured[horizon.label] = result

    base = float(series["5m"][anchor.entry].close)
    return Outcome(
        anchor=anchor,
        daily_entry=entry_1d,
        returns={label: result[0] for label, result in measured.items()},
        adverse={label: result[1] for label, result in measured.items()},
        favorable={label: result[2] for label, result in measured.items()},
        profile_intraday=path(series["5m"], anchor.entry, INTRADAY_OFFSETS, base),
        profile_daily=(
            path(series["1d"], entry_1d, DAILY_OFFSETS, base)
            if entry_1d is not None
            else {}
        ),
    )


def build_outcomes(
    episodes: Sequence[Episode],
    series: dict[str, list[Bar]],
    server_ahead_of_ny: timedelta,
) -> dict[str, list[Outcome]]:
    """二種類のアンカーを検出し、計測可能なエピソードを組み立てる。"""
    shocks = shock_anchors(series["5m"], episodes)
    shock_outcomes = [
        build_outcome(anchor, series) for anchor in shocks.values() if anchor is not None
    ]
    covered_episodes = [anchor.episode for anchor in shocks.values() if anchor is not None]
    news = [
        news_anchor(series["5m"], episode, server_ahead_of_ny)
        for episode in covered_episodes
    ]
    news_outcomes = [
        build_outcome(anchor, series) for anchor in news if anchor is not None
    ]
    return {SHOCK: shock_outcomes, NEWS: news_outcomes}


def stats(outcomes: Sequence[Outcome], label: str, seed: int) -> Stats:
    """一つの horizon のリターンと excursion を集計する。"""
    measured = [outcome for outcome in outcomes if label in outcome.returns]
    if not measured:
        return Stats(0, *([float("nan")] * 7))
    returns = [outcome.returns[label] for outcome in measured]
    low, high = bootstrap_interval(returns, seed)
    return Stats(
        count=len(measured),
        mean=statistics.fmean(returns),
        median=statistics.median(returns),
        hit_rate=sum(value < 0 for value in returns) / len(returns),
        adverse=statistics.fmean(outcome.adverse[label] for outcome in measured),
        favorable=statistics.fmean(outcome.favorable[label] for outcome in measured),
        low=low,
        high=high,
    )


def cluster_only(outcomes: Sequence[Outcome]) -> list[Outcome]:
    """非重複集計に使う、クラスタの最初の日のエピソードだけ。"""
    return [
        outcome
        for outcome in outcomes
        if outcome.anchor.episode.cluster == outcome.anchor.episode.action_date
    ]


def baseline_span(
    outcomes: Sequence[Outcome], bars: Sequence[Bar], horizon: Horizon
) -> Sequence[Bar]:
    """クラスタアンカーが届く範囲だけを無条件分布へ渡す。"""
    entries = [
        outcome.anchor.entry if horizon.timeframe == "5m" else outcome.daily_entry
        for outcome in cluster_only(outcomes)
    ]
    measured_entries = [entry for entry in entries if entry is not None]
    if not measured_entries:
        return ()
    return bars[min(measured_entries) : max(measured_entries) + horizon.bars + 1]


def _cluster_label(episode: Episode) -> str:
    if episode.action_date == episode.cluster:
        return "anchor"
    return f"overlap {episode.cluster.isoformat()}"


def _outcomes_by_date(outcomes: Sequence[Outcome]) -> dict[date, Outcome]:
    return {outcome.anchor.episode.action_date: outcome for outcome in outcomes}


def _shock_anchor_lines(
    outcomes: Sequence[Outcome], episodes: Sequence[Episode], bars: Sequence[Bar], anchor: timedelta
) -> list[str]:
    by_date = _outcomes_by_date(outcomes)
    lines = [
        "shock anchors",
        (
            "action_date | cluster | bars | bar start (label) | UTC | JST | "
            "open->close | drop % (yen)"
        ),
    ]
    for episode in episodes:
        outcome = by_date.get(episode.action_date)
        if outcome is None:
            lines.append(
                f"{episode.action_date} | {_cluster_label(episode)} | no shock anchor"
            )
            continue
        shock = outcome.anchor
        bar = bars[shock.entry]
        known = broker_label_to_known(bar.start, anchor)
        yen = bar.close - bar.open
        lines.append(
            f"{episode.action_date} | {_cluster_label(episode)} | {shock.window_bars} | "
            f"{bar.start.isoformat()} | {known.isoformat()} | "
            f"{known.astimezone(JST).isoformat()} | {bar.open}->{bar.close} | "
            f"{shock.drop * 100:.3f}% ({yen:+f})"
        )
    return lines


def _news_anchor_lines(
    outcomes: Sequence[Outcome],
    episodes: Sequence[Episode],
    bars: Sequence[Bar],
    anchor: timedelta,
    covered_dates: set[date],
) -> list[str]:
    by_date = _outcomes_by_date(outcomes)
    lines = [
        "news anchors",
        "action_date | cluster | known_at (UTC) | label | entry bar start | entry close",
    ]
    for episode in episodes:
        label = known_to_broker_label(episode.known_at, anchor)
        if episode.action_date not in covered_dates:
            lines.append(
                f"{episode.action_date} | {_cluster_label(episode)} | no shock anchor"
            )
            continue
        outcome = by_date.get(episode.action_date)
        if outcome is None:
            lines.append(
                f"{episode.action_date} | {_cluster_label(episode)} | "
                f"{episode.known_at.astimezone(UTC).isoformat()} | {label.isoformat()} | "
                "no bar close to known_at"
            )
            continue
        bar = bars[outcome.anchor.entry]
        lines.append(
            f"{episode.action_date} | {_cluster_label(episode)} | "
            f"{episode.known_at.astimezone(UTC).isoformat()} | {label.isoformat()} | "
            f"{bar.start.isoformat()} | {bar.close}"
        )
    return lines


def _episode_lines(
    kind: str, outcomes: Sequence[Outcome], episodes: Sequence[Episode]
) -> list[str]:
    by_date = _outcomes_by_date(outcomes)
    headings = " ".join(f"{horizon.label:>8}" for horizon in HORIZONS)
    lines = [f"{kind} episode details", f"action_date cluster            metric {headings}"]
    for episode in episodes:
        outcome = by_date.get(episode.action_date)
        for metric, attribute in (
            ("ret", "returns"),
            ("fav", "favorable"),
            ("adv", "adverse"),
        ):
            values = getattr(outcome, attribute) if outcome is not None else {}
            cells = " ".join(
                f"{values[horizon.label] * 100:>8.2f}"
                if horizon.label in values
                else f"{'-':>8}"
                for horizon in HORIZONS
            )
            # "overlap YYYY-MM-DD" は 18 文字。日付 10 文字 + 空白 + 18 = 29 桁を揃える。
            identity = (
                f"{episode.action_date} {_cluster_label(episode):<18}"
                if metric == "ret"
                else f"{'':29}"
            )
            lines.append(f"{identity} {metric:>6} {cells}")
    return lines


def _summary_lines(
    kind: str, outcomes: Sequence[Outcome], series: dict[str, list[Bar]]
) -> list[str]:
    lines = [f"{kind} summary"]
    cluster = cluster_only(outcomes)
    for index, horizon in enumerate(HORIZONS):
        seed = BOOTSTRAP_SEED + index
        description = (
            f"{horizon.bars} x 5m" if horizon.timeframe == "5m" else "trading days"
        )
        lines.extend(
            [
                f"horizon {horizon.label} ({description})",
                (
                    f"  {'':<22} {'n':>6} {'mean':>8} {'median':>8} {'hit':>6} "
                    f"{'adverse':>7} {'favour':>7}  CI90"
                ),
                _row("all episodes", stats(outcomes, horizon.label, seed)),
                _row("cluster anchors", stats(cluster, horizon.label, seed)),
                _row(
                    "unconditional",
                    unconditional(
                        baseline_span(outcomes, series[horizon.timeframe], horizon),
                        horizon.bars,
                        seed,
                    ),
                ),
                "",
            ]
        )
    return lines


def _profile_values(outcomes: Sequence[Outcome], attribute: str, offset: int) -> list[float]:
    return [
        profile[offset]
        for outcome in outcomes
        if offset in (profile := getattr(outcome, attribute))
    ]


def _profile_cell(values: Sequence[float]) -> str:
    if not values:
        return f"{0:>4} {'-':>8} {'-':>8}"
    return (
        f"{len(values):>4} {statistics.fmean(values) * 100:>8.2f} "
        f"{statistics.median(values) * 100:>8.2f}"
    )


def _offset_label(offset: int, daily: bool) -> str:
    if daily:
        return "entry-day close" if offset == 0 else f"+{offset}d"
    hours, minutes = divmod(offset * 5, 60)
    if hours == 0:
        return f"+{minutes}m"
    return f"+{hours}h" if minutes == 0 else f"+{hours}h{minutes:02d}m"


def _profile_lines(kind: str, outcomes: Sequence[Outcome]) -> list[str]:
    cluster = cluster_only(outcomes)
    columns = f"{'n':>4} {'mean':>8} {'median':>8}"
    lines = [
        f"{kind} decay profile (cumulative from the anchor close)",
        f"{'offset':<18} {'all episodes':^22}   {'cluster anchors':^22}",
        f"{'':<18} {columns}   {columns}",
    ]
    for attribute, offsets, daily in (
        ("profile_intraday", INTRADAY_OFFSETS, False),
        ("profile_daily", DAILY_OFFSETS, True),
    ):
        for offset in offsets:
            all_values = _profile_values(outcomes, attribute, offset)
            cluster_values = _profile_values(cluster, attribute, offset)
            lines.append(
                f"{_offset_label(offset, daily):<18} "
                f"{_profile_cell(all_values)}   {_profile_cell(cluster_values)}"
            )
    return lines


def report(
    outcomes_by_kind: dict[str, list[Outcome]],
    series: dict[str, list[Bar]],
    episodes: Sequence[Episode],
    anchor: timedelta,
) -> str:
    """アンカー、個票、集計、減衰を PowerShell 向け固定幅テキストにする。"""
    shock_count = len(outcomes_by_kind[SHOCK])
    lines = [
        (
            f"{len(episodes)} intervention episodes ({shock_count} with quotes), "
            f"{len(series['5m'])} 5m bars, {len(series['1d'])} daily bars"
        ),
        "negative = yen appreciation = short USD/JPY wins",
        "intraday = traded 5m bars; daily = trading-day bars",
        (
            "cluster = consecutive interventions within 5 business days; "
            "overlap excluded only from cluster aggregates"
        ),
        "hit = share ending lower; CI = 90% bootstrap of the mean",
        "unconditional covers the stretch the cluster anchors reach over",
        "",
        "daily series gaps",
    ]
    holes = gaps(series["1d"])
    irregular = irregular_steps(series["1d"])
    if not irregular:
        lines.append("none")
    for before, after in irregular:
        skipped = (after.start - before.start).days - 1
        kind = (
            "missing data, windows crossing it are not measured"
            if (before, after) in holes
            else "market closed, windows spanning it are still trading days"
        )
        lines.append(f"{before.start.date()} -> {after.start.date()} ({skipped}d): {kind}")
    lines.extend(
        [""] + _shock_anchor_lines(outcomes_by_kind[SHOCK], episodes, series["5m"], anchor)
    )
    covered_dates = set(_outcomes_by_date(outcomes_by_kind[SHOCK]))
    lines.extend(
        [""]
        + _news_anchor_lines(
            outcomes_by_kind[NEWS], episodes, series["5m"], anchor, covered_dates
        )
    )
    for kind in KINDS:
        outcomes = outcomes_by_kind[kind]
        lines.extend([""] + _episode_lines(kind, outcomes, episodes))
        lines.extend([""] + _summary_lines(kind, outcomes, series))
        lines.extend(_profile_lines(kind, outcomes) + [""])
    return "\n".join(lines).rstrip()


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Intervention event study")
    parser.add_argument("--env", default="backtest")
    args = parser.parse_args()

    config = load_config(args.env)
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
    series = fold_bars(
        PostgresMarketTickRepository(conn).stream_between(
            SYMBOL, EPOCH, now + BROKER_CLOCK_MARGIN
        ),
        SYMBOL,
        TIMEFRAMES,
        sys.stderr,
    )
    if not series["5m"]:
        raise SystemExit(f"no stored quotes for {SYMBOL}")

    episodes = load_episodes_from_events(
        PostgresEventRepository(conn).known_before(now, EVENT_TYPE)
    )
    if not episodes:
        raise SystemExit(
            "no INTERVENTION_REPORTED events — run "
            "trading.data.intervention.collector first"
        )
    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)
    print(report(build_outcomes(episodes, series, anchor), series, episodes, anchor))


if __name__ == "__main__":
    main()
