"""主な意見の公表に対する市場反応を測る、DB・LLM 非依存の研究ハーネス。

python -m trading.data.policy.opinions_reaction_study --run-dir tmp/opinions-reaction/run-1
事前登録: docs/research/2026-09-20-opinions-market-reaction-event-study.md
"""
from __future__ import annotations

import argparse
import hashlib
import lzma
import math
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from http.client import HTTPException
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading.data.market.dukascopy import (
    OUTAGE_MAX_PAUSES,
    OUTAGE_PAUSE_SECONDS,
    OUTAGE_THRESHOLD,
    REQUEST_INTERVAL_SECONDS,
    RETRY_WAITS,
    decode_bi5,
    default_fetch,
    hour_url,
)
from trading.data.policy.extraction_study import Document, fetch_document, write_json
from trading.data.policy.meetings import DEFAULT_MEETINGS_PATH, PolicyMeeting, load_meetings
from trading.data.policy.opinions import (
    JST,
    extract_pdf_text,
    japanese_statement_pdf_url,
    publication_from_statement,
)
from trading.data.policy.opinions_signal_study import (
    KEYWORD_VERSION,
    TARGET_DATES,
    keyword_balance,
    opinions_url,
    spearman,
    split_policy_opinions,
)
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.market import Tick

SYMBOL = "USDJPY"
STUDY_VERSION = "opinions_market_reaction_v1"
PRE_WINDOW = timedelta(minutes=10)
POST_PRIMARY = timedelta(minutes=10)
POST_SECONDARY = timedelta(minutes=60)
MINIMUM_TICKS = 30
REACTS_THRESHOLD = 0.70
CONTROL_RADIUS_DAYS = 10
MEETING_EXCLUSION_DAYS = 1
MINIMUM_CONTROLS_PER_WEEKDAY = 30


@dataclass(frozen=True)
class Case:
    decision_date: date
    t0: datetime | None
    balance: Fraction | None
    preparation_error: str | None = None
    keyword_error: str | None = None


class Window(BaseModel):
    model_config = ConfigDict(frozen=True)

    return_bp: float
    tick_count: int
    median_spread_bp: Decimal | None
    start: datetime
    end: datetime
    start_tick_at: datetime
    end_tick_at: datetime
    start_price: Decimal
    end_price: Decimal


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True)

    t0: datetime
    pre10: Window | None = None
    post10: Window | None = None
    post60: Window | None = None
    error: str | None = None
    window_errors: dict[str, str] = Field(default_factory=dict)


def select_meetings(meetings: Sequence[PolicyMeeting]) -> list[PolicyMeeting]:
    selected = [m for m in meetings if m.bank == "BOJ" and m.decision_date in TARGET_DATES]
    if (len(selected) != len(TARGET_DATES)
            or {m.decision_date for m in selected} != set(TARGET_DATES)):
        raise ValueError("事前登録した20会合が重複なく全件必要です")
    return sorted(selected, key=lambda m: m.decision_date)


def prepare_cases(
    meetings: Sequence[PolicyMeeting], cache_dir: Path,
    fetch: Callable[[str, Path], Document] = fetch_document,
    extract_text: Callable[[bytes], str] = extract_pdf_text,
) -> list[Case]:
    cases = []
    for index, meeting in enumerate(meetings, 1):
        t0 = balance = preparation_error = keyword_error = None
        try:
            document = fetch(japanese_statement_pdf_url(meeting.decision_date), cache_dir)
            if document.kind != "pdf":
                raise ValueError("声明の原文がPDFではありません")
            t0 = publication_from_statement(
                extract_text(document.raw), meeting.decision_date,
            ).astimezone(UTC)
        except (OSError, ValueError, RuntimeError, HTTPException) as exc:
            preparation_error = f"{type(exc).__name__}: {exc}"
        try:
            document = fetch(opinions_url(meeting.decision_date), cache_dir)
            if document.kind != "pdf":
                raise ValueError("主な意見の原文がPDFではありません")
            balance = keyword_balance(split_policy_opinions(extract_text(document.raw)))
        except (OSError, ValueError, RuntimeError, HTTPException) as exc:
            keyword_error = f"{type(exc).__name__}: {exc}"
        cases.append(Case(meeting.decision_date, t0, balance, preparation_error, keyword_error))
        print(f"原文準備 {index}/{len(meetings)}: {meeting.decision_date}",
              file=sys.stderr, flush=True)
    return cases


