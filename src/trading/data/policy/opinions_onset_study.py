"""主な意見の公表時刻に最大の1分リターンが集まるかを測る研究ハーネス。

python -m trading.data.policy.opinions_onset_study --run-dir tmp/opinions-onset/run-1
事前登録: docs/research/2026-09-21-opinions-onset-timing.md
"""
from __future__ import annotations

import argparse
import hashlib
import math
import sys
from bisect import bisect_left
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from trading.data.policy.extraction_study import write_json
from trading.data.policy.meetings import DEFAULT_MEETINGS_PATH, load_meetings
from trading.data.policy.opinions_reaction_study import (
    CONTROL_RADIUS_DAYS,
    JST,
    MEETING_EXCLUSION_DAYS,
    MINIMUM_CONTROLS_PER_WEEKDAY,
    MINIMUM_TICKS,
    POST_PRIMARY,
    SYMBOL,
    Case,
    control_days,
    fetch_hours,
    hours_for,
    prepare_cases,
    read_cached_ticks,
    select_meetings,
    window_stats,
)
from trading.domain.market import Tick

STUDY_VERSION = "opinions_onset_timing_v1"
ONSET_BEFORE = timedelta(minutes=10)
ONSET_AFTER = timedelta(minutes=10)
BIN = timedelta(minutes=1)
ONSET_MINIMUM_COUNT = 4
ONSET_RATIO = 3
SUBBIN = timedelta(seconds=10)
EXPECTED_CONTROL_CANDIDATES = 231
# 段階3bの結果を見てから選んだ事後の部分集合。主判定には使わない。
STAGE3B_TAIL_DATES = (
    date(2024, 4, 26), date(2024, 7, 31), date(2024, 9, 20),
    date(2025, 6, 17), date(2025, 9, 19),
)


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True)

    t0: datetime
    returns_bp: tuple[float, ...] = ()
    k_star: int | None = None
    subbin_max: int | None = None
    post10_tick_count: int | None = None
    missing_reason: str | None = None
    error: str | None = None
    post10_error: str | None = None


