"""H7 のイベント一覧・tick 出力・コスト測定・偏順位相関を扱う研究 CLI。"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import random
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trading.backtest.run import git_state
from trading.data.market.dukascopy import known_to_broker_label

if TYPE_CHECKING:
    from psycopg import Connection

Stage = Literal["explore", "confirm"]
Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
CSV_FIELDS = ["symbol", "event_time", "id", "bid", "ask"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Period(Record):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Period:
        if self.start > self.end:
            raise ValueError("期間の start は end 以前にしてください")
        return self

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class Symbols(Record):
    usdjpy: Nonempty
    eurusd: Nonempty


class Stages(Record):
    explore: Period
    confirm: Period


class ExcludedDate(Record):
    day: date
    reason: Nonempty


class Plan(Record):
    study_version: Nonempty
    symbols: Symbols
    usdjpy_pip_size: Decimal = Field(gt=0)
    series: tuple[Nonempty, Nonempty, Nonempty, Nonempty] = (
        "us_cpi_headline_sa", "us_nonfarm_payrolls_sa",
        "us_retail_sales_advance_sa", "us_real_gdp_growth_saar",
    )
    release_time: time = time(8, 30)
    release_timezone: Literal["America/New_York"] = "America/New_York"
    broker_server_ahead_of_ny_hours: int = Field(default=7, strict=True)
    stages: Stages
    excluded_dates: tuple[ExcludedDate, ...]
    pre_seconds: PositiveInt = 60
    post_minutes: PositiveInt = 15
    end_minutes: PositiveInt = 120
    quote_max_age_seconds: PositiveInt = 60
    execution_max_delay_seconds: PositiveInt = 60
    coverage_start_minutes: int = Field(default=-60, strict=True)
    coverage_end_minutes: int = Field(default=120, strict=True)
    coverage_interval_minutes: PositiveInt = 60
    placebo_offsets_days: tuple[int, int] = (-7, 7)
    window_start_minutes: int = Field(default=-60, strict=True)
    window_end_minutes: int = Field(default=135, strict=True)
    bootstrap_samples: PositiveInt = 10000
    bootstrap_seed: int = Field(strict=True)
    one_sided_level: float = Field(default=0.95, gt=0, lt=1)
    max_undefined_bootstrap_fraction: float = Field(default=0.01, ge=0, lt=1)
    explore_gate: float = Field(default=0.10, ge=-1, le=1)
    confirm_extra_cost_pips: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Plan:
        if self.symbols.usdjpy == self.symbols.eurusd or len(set(self.series)) != 4:
            raise ValueError("銘柄と系列はそれぞれ重複させないでください")
        a, b = self.stages.explore, self.stages.confirm
        if max(a.start, b.start) <= min(a.end, b.end):
            raise ValueError("探索と確認の期間を重ねないでください")
        if len({d.day for d in self.excluded_dates}) != len(self.excluded_dates):
            raise ValueError("excluded_dates の日付は一意にしてください")
        if self.release_time.tzinfo is not None:
            raise ValueError("release_time はタイムゾーンなしの現地時刻にしてください")
        if not self.placebo_offsets_days[0] < 0 < self.placebo_offsets_days[1]:
            raise ValueError("プラセボ候補は過去、未来の順にしてください")
        if not (self.coverage_start_minutes < 0 < self.coverage_end_minutes
                and (self.coverage_end_minutes - self.coverage_start_minutes)
                % self.coverage_interval_minutes == 0):
            raise ValueError("欠測を見る範囲は等間隔で発表の前後を含めてください")
        if not (self.post_minutes < self.end_minutes
                and self.window_start_minutes <= self.coverage_start_minutes
                and self.window_start_minutes * 60
                <= -self.pre_seconds - self.quote_max_age_seconds
                and self.window_end_minutes >= self.coverage_end_minutes
                and self.window_end_minutes * 60
                > self.end_minutes * 60 + self.execution_max_delay_seconds):
            raise ValueError("書き出し窓に価格・約定・欠測確認の全範囲を含めてください")
        return self


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True,
                  default=str, allow_nan=False)
        output.write("\n")


def release_at(day: date, plan: Plan) -> datetime:
    return datetime.combine(day, plan.release_time, ZoneInfo(plan.release_timezone)).astimezone(UTC)


def broker_at(instant: datetime, plan: Plan) -> datetime:
    return known_to_broker_label(
        instant, timedelta(hours=plan.broker_server_ahead_of_ny_hours),
    )


class Candidate(Record):
    day: date
    t0: AwareDatetime
    reasons: tuple[str, ...]
    excluded_date_reason: str | None = None


class Event(Record):
    day: date
    t0: AwareDatetime
    series: tuple[str, ...]
    candidates: tuple[Candidate, ...]


class Exclusion(Record):
    day: date
    t0: AwareDatetime
    reasons: tuple[str, ...]
    series: tuple[str, ...]
    excluded_date_reason: str | None = None


class Window(Record):
    day: date
    since: AwareDatetime
    until: AwareDatetime


class StageEvents(Record):
    events: tuple[Event, ...]
    excluded: tuple[Exclusion, ...]
    windows: tuple[Window, ...]


class EventStages(Record):
    explore: StageEvents
    confirm: StageEvents


class EventFile(Record):
    plan_sha256: str
    stages: EventStages
    excluded_release_times: tuple[Exclusion, ...]


def build_events(
    observations: Iterable[tuple[str, str, datetime]], plan: Plan, plan_hash: str,
) -> EventFile:
    first: dict[tuple[str, str], datetime] = {}
    for series, period, known_at in observations:
        if series in plan.series:
            key = series, period
            first[key] = min(first.get(key, known_at), known_at)
    grouped: dict[datetime, set[str]] = defaultdict(set)
    for (series, _), known_at in first.items():
        grouped[known_at.astimezone(UTC)].add(series)
    releases = {}
    wrong_time = []
    zone = ZoneInfo(plan.release_timezone)
    for t0, series in sorted(grouped.items()):
        local = t0.astimezone(zone)
        if local.time() == plan.release_time:
            releases[t0] = tuple(sorted(series))
        else:
            wrong_time.append(Exclusion(day=local.date(), t0=t0, series=tuple(sorted(series)),
                                        reasons=("release_time",)))
    all_event_days = {t0.astimezone(zone).date() for t0 in releases}
    excluded_dates = {item.day: item.reason for item in plan.excluded_dates}
    stages = {}
    for stage in ("explore", "confirm"):
        period = getattr(plan.stages, stage)
        events, excluded = [], []
        window_days: set[date] = set()

        def reasons_for(day: date, period: Period = period) -> list[str]:
            reasons = []
            if not period.contains(day):
                reasons.append("out_of_range")
            if day in excluded_dates:
                reasons.append("excluded_date")
            return reasons

        for t0, series in releases.items():
            day = t0.astimezone(zone).date()
            reasons = reasons_for(day)
            if reasons:
                excluded.append(Exclusion(day=day, t0=t0, series=series, reasons=tuple(reasons),
                                          excluded_date_reason=excluded_dates.get(day)))
                continue
            window_days.add(day)
            candidates = []
            for offset in plan.placebo_offsets_days:
                candidate_day = day + timedelta(days=offset)
                reasons = reasons_for(candidate_day)
                if candidate_day in all_event_days:
                    reasons.append("event_day")
                candidates.append(Candidate(day=candidate_day, t0=release_at(candidate_day, plan),
                                            reasons=tuple(reasons),
                                            excluded_date_reason=excluded_dates.get(candidate_day)))
                if not reasons:
                    window_days.add(candidate_day)
            events.append(Event(day=day, t0=t0, series=series, candidates=tuple(candidates)))
        windows = tuple(Window(
            day=day,
            since=release_at(day, plan) + timedelta(minutes=plan.window_start_minutes),
            until=release_at(day, plan) + timedelta(minutes=plan.window_end_minutes),
        ) for day in sorted(window_days))
        stages[stage] = StageEvents(events=tuple(events), excluded=tuple(excluded), windows=windows)
    return EventFile(plan_sha256=plan_hash, stages=EventStages(**stages),
                     excluded_release_times=tuple(wrong_time))


def events_from_db(conn: Connection, plan: Plan, plan_hash: str) -> EventFile:
    rows = conn.execute(
        "SELECT series, observation_period, min(known_at) FROM macro_observations "
        "WHERE series = ANY(%s) GROUP BY series, observation_period "
        "ORDER BY min(known_at), series, observation_period", (list(plan.series),),
    )
    return build_events(rows, plan, plan_hash)


def load_events(path: Path, plan: Plan, plan_hash: str) -> EventFile:
    events = EventFile.model_validate_json(path.read_bytes())
    if events.plan_sha256 != plan_hash:
        raise ValueError("events の Plan sha256 が一致しません")
    # 入力ファイルの窓を SQL に渡す前に、Plan とイベントから再構成して照合する。
    observations = []
    for stage in (events.stages.explore, events.stages.confirm):
        for event in (*stage.events, *stage.excluded):
            observations.extend((series, event.t0.isoformat(), event.t0) for series in event.series)
    for event in events.excluded_release_times:
        observations.extend((series, event.t0.isoformat(), event.t0) for series in event.series)
    if build_events(observations, plan, plan_hash) != events:
        raise ValueError("events の日付・候補・窓が Plan と整合しません")
    return events


def manifest_path(ticks: Path) -> Path:
    return ticks.with_name(ticks.name + ".manifest.json")


def export_ticks(
    conn: Connection, plan: Plan, events: EventFile, stage: Stage,
    output: Path, events_hash: str,
) -> dict:
    sidecar = manifest_path(output)
    if output.exists() or sidecar.exists():
        raise FileExistsError("tick または manifest の出力先が既に存在します")
    counts = {symbol: 0 for symbol in sorted((plan.symbols.usdjpy, plan.symbols.eurusd))}
    # id の天井に加え、未コミット行の遅い到着・削除も同じ snapshot で固定する。
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        ceiling = conn.execute("SELECT coalesce(max(id), 0) FROM market_ticks").fetchone()[0]
        with (
            output.open("xb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
            io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text,
        ):
            writer = csv.writer(text)
            writer.writerow(CSV_FIELDS)
            for symbol in counts:
                for window in getattr(events.stages, stage).windows:
                    with conn.cursor(name="h7_ticks") as cursor:
                        cursor.execute(
                            "SELECT symbol, event_time, id, bid::text, ask::text "
                            "FROM market_ticks WHERE symbol = %s AND event_time >= %s "
                            "AND event_time < %s AND id <= %s ORDER BY event_time, id",
                            (symbol, broker_at(window.since, plan),
                             broker_at(window.until, plan), ceiling),
                        )
                        for symbol_, event_time, id_, bid, ask in cursor:
                            writer.writerow((symbol_, event_time.astimezone(UTC).isoformat(),
                                             id_, bid, ask))
                            counts[symbol] += 1
    manifest = {"sha256": sha256(output), "max_id": ceiling, "rows_by_symbol": counts,
                "events_sha256": events_hash, "plan_sha256": events.plan_sha256, "stage": stage}
    write_json(sidecar, manifest)
    return manifest


@dataclass(frozen=True)
class Quote:
    event_time: datetime
    id: int
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass
class DayQuotes:
    before: list[Quote | None] = field(default_factory=lambda: [None, None, None])
    after: list[Quote | None] = field(default_factory=lambda: [None, None])
    coverage: list[int] = field(default_factory=list)


def targets(day: date, plan: Plan) -> tuple[datetime, datetime, datetime]:
    t0 = release_at(day, plan)
    return tuple(broker_at(t0 + offset, plan) for offset in (
        -timedelta(seconds=plan.pre_seconds), timedelta(minutes=plan.post_minutes),
        timedelta(minutes=plan.end_minutes),
    ))


def read_quotes(path: Path, plan: Plan, stage: StageEvents) -> dict[date, dict[str, DayQuotes]]:
    """CSV を一巡し、日ごとの必要な quote と区間別本数だけを保持する。"""
    windows = stage.windows
    starts = [broker_at(w.since, plan) for w in windows]
    ends = [broker_at(w.until, plan) for w in windows]
    days = [w.day for w in windows]
    target_times = [targets(day, plan) for day in days]
    coverage_starts = [broker_at(release_at(day, plan) + timedelta(
        minutes=plan.coverage_start_minutes), plan) for day in days]
    interval = timedelta(minutes=plan.coverage_interval_minutes)
    bucket_count = ((plan.coverage_end_minutes - plan.coverage_start_minutes)
                    // plan.coverage_interval_minutes)
    symbols = (plan.symbols.usdjpy, plan.symbols.eurusd)
    result = {day: {symbol: DayQuotes(coverage=[0] * bucket_count) for symbol in symbols}
              for day in days}
    previous = None
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        reader = csv.reader(source, strict=True)
        if next(reader, None) != CSV_FIELDS:
            raise ValueError("tick CSV のヘッダが一致しません")
        for number, row in enumerate(reader, 2):
            if len(row) != len(CSV_FIELDS):
                raise ValueError(f"tick CSV {number} 行目: 5 列が必要です")
            symbol, timestamp, id_text, bid_text, ask_text = row
            instant = datetime.fromisoformat(timestamp)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError(f"tick CSV {number} 行目: 時刻には timezone が必要です")
            quote = Quote(instant, int(id_text), Decimal(bid_text), Decimal(ask_text))
            if (not quote.bid.is_finite() or not quote.ask.is_finite()
                    or not 0 < quote.bid <= quote.ask or quote.id <= 0):
                raise ValueError(f"tick CSV {number} 行目: id または bid/ask が不正です")
            key = symbol, instant, quote.id
            if previous is not None and key <= previous:
                raise ValueError("tick CSV は (symbol, event_time, id) の重複なし昇順が必要です")
            previous = key
            index = bisect_right(starts, instant) - 1
            if symbol not in symbols or index < 0 or instant >= ends[index]:
                raise ValueError(f"tick CSV {number} 行目: 段階の銘柄・窓の範囲外です")
            sample = result[days[index]][symbol]
            bucket = (instant - coverage_starts[index]) // interval
            if 0 <= bucket < bucket_count:
                sample.coverage[bucket] += 1
            for i, target in enumerate(target_times[index]):
                if instant <= target:
                    sample.before[i] = quote
            for i, target in enumerate(target_times[index][1:]):
                if sample.after[i] is None and instant >= target:
                    sample.after[i] = quote
    return result


@dataclass(frozen=True)
class Observation:
    day: date
    r_uj: float
    r_eu: float
    m: float
    x: float
    fwd: float


def observe(day: date, quotes: dict[str, DayQuotes], plan: Plan) -> tuple[Observation | None, str]:
    if any(0 in sample.coverage for sample in quotes.values()):
        return None, "missing_ticks"
    times = targets(day, plan)
    max_age = timedelta(seconds=plan.quote_max_age_seconds)
    required = (quotes[plan.symbols.usdjpy].before, quotes[plan.symbols.eurusd].before[:2])
    if any(q is None or target - q.event_time > max_age
           for before in required for target, q in zip(times[:len(before)], before, strict=True)):
        return None, "stale_quote"
    uj = [q.mid for q in quotes[plan.symbols.usdjpy].before]
    eu = [q.mid for q in quotes[plan.symbols.eurusd].before[:2]]
    r_uj = float((uj[1] / uj[0]).ln())
    r_eu = float((eu[1] / eu[0]).ln())
    return Observation(day, r_uj, r_eu, (r_uj - r_eu) / 2, (r_uj + r_eu) / 2,
                       float((uj[2] / uj[1]).ln())), ""


def execution_quotes(day: date, quotes: DayQuotes, plan: Plan) -> tuple[Quote, Quote] | None:
    delay = timedelta(seconds=plan.execution_max_delay_seconds)
    if any(q is None or q.event_time - target > delay
           for q, target in zip(quotes.after, targets(day, plan)[1:], strict=True)):
        return None
    return tuple(quotes.after)


def ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[start]] == values[order[end]]:
            end += 1
        for index in order[start:end]:
            result[index] = (start + 1 + end) / 2
        start = end
    return result


def pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) < 2:
        return None
    ma, mb = fmean(a), fmean(b)
    da, db = [x - ma for x in a], [x - mb for x in b]
    aa, bb = sum(x * x for x in da), sum(x * x for x in db)
    if aa == 0 or bb == 0:
        return None
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(da, db, strict=True)) / math.sqrt(aa * bb)))


def partial_rank(a: Sequence[float], b: Sequence[float], control: Sequence[float]) -> float | None:
    ra, rb, rc = ranks(a), ranks(b), ranks(control)
    ab, ac, bc = pearson(ra, rb), pearson(ra, rc), pearson(rb, rc)
    if ab is None or ac is None or bc is None or abs(ac) == 1 or abs(bc) == 1:
        return None
    return max(-1.0, min(1.0, (ab - ac * bc) / math.sqrt((1 - ac * ac) * (1 - bc * bc))))


def statistics(observations: Sequence[Observation]) -> dict[str, float | None]:
    fwd, m, x, uj = ([getattr(o, name) for o in observations] for name in ("fwd", "m", "x", "r_uj"))
    return {"C": partial_rank(fwd, m, x), "A": partial_rank(fwd, m, uj),
            "control": pearson(ranks(fwd), ranks(uj))}


def pair_statistics(pairs: Sequence[tuple[Observation, Observation]]) -> dict[str, float | None]:
    event = statistics([a for a, _ in pairs])
    placebo = statistics([b for _, b in pairs])
    result = {**event, "C_p": placebo["C"], "A_p": placebo["A"]}
    for key in ("C", "A"):
        result["delta_" + key] = (event[key] - placebo[key]
                                 if event[key] is not None and placebo[key] is not None else None)
    return result


def percentile(
    values: Sequence[float] | Sequence[Decimal], fraction: float,
) -> float | Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    weight = position - low
    if isinstance(ordered[low], Decimal):
        weight = Decimal(str(weight))
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def bootstrap(pairs: Sequence[tuple[Observation, Observation]], plan: Plan) -> dict:
    keys = ("C", "A", "C_p", "A_p", "delta_C", "delta_A")
    values = {key: [] for key in keys}
    undefined = 0
    rng = random.Random(plan.bootstrap_seed)
    for _ in range(plan.bootstrap_samples):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        stats = pair_statistics(sample)
        if any(stats[key] is None for key in keys):
            undefined += 1
            continue
        for key in keys:
            values[key].append(stats[key])
    return {"samples": plan.bootstrap_samples, "undefined": undefined,
            "valid": plan.bootstrap_samples - undefined,
            "lower": {key: percentile(value, 1 - plan.one_sided_level)
                      for key, value in values.items()}}


def net_pips(signal: float, entry: Quote, exit_: Quote, pip_size: Decimal) -> Decimal | None:
    if signal == 0:
        return None
    return (exit_.bid - entry.ask if signal > 0 else entry.bid - exit_.ask) / pip_size


def average(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def pnl_report(
    pairs: Sequence[tuple[Observation, Observation]], quotes: dict[date, dict[str, DayQuotes]],
    plan: Plan, stage: Stage,
) -> dict:
    cost = plan.confirm_extra_cost_pips if stage == "confirm" else Decimal(0)
    raw: dict[str, list[Decimal]] = {"m": [], "r_uj": []}
    adjusted: dict[str, list[Decimal]] = {"m": [], "r_uj": []}
    zero, missing = Counter(), Counter()
    differences, adjusted_differences, rows = [], [], []
    for event, _ in pairs:
        fills = execution_quotes(event.day, quotes[event.day][plan.symbols.usdjpy], plan)
        row = {"day": event.day, "m": None, "r_uj": None}
        for rule, profits in raw.items():
            signal = getattr(event, rule)
            if signal == 0:
                zero[rule] += 1
            elif fills is None:
                missing[rule] += 1
            else:
                value = net_pips(signal, *fills, plan.usdjpy_pip_size)
                profits.append(value)
                adjusted[rule].append(value - cost)
                row[rule] = value
        if row["m"] is not None and row["r_uj"] is not None:
            differences.append(row["m"] - row["r_uj"])
            adjusted_differences.append((row["m"] - cost) - (row["r_uj"] - cost))
        rows.append(row)
    return {"extra_cost_pips": cost, "rules": {
        rule: {"trades": len(raw[rule]), "zero_signal": zero[rule],
               "missing_execution_quote": missing[rule], "mean_pips": average(raw[rule]),
               "mean_adjusted_pips": average(adjusted[rule])} for rule in raw},
        "paired_difference": {"count": len(differences), "mean_pips": average(differences),
                              "mean_adjusted_pips": average(adjusted_differences)},
        "events": rows}


def decide(stats: dict, boot: dict, mean_pnl: Decimal | None, plan: Plan, stage: Stage) -> dict:
    if stage == "explore":
        passed = all(stats[k] is not None and stats[k] >= plan.explore_gate for k in ("A", "C"))
        return {"result": "確認へ進む" if passed else "止める",
                "reason": "探索の A・C の点推定を関門と比較"}
    if boot["undefined"] / boot["samples"] > plan.max_undefined_bootstrap_fraction:
        return {"result": "判定不能", "reason": "ブートストラップの未定義率が上限を超えた"}
    keys = ("A", "C", "delta_A", "delta_C")
    if any(stats[k] is not None and stats[k] <= 0 for k in keys):
        return {"result": "棄却", "reason": "A・C・プラセボとの差のいずれかの点推定が 0 以下"}
    if (all(stats[k] is not None and boot["lower"][k] is not None and boot["lower"][k] > 0
            for k in keys) and mean_pnl is not None and mean_pnl > 0):
        return {"result": "支持", "reason": "4 つの片側下限と追加コスト控除後の平均損益が正"}
    return {"result": "判定不能", "reason": "支持・棄却の条件を満たさない、または統計量が未定義"}


def measure(
    plan: Plan, events: StageEvents, quotes: dict[date, dict[str, DayQuotes]], stage: Stage,
) -> dict:
    observations, invalid = {}, {}
    for day, samples in quotes.items():
        observation, reason = observe(day, samples, plan)
        if observation is None:
            invalid[day] = reason
        else:
            observations[day] = observation
    pairs, unpaired, selections = [], [], []
    for event in events.events:
        if event.day not in observations:
            continue
        placebo = next((observations[c.day] for c in event.candidates
                        if not c.reasons and c.day in observations), None)
        if placebo is None:
            unpaired.append(observations[event.day])
        else:
            pairs.append((observations[event.day], placebo))
            selections.append({"event_day": event.day, "placebo_day": placebo.day})
    stats = pair_statistics(pairs)
    boot = bootstrap(pairs, plan)
    pnl = pnl_report(pairs, quotes, plan, stage)
    event_days = {e.day for e in events.events}
    candidates = [c for e in events.events for c in e.candidates]
    exclusions = {
        "registration_events": dict(Counter(r for e in events.excluded for r in e.reasons)),
        "registration_candidates": dict(Counter(r for c in candidates for r in c.reasons)),
        "event_days": dict(Counter(reason for day, reason in invalid.items() if day in event_days)),
        "placebo_days": dict(Counter(reason for day, reason in invalid.items()
                                     if day not in event_days)),
        "no_pair": len(unpaired),
        "missing_execution_quote": sum(execution_quotes(
            e.day, quotes[e.day][plan.symbols.usdjpy], plan) is None for e, _ in pairs),
    }
    return {"stage": stage, "registered_events": len(events.events), "paired_events": len(pairs),
            "statistics": stats, "bootstrap": boot, "pnl": pnl, "pairs": selections,
            "unpaired_reference": {"count": len(unpaired), **statistics(unpaired)},
            "excluded_counts": exclusions,
            "invalid_days": [{"day": day, "reason": reason} for day, reason in invalid.items()],
            "decision": decide(stats, boot, pnl["rules"]["m"]["mean_adjusted_pips"], plan, stage)}


def spreads(plan: Plan, events: StageEvents, quotes: dict[date, dict[str, DayQuotes]]) -> dict:
    distributions = {}
    for i, label in enumerate(("post", "end")):
        values = []
        for event in events.events:
            quote = quotes[event.day][plan.symbols.usdjpy].after[i]
            target = targets(event.day, plan)[i + 1]
            if (quote is not None and quote.event_time - target
                    <= timedelta(seconds=plan.execution_max_delay_seconds)):
                values.append((quote.ask - quote.bid) / plan.usdjpy_pip_size)
        distributions[label] = {
            "count": len(values), "missing_execution_quote": len(events.events) - len(values),
            "mean_pips": average(values), "median_pips": percentile(values, 0.5),
            "p75_pips": percentile(values, 0.75), "p90_pips": percentile(values, 0.9),
        }
    return distributions


def render_markdown(report: dict) -> str:
    lines = ["# H7 イベント起点の通貨強弱", "", f"段階: {report['stage']}",
             f"判定: {report['decision']['result']}（{report['decision']['reason']}）", "",
             (f"登録イベント {report['registered_events']} 件、対になったイベント "
              f"{report['paired_events']} 件。未定義は null で表示する。"), ""]
    for title, key in (("統計量", "statistics"), ("ブートストラップ", "bootstrap"),
                       ("約定価格での損益（pips）", "pnl"), ("除外件数", "excluded_counts"),
                       ("対にならなかったイベント（参考）", "unpaired_reference"),
                       ("イベントとプラセボの対", "pairs"), ("除外日", "invalid_days"),
                       ("再現情報", "provenance"), ("Plan", "plan")):
        lines.extend([f"## {title}", "", "```json", json.dumps(
            report[key], ensure_ascii=False, indent=2, default=str, allow_nan=False), "```", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("events", "macro_observations からイベント・候補・実 UTC の窓を出力"),
        ("export", "登録した窓の tick と .manifest.json を出力"),
        ("spreads", "出力済み tick のスプレッドだけを測定（リターンは計算しない）"),
        ("measure", "出力済み tick の統計量と判定を report.json / report.md に出力"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--plan", type=Path, required=True, help="Plan JSON")
        if name != "events":
            command.add_argument("--events", type=Path, required=True)
            command.add_argument("--stage", choices=("explore", "confirm"), required=True)
        if name in ("spreads", "measure"):
            command.add_argument("--ticks", type=Path, required=True)
        command.add_argument("--output-dir" if name == "measure" else "--output",
                             type=Path, required=True)
    args = parser.parse_args(argv)
    plan = Plan.model_validate_json(args.plan.read_bytes())
    plan_hash = sha256(args.plan)
    if args.command == "events":
        import psycopg
        with psycopg.connect(
            os.environ["TRADING_DB_DSN"], options="-c default_transaction_read_only=on",
        ) as conn:
            events = events_from_db(conn, plan, plan_hash)
        write_json(args.output, events.model_dump(mode="json"))
        return 0
    events = load_events(args.events, plan, plan_hash)
    events_hash = sha256(args.events)
    if args.command == "export":
        import psycopg
        with psycopg.connect(os.environ["TRADING_DB_DSN"]) as conn:
            export_ticks(conn, plan, events, args.stage, args.output, events_hash)
        return 0
    ticks_hash = sha256(args.ticks)
    sidecar = manifest_path(args.ticks)
    if not sidecar.exists():
        raise ValueError("tick manifest がありません。export が書き終えた tick だけを読みます")
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    expected = {"sha256": ticks_hash, "events_sha256": events_hash,
                "plan_sha256": plan_hash, "stage": args.stage}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("tick manifest のハッシュまたは段階が一致しません")
    if args.command == "measure":
        args.output_dir.mkdir(parents=True, exist_ok=False)
    stage_events = getattr(events.stages, args.stage)
    quotes = read_quotes(args.ticks, plan, stage_events)
    provenance = {"plan_sha256": plan_hash, "events_sha256": events_hash, "ticks_sha256": ticks_hash}
    if args.command == "spreads":
        write_json(args.output, {"stage": args.stage, "provenance": provenance,
                                 "spreads": spreads(plan, stage_events, quotes)})
    else:
        report = measure(plan, stage_events, quotes, args.stage)
        report.update(plan=plan.model_dump(mode="json"), provenance={**provenance, **git_state()})
        write_json(args.output_dir / "report.json", report)
        (args.output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
