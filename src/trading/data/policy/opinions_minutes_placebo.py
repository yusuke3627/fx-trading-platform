"""議事要旨の公表後へのオンセットの偏りを測るプラセボ研究ハーネス。

python -m trading.data.policy.opinions_minutes_placebo --run-dir tmp/opinions-minutes/run-1
事前登録: docs/research/2026-09-22-opinions-minutes-placebo.md
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from http.client import HTTPException
from math import comb
from pathlib import Path
from typing import Any

from trading.data.policy.extraction_study import Document, fetch_document, write_json
from trading.data.policy.meetings import DEFAULT_MEETINGS_PATH, PolicyMeeting, load_meetings
from trading.data.policy.opinions import (
    extract_pdf_text,
    japanese_statement_pdf_url,
    minutes_publication_from_statement,
    publication_from_statement,
)
from trading.data.policy.opinions_onset_study import (
    BIN,
    ONSET_AFTER,
    ONSET_BEFORE,
    Observation,
    control_exclusion,
    histogram,
    observe,
    required_hours,
)
from trading.data.policy.opinions_reaction_study import (
    JST,
    MEETING_EXCLUSION_DAYS,
    MINIMUM_CONTROLS_PER_WEEKDAY,
    MINIMUM_TICKS,
    SYMBOL,
    control_days,
    fetch_hours,
    select_meetings,
)

STUDY_VERSION = "opinions_minutes_placebo_v1"
CONTROL_RADIUS_DAYS = 15
AS_OF = date(2026, 9, 22)
EXPECTED_EVENTS = 14
EXPECTED_CONTROL_CANDIDATES = 236
ALPHA = Fraction(5, 100)
PREREGISTRATION = "docs/research/2026-09-22-opinions-minutes-placebo.md"


@dataclass(frozen=True)
class Case:
    decision_date: date
    t0: datetime | None
    opinions_t0: datetime | None = None
    preparation_error: str | None = None


def prepare_cases(
    meetings: Sequence[PolicyMeeting], cache_dir: Path,
    fetch: Callable[[str, Path], Document] = fetch_document,
    extract_text: Callable[[bytes], str] = extract_pdf_text,
) -> list[Case]:
    opinions_dates = {m.decision_date for m in select_meetings(meetings)}
    # 主な意見の20会合に含まれない会合も、議事要旨の除外日には必要。
    selected = sorted((m for m in meetings if m.bank == "BOJ"), key=lambda m: m.decision_date)
    cases = []
    for index, meeting in enumerate(selected, 1):
        t0 = opinions_t0 = preparation_error = None
        try:
            document = fetch(japanese_statement_pdf_url(meeting.decision_date), cache_dir)
            if document.kind != "pdf":
                raise ValueError("声明の原文がPDFではありません")
            text = extract_text(document.raw)
            t0 = minutes_publication_from_statement(text, meeting.decision_date).astimezone(UTC)
            if meeting.decision_date in opinions_dates:
                opinions_t0 = publication_from_statement(text, meeting.decision_date).astimezone(UTC)
        except (OSError, ValueError, RuntimeError, HTTPException) as exc:
            preparation_error = f"{type(exc).__name__}: {exc}"
        cases.append(Case(meeting.decision_date, t0, opinions_t0, preparation_error))
        print(f"原文準備 {index}/{len(selected)}: {meeting.decision_date}",
              file=sys.stderr, flush=True)
    return cases


def select_events(
    cases: Sequence[Case], meetings: Sequence[PolicyMeeting],
) -> tuple[list[Case], dict[str, list[str]]]:
    if any(c.t0 is None or c.preparation_error for c in cases):
        raise ValueError("全会合の公表日時を確認できないため、事象・対照群を確定できません")
    policy_days = {
        m.decision_date + timedelta(days=offset)
        for m in meetings for offset in range(-MEETING_EXCLUSION_DAYS, MEETING_EXCLUSION_DAYS + 1)
    }
    selected, excluded = [], {}
    for case in cases:
        day = case.t0.astimezone(JST).date()
        reasons = []
        if day > AS_OF:
            reasons.append("not_yet_published")
        if day.weekday() >= 5:
            reasons.append("weekend")
        if day in policy_days:
            reasons.append("policy_decision_window")
        if reasons:
            excluded[case.decision_date.isoformat()] = reasons
        else:
            selected.append(case)
    if len(selected) != EXPECTED_EVENTS:
        raise ValueError(f"事象が事前登録の{EXPECTED_EVENTS}件と一致しません: {len(selected)}件")
    return selected, excluded


def judge(onsets: Sequence[int | None], p_hat: Fraction) -> dict[str, Any]:
    n = len(onsets)
    # p_hatが大きく、全件が公表後でも裾がALPHAを超える場合は到達不能なn+1になる。
    threshold = next(m for m in range(n + 2) if sum(
        (comb(n, k) * p_hat ** k * (1 - p_hat) ** (n - k) for k in range(m, n + 1)),
        Fraction(),
    ) <= ALPHA)
    m = sum(k is not None and k >= 0 for k in onsets)
    missing = sum(k is None for k in onsets)
    if m >= threshold:
        verdict = "not_specific_to_opinions"
    elif missing:
        verdict = "incomplete"
    else:
        verdict = "not_established"
    return {"verdict": verdict, "n": n, "p_hat": float(p_hat),
            "threshold": threshold, "m": m, "missing": missing}


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
    rates = {day: Fraction(sum(o.k_star >= 0 for o in group), len(group))
             for day, group in by_weekday.items()}
    p_hat = sum((rates[day] for day in event_weekdays), Fraction()) / len(cases)
    primary = judge([events[c.decision_date].k_star for c in cases], p_hat)
    return {
        "study_version": STUDY_VERSION, "as_of": AS_OF.isoformat(),
        "preregistration": PREREGISTRATION, "primary": primary,
        "events": [
            {"decision_date": c.decision_date.isoformat(), "jst_weekday": day,
             **events[c.decision_date].model_dump(mode="json")}
            for c, day in zip(cases, event_weekdays)
        ],
        "controls": {
            "n": len(controls), "post_half_count": sum(o.k_star >= 0 for o in controls),
            "rate": sum(o.k_star >= 0 for o in controls) / len(controls),
            "by_jst_weekday": {
                str(day): {"n": len(group), "post_half_count": sum(o.k_star >= 0 for o in group),
                           "rate": float(rates[day])}
                for day, group in by_weekday.items()
            },
            "observations": [o.model_dump(mode="json") for o in controls],
            "excluded": dict(excluded),
            "exclusion_counts": dict(Counter(r for reasons in excluded.values() for r in reasons)),
        },
        "histograms": {"controls": histogram(controls)},
    }


def render_report(report: Mapping[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "—" if value is None else str(value)

    primary, controls = report["primary"], report["controls"]
    lines = [
        "# 議事要旨の公表後へのオンセットの偏り（段階3b-3）", "",
        f"主判定: **{primary['verdict']}**", "",
        (f"公表後の件数 m = {primary['m']}/{primary['n']}、"
         f"曜日構成で重みづけた帰無率 = {primary['p_hat']:.4f}、"
         f"閾値 = {primary['threshold']}件、欠測 = {primary['missing']}件、"
         f"対照 = {controls['n']}日。"), "",
        f"公表済みの基準日: {report['as_of']}（JST）。事前登録: {report['preregistration']}", "",
        "## 事象ごと", "",
        "| 会合日 | 議事要旨の公表(UTC) | JST曜日 | k* | post10 tick数 | 欠測理由 |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in report["events"]:
        note = "; ".join(str(row[key]) for key in ("missing_reason", "error", "post10_error")
                         if row[key]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {row['decision_date']} | {row['t0']} | {'月火水木金土日'[row['jst_weekday']]} "
            f"| {fmt(row['k_star'])} | {fmt(row['post10_tick_count'])} | {note or '—'} |"
        )
    lines += [
        "", "## 対照の k* >= 0 の率", "",
        "| JST曜日 | 対照日数 | k* >= 0 件数 | 率 |", "|---|---:|---:|---:|",
    ]
    for label, values in [
        *[("月火水木金土日"[int(day)], values)
          for day, values in controls["by_jst_weekday"].items()], ("全体", controls),
    ]:
        lines.append(f"| {label} | {values['n']} | {values['post_half_count']} "
                     f"| {values['rate']:.4f} |")
    lines += [
        "", "除外理由（重複を含む）:", "",
        *[f"- {reason}: {count}日" for reason, count in controls["exclusion_counts"].items()],
        "", "## 対照の k* のヒストグラム", "",
        "| k（公表からの分） | 件数 | 率 |", "|---:|---:|---:|",
    ]
    for row in report["histograms"]["controls"]["bins"]:
        lines.append(f"| {row['k']} | {row['count']} | {row['rate']:.4f} |")
    lines += [
        "", "欠測した事象は公表後ではなかったと数える。閾値に届かず欠測があれば判定はincomplete。",
        "not_established は主な意見の結果を確認するものではない。",
        "収益性・執行可能性・Data Upgrade Gate通過は判定しない。", "",
    ]
    return "\n".join(lines)


def study_definition() -> dict[str, Any]:
    return {
        "STUDY_VERSION": STUDY_VERSION, "SYMBOL": SYMBOL, "AS_OF": AS_OF.isoformat(),
        "PREREGISTRATION": PREREGISTRATION, "ALPHA": str(ALPHA),
        "EXPECTED_EVENTS": EXPECTED_EVENTS, "EXPECTED_CONTROL_CANDIDATES": EXPECTED_CONTROL_CANDIDATES,
        "CONTROL_RADIUS_DAYS": CONTROL_RADIUS_DAYS, "MEETING_EXCLUSION_DAYS": MEETING_EXCLUSION_DAYS,
        "MINIMUM_TICKS": MINIMUM_TICKS, "MINIMUM_CONTROLS_PER_WEEKDAY": MINIMUM_CONTROLS_PER_WEEKDAY,
        "ONSET_BEFORE_seconds": ONSET_BEFORE.total_seconds(),
        "ONSET_AFTER_seconds": ONSET_AFTER.total_seconds(), "BIN_seconds": BIN.total_seconds(),
        "onset_window": "[t0 - ONSET_BEFORE, t0 + ONSET_AFTER)",
        "post10_liquidity_window": "[t0, t0 + ONSET_AFTER]",
        "price": "last tick strictly before endpoint", "tie_break": "earliest",
        "all_zero": "no_movement", "primary_order": [
            "not_specific_to_opinions", "incomplete", "not_established",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--meetings", type=Path, default=DEFAULT_MEETINGS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=Path("tmp/opinions-reaction/cache"))
    args = parser.parse_args(argv)
    started_at = datetime.now(UTC)
    run_dir = args.run_dir or Path("tmp/opinions-minutes") / started_at.strftime("%Y%m%dT%H%M%S%fZ")
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
        prepared = prepare_cases(meetings, args.cache_dir)
        manifest["publications"] = [
            {"decision_date": c.decision_date.isoformat(),
             "t0": c.t0.isoformat() if c.t0 else None,
             "opinions_t0": c.opinions_t0.isoformat() if c.opinions_t0 else None,
             "preparation_error": c.preparation_error}
            for c in prepared
        ]
        write_json(manifest_path, manifest)
        cases, excluded_events = select_events(prepared, meetings)
        publications = [c.t0 for c in cases]
        extra_days = {t.astimezone(JST).date() for c in prepared
                      for t in (c.t0, c.opinions_t0) if t is not None}
        candidates, excluded = control_days(
            publications, meetings, radius_days=CONTROL_RADIUS_DAYS,
            extra_excluded_jst_days=extra_days,
        )
        manifest.update({
            "events": [{"decision_date": c.decision_date.isoformat(), "t0": c.t0.isoformat()}
                       for c in cases],
            "excluded_events": excluded_events,
            "control_candidates": [t.isoformat() for t in candidates],
            "excluded_control_days": excluded,
        })
        if len(candidates) != EXPECTED_CONTROL_CANDIDATES:
            raise ValueError(f"対照候補が事前登録の{EXPECTED_CONTROL_CANDIDATES}日と一致しません: "
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