def onset_bins(t0: datetime) -> list[tuple[int, datetime, datetime]]:
    return [(k, t0 + k * BIN, t0 + (k + 1) * BIN)
            for k in range(-ONSET_BEFORE // BIN, ONSET_AFTER // BIN)]


def measure(ticks: Sequence[Tick], t0: datetime) -> Observation:
    post10 = window_stats(ticks, t0, t0 + POST_PRIMARY)
    tick_count = post10.tick_count if post10 is not None else None
    start_index = bisect_left(ticks, t0 - ONSET_BEFORE, key=lambda tick: tick.time) - 1
    if start_index < 0:
        return Observation(t0=t0, post10_tick_count=tick_count,
                           missing_reason="missing_start_price")
    price = ticks[start_index].mid
    returns = []
    bins = onset_bins(t0)
    for _, _, end in bins:
        next_price = ticks[bisect_left(ticks, end, key=lambda tick: tick.time) - 1].mid
        returns.append(math.log(float(next_price / price)) * 10_000)
        price = next_price
    largest = max(range(len(returns)), key=lambda i: abs(returns[i]))
    if returns[largest] == 0:
        return Observation(t0=t0, returns_bp=tuple(returns), post10_tick_count=tick_count,
                           missing_reason="no_movement")
    k_star = bins[largest][0]
    subbin_max = None
    if k_star == 0:
        subprices = [
            ticks[bisect_left(ticks, t0 + i * SUBBIN, key=lambda tick: tick.time) - 1].mid
            for i in range(BIN // SUBBIN + 1)
        ]
        subreturns = [
            math.log(float(end_price / start_price)) * 10_000
            for start_price, end_price in pairwise(subprices)
        ]
        subbin_max = max(range(len(subreturns)), key=lambda i: abs(subreturns[i]))
    return Observation(t0=t0, returns_bp=tuple(returns), k_star=k_star,
                       subbin_max=subbin_max, post10_tick_count=tick_count)


def required_hours(t0: datetime) -> list[datetime]:
    # post10の終点ちょうどのtickも流動性判定に必要なので、そのUTC時間帯まで読む。
    return [hour for hour in hours_for(t0)
            if hour + timedelta(hours=1) > t0 - ONSET_BEFORE
            and hour <= t0 + ONSET_AFTER]


def observe(t0: datetime, cache_dir: Path, errors: Mapping[datetime, str]) -> Observation:
    ticks, failed_hours = read_cached_ticks(required_hours(t0), cache_dir, errors)
    onset_errors = [f"{hour.isoformat()}: {error}" for hour, error in failed_hours.items()
                    if hour < t0 + ONSET_AFTER]
    post10_error = "; ".join(f"{hour.isoformat()}: {error}"
                             for hour, error in failed_hours.items()) or None
    if onset_errors:
        return Observation(t0=t0, missing_reason="tick_fetch_error",
                           error="; ".join(onset_errors), post10_error=post10_error)
    observation = measure(ticks, t0)
    if post10_error:
        observation = observation.model_copy(update={
            "post10_tick_count": None, "post10_error": post10_error,
        })
    return observation


def control_exclusion(observation: Observation) -> str | None:
    if observation.post10_error:
        return "tick_fetch_error"
    if observation.missing_reason:
        return observation.missing_reason
    if observation.post10_tick_count < MINIMUM_TICKS:
        return "insufficient_post10_ticks"
    return None


def judge(onsets: Sequence[int | None], expected_count: Fraction | float) -> dict[str, Any]:
    n0 = sum(k == 0 for k in onsets)
    missing = sum(k is None for k in onsets)
    if n0 >= ONSET_MINIMUM_COUNT and n0 >= ONSET_RATIO * expected_count:
        verdict = "onset_at_publication"
    elif missing:
        verdict = "incomplete"
    else:
        verdict = "not_established"
    return {"verdict": verdict, "n0": n0, "expected_count": float(expected_count),
            "missing": missing, "n": len(onsets)}


def histogram(observations: Sequence[Observation]) -> dict[str, Any]:
    counts = Counter(o.k_star for o in observations if o.k_star is not None)
    n = sum(counts.values())
    return {
        "n": n, "missing": len(observations) - n, "rate_denominator": "observed_onsets",
        "bins": [{"k": k, "count": counts[k], "rate": counts[k] / n if n else None}
                 for k in range(-ONSET_BEFORE // BIN, ONSET_AFTER // BIN)],
    }


def summarize(
    cases: Sequence[Case], events: Mapping[date, Observation],
    controls: Sequence[Observation], excluded: Mapping[str, list[str]],
) -> dict[str, Any]:
    event_weekdays = [c.t0.astimezone(JST).weekday() for c in cases]
    weekdays = sorted(set(event_weekdays) | {o.t0.astimezone(JST).weekday() for o in controls})
    by_weekday = {
        day: [o for o in controls if o.t0.astimezone(JST).weekday() == day]
        for day in weekdays
    }
    for day in sorted(set(event_weekdays)):
        count = len(by_weekday[day])
        if count < MINIMUM_CONTROLS_PER_WEEKDAY:
            raise ValueError(f"JST {'月火水木金土日'[day]}曜の対照が"
                             f"{MINIMUM_CONTROLS_PER_WEEKDAY}日未満です: {count}日")
    # 整数の閾値ちょうどで丸め誤差により判定が変わらないよう、件数の比で計算する。
    rates = {day: Fraction(sum(o.k_star == 0 for o in group), len(group))
             for day, group in by_weekday.items()}
    post_half_rates = {day: Fraction(sum(o.k_star >= 0 for o in group), len(group))
                       for day, group in by_weekday.items()}
    expected = sum((rates[day] for day in event_weekdays), Fraction())
    onsets = [events[c.decision_date].k_star for c in cases]
    primary = judge(onsets, expected)
    rows = [
        {"decision_date": c.decision_date.isoformat(), "jst_weekday": day,
         **events[c.decision_date].model_dump(mode="json")}
        for c, day in zip(cases, event_weekdays)
    ]
    leave_one_out = [
        {"dropped_decision_date": case.decision_date.isoformat(),
         **judge(onsets[:i] + onsets[i + 1:], expected - rates[event_weekdays[i]])}
        for i, case in enumerate(cases)
    ]
    return {
        "study_version": STUDY_VERSION, "primary": primary, "events": rows,
        "controls": {
            "n": len(controls), "n0": sum(o.k_star == 0 for o in controls),
            "rate": sum(o.k_star == 0 for o in controls) / len(controls),
            "by_jst_weekday": {
                str(day): {"n": len(group), "n0": sum(o.k_star == 0 for o in group),
                           "rate": float(rates[day])}
                for day, group in by_weekday.items()
            },
            "observations": [o.model_dump(mode="json") for o in controls],
            "excluded": dict(excluded),
            "exclusion_counts": dict(Counter(r for reasons in excluded.values() for r in reasons)),
        },
        "histograms": {"events": histogram([events[c.decision_date] for c in cases]),
                       "controls": histogram(controls)},
        # 事前登録した主判定は k* == 0 だけを見る。ヒストグラムを見て気づいた
        # 「k* が公表後の半分に寄る」形は登録しておらず、閾値も無い。判定は出さない。
        "post_hoc_post_half": {
            "verdict": None, "no_threshold_was_preregistered": True,
            "events": sum(o.k_star is not None and o.k_star >= 0
                          for o in (events[c.decision_date] for c in cases)),
            "n": len(cases),
            "expected_count": float(sum(
                (post_half_rates[day] for day in event_weekdays), Fraction(),
            )),
            "controls": {
                "n": len(controls), "count": sum(o.k_star >= 0 for o in controls),
                "rate": sum(o.k_star >= 0 for o in controls) / len(controls),
                "by_jst_weekday": {str(day): float(post_half_rates[day]) for day in weekdays},
            },
        },
        "stage3b_tail": {
            "post_hoc_subset": True, "used_for_primary": False,
            "events": [row for row in rows
                       if date.fromisoformat(row["decision_date"]) in STAGE3B_TAIL_DATES],
        },
        "subbins": {"seconds": SUBBIN.total_seconds(), "events": [
            {"decision_date": row["decision_date"], "bucket": row["subbin_max"]}
            for row in rows if row["k_star"] == 0
        ]},
        "leave_one_out": {
            "n": len(cases),
            "onset_at_publication_count": sum(
                row["verdict"] == "onset_at_publication" for row in leave_one_out
            ),
            "unchanged_verdict_count": sum(
                row["verdict"] == primary["verdict"] for row in leave_one_out
            ),
            "results": leave_one_out,
        },
    }


def render_report(report: Mapping[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "—" if value is None else str(value)

    primary, controls = report["primary"], report["controls"]
    lines = [
        "# 主な意見の公表時刻へのオンセットの集中", "",
        f"主判定: **{primary['verdict']}**", "",
        (f"公表直後の件数 n0 = {primary['n0']}/{primary['n']}、曜日構成を揃えた期待件数 = "
         f"{primary['expected_count']:.4f}、欠測 = {primary['missing']}件、"
         f"対照 = {controls['n']}日。"), "",
        "## 事象ごと", "",
        "| 会合日 | 公表(UTC) | JST曜日 | k* | 分0の10秒バケット | 欠測理由 |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in report["events"]:
        lines.append(
            f"| {row['decision_date']} | {row['t0']} | {'月火水木金土日'[row['jst_weekday']]} "
            f"| {fmt(row['k_star'])} | {fmt(row['subbin_max'])} | {fmt(row['missing_reason'])} |"
        )
    lines += [
        "", "10秒バケットは0..5（0は公表後0〜10秒、5は50〜60秒）。k* = 0の事象のみ。",
        "", "## k* のヒストグラム", "",
        "率の分母は欠測を除いた測定数。主判定の期待件数には欠測した事象も含める。", "",
        "| k（公表からの分） | 事象 件数 | 事象 率 | 対照 件数 | 対照 率 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for event, control in zip(report["histograms"]["events"]["bins"],
                              report["histograms"]["controls"]["bins"]):
        event_rate = "—" if event["rate"] is None else f"{event['rate']:.4f}"
        lines.append(f"| {event['k']} | {event['count']} | {event_rate} "
                     f"| {control['count']} | {control['rate']:.4f} |")
    lines += [
        "", "## 対照の k* = 0 の率", "",
        "| JST曜日 | 対照日数 | k* = 0 件数 | 率 |", "|---|---:|---:|---:|",
    ]
    for label, values in [
        *[("月火水木金土日"[int(day)], values)
          for day, values in controls["by_jst_weekday"].items()], ("全体", controls),
    ]:
        lines.append(f"| {label} | {values['n']} | {values['n0']} | {values['rate']:.4f} |")
    lines += ["", "除外理由（重複を含む）:", "",
              *[f"- {reason}: {count}日" for reason, count in controls["exclusion_counts"].items()],
              "", "## 段階3bで上側に来た5件", "",
              "段階3bの結果を見てから選んだ事後の部分集合であり、主判定には使わない。", "",
              "| 会合日 | k* | 欠測理由 |", "|---|---:|---|"]
    for row in report["stage3b_tail"]["events"]:
        lines.append(f"| {row['decision_date']} | {fmt(row['k_star'])} "
                     f"| {fmt(row['missing_reason'])} |")
    post_half = report["post_hoc_post_half"]
    lines += [
        "", "## 事後に気づいた形: k* が公表後に寄る（判定なし）", "",
        (f"k* >= 0 の事象は {post_half['events']}/{post_half['n']} 件。曜日構成を揃えた"
         f"期待件数は {post_half['expected_count']:.4f} 件"
         f"（対照 {post_half['controls']['count']}/{post_half['controls']['n']} = "
         f"{post_half['controls']['rate']:.4f}）。"), "",
        ("**事前登録した主判定は k* = 0 だけを見る。この形は登録しておらず閾値も無いので、"
         "ここから判定は出さない。** 同じ標本で検定し直すこともしない。"), "",
    ]
    robustness = report["leave_one_out"]
    lines += [
        "", "## 1件落としの頑健性", "",
        (f"onset_at_publication が成立するのは {robustness['onset_at_publication_count']}"
         f"/{robustness['n']}通り。元の主判定と同じなのは "
         f"{robustness['unchanged_verdict_count']}/{robustness['n']}通り。"), "",
        "| 落とした会合日 | 判定 | n0 | 期待件数 | 欠測数 |", "|---|---|---:|---:|---:|",
    ]
    for row in robustness["results"]:
        lines.append(f"| {row['dropped_decision_date']} | {row['verdict']} | {row['n0']} "
                     f"| {row['expected_count']:.4f} | {row['missing']} |")
    lines += [
        "", "not_established は公表時刻に反応がないことを意味しない。",
        "収益性・執行可能性・Data Upgrade Gate通過は判定しない。", "",
    ]
    return "\n".join(lines)


def study_definition() -> dict[str, Any]:
    return {
        "STUDY_VERSION": STUDY_VERSION, "SYMBOL": SYMBOL,
        "ONSET_BEFORE_seconds": ONSET_BEFORE.total_seconds(),
        "ONSET_AFTER_seconds": ONSET_AFTER.total_seconds(), "BIN_seconds": BIN.total_seconds(),
        "SUBBIN_seconds": SUBBIN.total_seconds(), "ONSET_MINIMUM_COUNT": ONSET_MINIMUM_COUNT,
        "ONSET_RATIO": ONSET_RATIO, "POST_PRIMARY_seconds": POST_PRIMARY.total_seconds(),
        "MINIMUM_TICKS": MINIMUM_TICKS,
        "MINIMUM_CONTROLS_PER_WEEKDAY": MINIMUM_CONTROLS_PER_WEEKDAY,
        "CONTROL_RADIUS_DAYS": CONTROL_RADIUS_DAYS, "MEETING_EXCLUSION_DAYS": MEETING_EXCLUSION_DAYS,
        "EXPECTED_CONTROL_CANDIDATES": EXPECTED_CONTROL_CANDIDATES,
        "STAGE3B_TAIL_DATES": [day.isoformat() for day in STAGE3B_TAIL_DATES],
        "onset_window": "[t0 - ONSET_BEFORE, t0 + ONSET_AFTER)",
        "post10_liquidity_window": "[t0, t0 + POST_PRIMARY]",
        "tie_break": "earliest", "all_zero": "no_movement",
        "price": "last tick strictly before endpoint",
        "primary_order": ["onset_at_publication", "incomplete", "not_established"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--meetings", type=Path, default=DEFAULT_MEETINGS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=Path("tmp/opinions-reaction/cache"))
    args = parser.parse_args(argv)
    started_at = datetime.now(UTC)
    run_dir = args.run_dir or Path("tmp/opinions-onset") / started_at.strftime("%Y%m%dT%H%M%S%fZ")
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
             "t0": c.t0.isoformat() if c.t0 else None, "preparation_error": c.preparation_error}
            for c in cases
        ]
        write_json(manifest_path, manifest)
        if any(c.t0 is None for c in cases):
            raise ValueError("全20件の公表日時を確認できないため、対照群を確定できません")
        publications = [c.t0 for c in cases]
        candidates, excluded = control_days(publications, meetings)
        manifest.update({"control_candidates": [t.isoformat() for t in candidates],
                         "excluded_control_days": excluded})
        if len(candidates) != EXPECTED_CONTROL_CANDIDATES:
            raise ValueError(f"対照候補が段階3bの{EXPECTED_CONTROL_CANDIDATES}日と一致しません: "
                             f"{len(candidates)}日")
        hours = sorted({hour for t0 in [*publications, *candidates] for hour in required_hours(t0)})
        manifest.update({"status": "fetching", "hours": [hour.isoformat() for hour in hours]})
        write_json(manifest_path, manifest)
        errors = fetch_hours(hours, args.cache_dir)
        manifest["tick_fetch_errors"] = {hour.isoformat(): error for hour, error in errors.items()}
        events = {c.decision_date: observe(c.t0, args.cache_dir, errors) for c in cases}
        controls, rejected = [], []
        for t0 in candidates:
            observation = observe(t0, args.cache_dir, errors)
            reason = control_exclusion(observation)
            if reason:
                excluded[t0.date().isoformat()] = [reason]
                rejected.append({"reason": reason, "observation": observation.model_dump(mode="json")})
            else:
                controls.append(observation)
        manifest.update({"status": "analyzing", "controls": [o.t0.isoformat() for o in controls],
                         "rejected_controls": rejected, "excluded_control_days": excluded})
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