def control_days(
    publications: Sequence[datetime], meetings: Sequence[PolicyMeeting],
) -> tuple[list[datetime], dict[str, list[str]]]:
    utc = [t.astimezone(UTC) for t in publications]
    if len({t.time() for t in utc}) != 1:
        raise ValueError("公表のUTC時計時刻が全件一致していません")
    publication_days = {t.date() for t in utc}
    publication_jst_days = {t.astimezone(JST).date() for t in utc}
    nearby_days = {
        day + timedelta(days=offset)
        for day in publication_days
        for offset in range(-CONTROL_RADIUS_DAYS, CONTROL_RADIUS_DAYS + 1)
    }
    policy_days = {
        m.decision_date + timedelta(days=offset)
        for m in meetings for offset in range(-MEETING_EXCLUSION_DAYS, MEETING_EXCLUSION_DAYS + 1)
    }
    candidates, excluded = [], {}
    for day in sorted(nearby_days):
        t0 = datetime.combine(day, utc[0].time(), tzinfo=UTC)
        jst_day = t0.astimezone(JST).date()
        reasons = []
        if jst_day.weekday() >= 5:
            reasons.append("weekend")
        if jst_day in publication_jst_days:
            reasons.append("publication_day")
        if jst_day in policy_days:
            reasons.append("policy_decision_window")
        if reasons:
            excluded[day.isoformat()] = reasons
        else:
            candidates.append(t0)
    return candidates, excluded


def hours_for(t0: datetime) -> list[datetime]:
    hour = (t0 - PRE_WINDOW).replace(minute=0, second=0, microsecond=0)
    last = (t0 + POST_SECONDARY).replace(minute=0, second=0, microsecond=0)
    hours = []
    while hour <= last:
        hours.append(hour)
        hour += timedelta(hours=1)
    return hours


def cache_path(cache_dir: Path, hour: datetime) -> Path:
    return cache_dir / SYMBOL / hour.strftime("%Y/%m/%d/%Hh_ticks.bi5")


def fetch_hours(
    hours: Sequence[datetime], cache_dir: Path,
    fetch: Callable[[str], bytes | None] = default_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[datetime, str]:
    errors: dict[datetime, str] = {}
    failures = pauses = index = 0
    while index < len(hours):
        hour = hours[index]
        path = cache_path(cache_dir, hour)
        if path.exists():
            status = "cache"
            failures = pauses = 0
        else:
            error = None
            for attempt in range(len(RETRY_WAITS) + 1):
                try:
                    payload = fetch(hour_url(SYMBOL, hour)) or b""
                    decode_bi5(payload, SYMBOL, hour, hour)
                    error = None
                    break
                except (OSError, HTTPException, ValueError, lzma.LZMAError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt < len(RETRY_WAITS):
                        sleep(RETRY_WAITS[attempt])
            if error is not None:
                failures += 1
                print(f"tick取得失敗 {index + 1}/{len(hours)}: {hour.isoformat()} {error}",
                      file=sys.stderr, flush=True)
                if failures >= OUTAGE_THRESHOLD:
                    pauses += 1
                    if pauses > OUTAGE_MAX_PAUSES:
                        raise RuntimeError("Dukascopyの障害が継続しています。回復後に再実行してください")
                    print(f"Dukascopy待避: {OUTAGE_PAUSE_SECONDS:g}秒 ({pauses}/{OUTAGE_MAX_PAUSES})",
                          file=sys.stderr, flush=True)
                    sleep(OUTAGE_PAUSE_SECONDS)
                    continue
                errors[hour] = error
                index += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
            failures = pauses = 0
            sleep(REQUEST_INTERVAL_SECONDS)
            status = "downloaded" if payload else "empty"
        index += 1
        print(f"tick取得 {index}/{len(hours)}: {hour.isoformat()} {status}",
              file=sys.stderr, flush=True)
    return errors


def mid_at(ticks: Sequence[Tick], at: datetime) -> Decimal | None:
    index = bisect_right(ticks, at, key=lambda tick: tick.time) - 1
    return ticks[index].mid if index >= 0 else None


def window_stats(ticks: Sequence[Tick], start: datetime, end: datetime) -> Window | None:
    p0, p1 = mid_at(ticks, start), mid_at(ticks, end)
    if p0 is None or p1 is None:
        return None
    first = bisect_left(ticks, start, key=lambda tick: tick.time)
    last = bisect_right(ticks, end, key=lambda tick: tick.time)
    spreads = [tick.spread / tick.mid * 10_000 for tick in ticks[first:last]]
    return Window(
        return_bp=math.log(float(p1) / float(p0)) * 10_000,
        tick_count=last - first,
        median_spread_bp=statistics.median(spreads) if spreads else None,
        start=start, end=end,
        start_tick_at=ticks[bisect_right(ticks, start, key=lambda tick: tick.time) - 1].time,
        end_tick_at=ticks[last - 1].time,
        start_price=p0, end_price=p1,
    )


def observe(t0: datetime, cache_dir: Path, errors: Mapping[datetime, str]) -> Observation:
    ticks = []
    failed_hours = {}
    for hour in hours_for(t0):
        if hour in errors:
            failed_hours[hour] = errors[hour]
            continue
        try:
            # DBを経由しない研究なのでADR-005のbrokerラベル軸へ変換せず、実UTCを使う。
            ticks.extend(decode_bi5(cache_path(cache_dir, hour).read_bytes(), SYMBOL, hour, hour))
        except (OSError, ValueError, lzma.LZMAError) as exc:
            failed_hours[hour] = f"{type(exc).__name__}: {exc}"
    ticks.sort(key=lambda tick: tick.time)
    windows, window_errors = {}, {}
    for name, start, end in (
        ("pre10", t0 - PRE_WINDOW, t0),
        ("post10", t0, t0 + POST_PRIMARY),
        ("post60", t0, t0 + POST_SECONDARY),
    ):
        window = window_stats(ticks, start, end)
        first_tick = window.start_tick_at if window else start
        failures = [
            f"{hour.isoformat()}: {error}" for hour, error in failed_hours.items()
            if hour <= end and hour + timedelta(hours=1) > first_tick
        ]
        if failures:
            window_errors[name] = "; ".join(failures)
        windows[name] = None if failures else window
    return Observation(
        t0=t0, **windows, error=window_errors.get("post10"), window_errors=window_errors,
    )


def missing_reason(observation: Observation) -> str | None:
    if observation.error:
        return "tick_fetch_error"
    if observation.post10 is None:
        return "missing_endpoint_price"
    return None


def control_exclusion(observation: Observation) -> str | None:
    reason = missing_reason(observation)
    if reason:
        return reason
    if observation.post10.tick_count < MINIMUM_TICKS:
        return "insufficient_post10_ticks"
    return None


def percentile(value: float, sample: Sequence[float]) -> float:
    return (sum(s < value for s in sample) + sum(s == value for s in sample) / 2) / len(sample)


def judge(percentiles: Sequence[float | None]) -> dict[str, Any]:
    lower_median = statistics.median(p if p is not None else 0.0 for p in percentiles)
    missing = sum(p is None for p in percentiles)
    if lower_median >= REACTS_THRESHOLD:
        verdict = "reacts"
    elif missing:
        verdict = "incomplete"
    else:
        verdict = "not_established"
    return {"verdict": verdict, "median_percentile_missing_zero": lower_median,
            "missing": missing, "n": len(percentiles)}


def distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median_bp": None, "p90_bp": None, "p95_bp": None}
    ordered = sorted(values)

    def quantile(q: float) -> float:
        position = (len(ordered) - 1) * q
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {"n": len(values), "median_bp": statistics.median(values),
            "p90_bp": quantile(0.90), "p95_bp": quantile(0.95)}


def compare_returns(
    values: Sequence[float], control: Mapping[str, Any], above_p90: int,
) -> dict[str, Any]:
    event_dist = distribution(values)
    denominator = control["median_bp"]
    return {
        "event_abs_post10": event_dist,
        "median_ratio": event_dist["median_bp"] / denominator
        if values and denominator else None,
        "above_control_p90": above_p90,
        "null_expected_count": len(values) * 0.10,
    }


def descriptive_stats(observations: Sequence[Observation]) -> dict[str, Any]:
    return {
        "abs_pre10": distribution([
            abs(o.pre10.return_bp) for o in observations if o.pre10 is not None
        ]),
        "abs_post60": distribution([
            abs(o.post60.return_bp) for o in observations if o.post60 is not None
        ]),
        "post10_spread_bp": distribution([
            float(o.post10.median_spread_bp) for o in observations
            if o.post10 is not None and o.post10.median_spread_bp is not None
        ]),
    }


def summarize(
    cases: Sequence[Case], events: Mapping[date, Observation],
    controls: Sequence[Observation], excluded: Mapping[str, list[str]],
) -> dict[str, Any]:
    used_weekdays = {c.t0.astimezone(JST).weekday() for c in cases if c.t0 is not None}
    weekdays = sorted(used_weekdays | {o.t0.astimezone(JST).weekday() for o in controls})
    controls_by_weekday = {
        day: [o for o in controls if o.t0.astimezone(JST).weekday() == day]
        for day in weekdays
    }
    for day in sorted(used_weekdays):
        count = len(controls_by_weekday[day])
        if count < MINIMUM_CONTROLS_PER_WEEKDAY:
            raise ValueError(
                f"JST {'月火水木金土日'[day]}曜の対照が"
                f"{MINIMUM_CONTROLS_PER_WEEKDAY}日未満です: {count}日"
            )
    samples = {
        day: [abs(o.post10.return_bp) for o in observations]
        for day, observations in controls_by_weekday.items()
    }
    control_distributions = {day: distribution(sample) for day, sample in samples.items()}
    # post60 は副次窓なので判定に使わない。閾値も登録していない。事前登録が
    # 「主判定が not_established でも post60 に反応があれば、反応が無いのではなく
    # 時刻の取り方が悪い証拠になる」と定めた分を、主判定と同じ手続きで並べる。
    samples60 = {
        day: [abs(o.post60.return_bp) for o in observations if o.post60 is not None]
        for day, observations in controls_by_weekday.items()
    }
    returns_by_weekday: dict[int, list[float]] = {day: [] for day in weekdays}
    above_by_weekday: Counter[int] = Counter()
    ranks60: list[float] = []
    rows, ranks = [], []
    delta_x, delta_y, level_x, level_y = [], [], [], []
    for index, case in enumerate(cases):
        observation = events.get(case.decision_date)
        reason = case.preparation_error or (
            missing_reason(observation) if observation is not None else "preparation_failed"
        )
        day = case.t0.astimezone(JST).weekday() if case.t0 is not None else None
        value = observation.post10.return_bp if reason is None else None
        rank = percentile(abs(value), samples[day]) if value is not None else None
        ranks.append(rank)
        if value is not None:
            returns_by_weekday[day].append(abs(value))
            above_by_weekday[day] += abs(value) > control_distributions[day]["p90_bp"]
            if case.balance is not None:
                level_x.append(case.balance)
                level_y.append(value)
                if index > 0 and cases[index - 1].balance is not None:
                    delta_x.append(case.balance - cases[index - 1].balance)
                    delta_y.append(value)
        rank60 = (
            percentile(abs(observation.post60.return_bp), samples60[day])
            if day is not None and observation is not None and observation.post60 is not None
            and samples60[day] else None
        )
        if rank60 is not None:
            ranks60.append(rank60)
        rows.append({
            "decision_date": case.decision_date.isoformat(),
            "t0": case.t0.isoformat() if case.t0 else None,
            "jst_weekday": day,
            "keyword_balance": str(case.balance) if case.balance is not None else None,
            "keyword_error": case.keyword_error, "missing_reason": reason,
            "percentile_abs_post10": rank,
            "percentile_abs_post60": rank60,
            "observation": observation.model_dump(mode="json") if observation else None,
        })
    control_dist = distribution([value for sample in samples.values() for value in sample])
    returns = [value for values in returns_by_weekday.values() for value in values]
    event_observations = [events[c.decision_date] for c in cases if c.decision_date in events]
    return {
        "study_version": STUDY_VERSION, "primary": judge(ranks), "events": rows,
        "secondary_post60": {
            "verdict": None,
            "no_threshold_was_preregistered": True,
            "n": len(ranks60),
            "median_percentile": statistics.median(ranks60) if ranks60 else None,
            "above_control_p90": sum(rank > 0.90 for rank in ranks60),
        },
        "controls": {
            "abs_post10": control_dist,
            "by_jst_weekday": {
                str(day): {
                    "abs_post10": control_distributions[day],
                    "descriptive": descriptive_stats(observations),
                } for day, observations in controls_by_weekday.items()
            },
            "observations": [o.model_dump(mode="json") for o in controls],
            "excluded_days": len(excluded),
            "exclusion_counts": dict(Counter(r for reasons in excluded.values() for r in reasons)),
            "excluded": dict(excluded),
        },
        "comparison": {
            **compare_returns(returns, control_dist, sum(above_by_weekday.values())),
            "p90_reference": "same_jst_weekday",
            "by_jst_weekday": {
                str(day): compare_returns(
                    returns_by_weekday[day], control_distributions[day], above_by_weekday[day],
                ) for day in weekdays
            },
        },
        "descriptive": {
            "events": descriptive_stats(event_observations),
            "controls": descriptive_stats(controls),
            "events_by_jst_weekday": {
                str(day): descriptive_stats([
                    o for o in event_observations if o.t0.astimezone(JST).weekday() == day
                ]) for day in weekdays
            },
        },
        "direction": {
            "interpret_only_if_reacts": True,
            "keyword_change": {"n": len(delta_x), "rho": spearman(delta_x, delta_y)},
            "keyword_level": {"n": len(level_x), "rho": spearman(level_x, level_y)},
        },
        "quantile_method": "linear interpolation at (n - 1) * q",
    }


def render_report(report: Mapping[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{float(value):.4f}"

    def weekday_label(day: str | int | None) -> str:
        return "—" if day is None else "月火水木金土日"[int(day)]

    primary, control, comparison = report["primary"], report["controls"], report["comparison"]
    lines = [
        "# 主な意見の公表に対する USD/JPY の反応（段階3b）", "",
        (f"主判定: **{primary['verdict']}**（欠測を0としたパーセンタイル中央値 = "
         f"{fmt(primary['median_percentile_missing_zero'])}, 欠測 = "
         f"{primary['missing']}/{primary['n']}, 対照 = {control['abs_post10']['n']}日）"), "",
        "## 事象ごと", "",
        ("| 会合日 | 公表(UTC) | JST曜日 | post10 bp | abs(post10) の同曜日対照パーセンタイル "
         "| post60 bp | abs(post60) の同曜日対照パーセンタイル | abs(pre10) bp | tick数 "
         "| スプレッド中央値 bp | 備考 |"),
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["events"]:
        observation = row["observation"] or {}
        post10, post60, pre10 = (
            observation.get(key) or {} for key in ("post10", "post60", "pre10")
        )
        note = "; ".join(str(s) for s in (
            row["missing_reason"], observation.get("error"), row["keyword_error"],
            *[f"{name}: {error}" for name, error in observation.get("window_errors", {}).items()
              if name != "post10"],
        ) if s).replace("|", "\\|").replace("\n", " ")
        pre_value = pre10.get("return_bp")
        lines.append(
            f"| {row['decision_date']} | {row['t0'] or '—'} | {weekday_label(row['jst_weekday'])} "
            f"| {fmt(post10.get('return_bp'))} | {fmt(row['percentile_abs_post10'])} "
            f"| {fmt(post60.get('return_bp'))} | {fmt(row['percentile_abs_post60'])} "
            f"| {fmt(abs(pre_value) if pre_value is not None else None)} "
            f"| {post10.get('tick_count', '—')} | {fmt(post10.get('median_spread_bp'))} | {note} |"
        )
    lines += [
        "", "## 対照群", "",
        "| JST曜日 | n | abs(post10) 中央値 bp | p90 bp | p95 bp |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, dist in [
        *[(weekday_label(day), values["abs_post10"])
          for day, values in control["by_jst_weekday"].items()],
        ("全体", control["abs_post10"]),
    ]:
        lines.append(
            f"| {label} | {dist['n']} | {fmt(dist['median_bp'])} "
            f"| {fmt(dist['p90_bp'])} | {fmt(dist['p95_bp'])} |"
        )
    lines += [
        "", f"除外: {control['excluded_days']}日。理由は重複を含む。", "",
        *[f"- {reason}: {count}日" for reason, count in control["exclusion_counts"].items()],
        "", "## 事象 vs 対照", "",
        ("| JST曜日 | 測定できた事象数 | 事象 abs(post10) 中央値 bp | 中央値の比 "
         "| 同曜日対照p90超過件数 / 測定数 | 帰無の期待件数 / 測定数 |"),
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, values in [
        *[(weekday_label(day), values) for day, values in comparison["by_jst_weekday"].items()],
        ("全体", comparison),
    ]:
        dist = values["event_abs_post10"]
        lines.append(
            f"| {label} | {dist['n']} | {fmt(dist['median_bp'])} | {fmt(values['median_ratio'])} "
            f"| {values['above_control_p90']}/{dist['n']} "
            f"| {values['null_expected_count']:g}/{dist['n']} |"
        )
    lines += [
        "", "全体の中央値と比は全測定値から計算。p90超過件数は同曜日対照との比較を合算。",
        "", "## 記述統計", "",
        ("| 群 | JST曜日 | abs(pre10) n | abs(pre10) 中央値 bp | abs(pre10) p90 bp "
         "| abs(pre10) p95 bp | abs(post60) n | abs(post60) 中央値 bp "
         "| post10スプレッド n | 日別スプレッド中央値の中央値 bp |"),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group, by_weekday in (
        ("事象", report["descriptive"]["events_by_jst_weekday"]),
        ("対照", {day: values["descriptive"]
                  for day, values in control["by_jst_weekday"].items()}),
    ):
        overall = report["descriptive"]["events" if group == "事象" else "controls"]
        for label, values in [
            *[(weekday_label(day), values) for day, values in by_weekday.items()],
            ("全体", overall),
        ]:
            pre, post, spread = (values[key] for key in (
                "abs_pre10", "abs_post60", "post10_spread_bp",
            ))
            lines.append(
                f"| {group} | {label} | {pre['n']} | {fmt(pre['median_bp'])} "
                f"| {fmt(pre['p90_bp'])} | {fmt(pre['p95_bp'])} | {post['n']} "
                f"| {fmt(post['median_bp'])} | {spread['n']} | {fmt(spread['median_bp'])} |"
            )
    secondary = report["secondary_post60"]
    lines += [
        "", "## 副次窓 post60（判定なし）", "",
        (f"同曜日対照に対する abs(post60) のパーセンタイル中央値 = "
         f"{fmt(secondary['median_percentile'])}（n={secondary['n']}, "
         f"対照90パーセンタイル超過 = {secondary['above_control_p90']}件）。"),
        "",
        ("**post60 に閾値は事前登録していない。ここから判定を出さない。**"
         "主判定が not_established のとき、反応が無いのか窓の取り方が短すぎるのかを"
         "見分けるための材料として置いてある。"),
        "",
        "## 向き（副次）", "", "解釈するのは主判定が reacts の場合のみ。", "",
    ]
    for key, label in (("keyword_change", "前会合差"), ("keyword_level", "水準")):
        direction = report["direction"][key]
        lines += [(f"Spearman（キーワード指標の{label} と post10 bp）: "
                   f"n={direction['n']}, rho={fmt(direction['rho'])}"), ""]
    lines += ["not_established は反応がないことを意味しない。収益性・執行可能性・Gate通過は判定しない。", ""]
    return "\n".join(lines)


def study_definition() -> dict[str, Any]:
    return {
        "SYMBOL": SYMBOL, "STUDY_VERSION": STUDY_VERSION,
        "PRE_WINDOW_seconds": PRE_WINDOW.total_seconds(),
        "POST_PRIMARY_seconds": POST_PRIMARY.total_seconds(),
        "POST_SECONDARY_seconds": POST_SECONDARY.total_seconds(),
        "MINIMUM_TICKS": MINIMUM_TICKS, "REACTS_THRESHOLD": REACTS_THRESHOLD,
        "CONTROL_RADIUS_DAYS": CONTROL_RADIUS_DAYS,
        "MEETING_EXCLUSION_DAYS": MEETING_EXCLUSION_DAYS,
        "MINIMUM_CONTROLS_PER_WEEKDAY": MINIMUM_CONTROLS_PER_WEEKDAY,
        "RETRY_WAITS": list(RETRY_WAITS), "REQUEST_INTERVAL_SECONDS": REQUEST_INTERVAL_SECONDS,
        "OUTAGE_THRESHOLD": OUTAGE_THRESHOLD, "OUTAGE_PAUSE_SECONDS": OUTAGE_PAUSE_SECONDS,
        "OUTAGE_MAX_PAUSES": OUTAGE_MAX_PAUSES,
        "TARGET_DATES": [day.isoformat() for day in TARGET_DATES],
        "SCORING_VERSION": SCORING_VERSION, "KEYWORD_VERSION": KEYWORD_VERSION,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--meetings", type=Path, default=DEFAULT_MEETINGS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=Path("tmp/opinions-reaction/cache"))
    args = parser.parse_args(argv)
    started_at = datetime.now(UTC)
    run_dir = args.run_dir or (
        Path("tmp/opinions-reaction") / started_at.strftime("%Y%m%dT%H%M%S%fZ")
    )
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"出力先を作成できません（既存ディレクトリは指定できません）: {exc}", file=sys.stderr)
        return 1
    manifest: dict[str, Any] = {
        "study_version": STUDY_VERSION, "definition": study_definition(),
        "started_at": started_at.isoformat(), "cache_dir": str(args.cache_dir.resolve()),
        "meetings_path": str(args.meetings.resolve()), "status": "preparing",
    }
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest["meetings_sha256"] = hashlib.sha256(args.meetings.read_bytes()).hexdigest()
        meetings = load_meetings(args.meetings)
        cases = prepare_cases(select_meetings(meetings), args.cache_dir)
        manifest["events"] = [
            {"decision_date": c.decision_date.isoformat(),
             "t0": c.t0.isoformat() if c.t0 else None,
             "keyword_balance": str(c.balance) if c.balance is not None else None,
             "preparation_error": c.preparation_error, "keyword_error": c.keyword_error}
            for c in cases
        ]
        write_json(manifest_path, manifest)
        # 公表日不明のまま対照を組むと、その公表日を除外できなくなる。
        if any(c.t0 is None for c in cases):
            raise ValueError("全20件の公表日時を確認できないため、対照群を確定できません")
        publications = [c.t0 for c in cases]
        candidates, excluded = control_days(publications, meetings)
        manifest.update({
            "status": "fetching", "control_candidates": [t.isoformat() for t in candidates],
            "excluded_control_days": excluded,
            "event_period_utc": {"first": min(publications).date().isoformat(),
                                 "last": max(publications).date().isoformat()},
        })
        write_json(manifest_path, manifest)
        hours = sorted({hour for t0 in [*publications, *candidates] for hour in hours_for(t0)})
        errors = fetch_hours(hours, args.cache_dir)
        manifest["tick_fetch_errors"] = {hour.isoformat(): error for hour, error in errors.items()}
        events = {c.decision_date: observe(c.t0, args.cache_dir, errors) for c in cases}
        controls, rejected = [], []
        for t0 in candidates:
            observation = observe(t0, args.cache_dir, errors)
            reason = control_exclusion(observation)
            if reason:
                excluded[t0.date().isoformat()] = [reason]
                rejected.append({
                    "reason": reason, "observation": observation.model_dump(mode="json"),
                })
            else:
                controls.append(observation)
        manifest.update({
            "status": "analyzing", "controls": [o.t0.isoformat() for o in controls],
            "rejected_controls": rejected,
            "excluded_control_days": excluded,
        })
        write_json(manifest_path, manifest)
        report = summarize(cases, events, controls, excluded)
        write_json(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(render_report(report), encoding="utf-8")
        manifest["status"] = "completed"
        write_json(manifest_path, manifest)
    except (OSError, ValueError, RuntimeError) as exc:
        manifest.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        write_json(manifest_path, manifest)
        print(f"測定を終了しました: {exc}", file=sys.stderr)
        return 1
    print(f"{report['primary']['verdict']}: {run_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
