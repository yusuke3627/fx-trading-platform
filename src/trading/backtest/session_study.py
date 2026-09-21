"""固定した市場開始前後の1時間を、保存済みBid/Askで探索する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from trading.backtest.execution_quality_study import QuoteObservation
from trading.backtest.rollover import next_rollover_boundary
from trading.backtest.run import git_state
from trading.domain.instrument import InstrumentSpec
from trading.indicators.session import SESSION_WINDOWS_LOCAL, Session, sessions_at

FAMILY_SIZE = 18  # 3市場 × 2方向 ×（開始後・直前・差）
HOUR = timedelta(hours=1)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Exclusion(Record):
    start: AwareDatetime
    end: AwareDatetime
    reason: Literal["macro", "holiday", "data_outage"]

    @model_validator(mode="after")
    def ordered(self) -> Exclusion:
        if self.start >= self.end:
            raise ValueError("除外窓のstartはendより前にしてください")
        return self


class Plan(Record):
    schema_version: Literal["session_study_v1"]
    population_description: str = Field(min_length=1)
    instrument: InstrumentSpec
    from_date: date
    to_date: date
    basis: Literal["observed_utc", "reconstructed", "simulated"]
    quotes_complete: bool
    calendar_source: str = Field(min_length=1)
    calendar_start: AwareDatetime
    calendar_end: AwareDatetime
    exclusions: tuple[Exclusion, ...] = ()
    quote_max_age_seconds: Decimal = Field(gt=0, le=60, decimal_places=6)
    latency_ms: Decimal = Field(ge=0, le=60000, decimal_places=3)
    slippage_pips_per_side: Decimal = Field(ge=0)
    commission_pips_round_trip: Decimal = Field(ge=0)
    server_ahead_of_ny_hours: float = Field(ge=0, le=24)
    minimum_effect_pips: Decimal = Field(gt=0)
    planned_days: int = Field(ge=2)
    sample_size_rationale: str = Field(min_length=1)
    block_days: int = Field(ge=1)
    bootstrap_replicates: int = Field(default=5000, ge=2000)
    seed: int = 1

    @model_validator(mode="after")
    def valid(self) -> Plan:
        if self.from_date > self.to_date:
            raise ValueError("from_dateはto_date以前にしてください")
        if (self.to_date - self.from_date).days > 3660:
            raise ValueError("一度の診断は10年以内にしてください")
        if self.calendar_start >= self.calendar_end:
            raise ValueError("カレンダーの確認範囲が逆転しています")
        if self.instrument.pip_size <= 0:
            raise ValueError("pip_sizeは正の値が必要です")
        if 2 * self.block_days > self.planned_days:
            raise ValueError("planned_daysにはblock_daysの2倍以上が必要です")
        return self


def windows(plan: Plan) -> list[dict]:
    rows = []
    day = plan.from_date
    latency = timedelta(microseconds=int(plan.latency_ms * 1000))
    while day <= plan.to_date:
        if day.weekday() < 5:
            for session, (zone, hour, _) in SESSION_WINDOWS_LOCAL.items():
                start = datetime.combine(day, time(hour), ZoneInfo(zone)).astimezone(UTC)
                rows.append({
                    "day": day.isoformat(), "session": session.value,
                    "week_open": day.weekday() == 0,
                    "active_sessions": sorted(s.value for s in sessions_at(start)),
                    "times": (start - HOUR + latency, start + latency, start + HOUR + latency),
                })
        day += timedelta(days=1)
    return rows


def sample_quotes(path: Path, plan: Plan, targets: list[datetime]) -> tuple[dict, dict]:
    """全入力を検証・hash化し、必要な時点以前のquoteだけを保持する。"""
    targets = sorted(set(targets))
    selected: dict[datetime, QuoteObservation | None] = {}
    previous = None
    first = None
    index = 0
    count = 0
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for line in source:
            digest.update(line)
            if not line.strip():
                continue
            quote = QuoteObservation.model_validate_json(line)
            at = quote.observed_at.at
            if quote.symbol != plan.instrument.symbol or quote.observed_at.basis != plan.basis:
                raise ValueError("quoteのsymbol/時刻由来が計画と一致しません")
            if previous is not None and at <= previous.observed_at.at:
                raise ValueError("quoteは重複のない観測時刻昇順にしてください")
            if first is None:
                first = at
            while index < len(targets) and targets[index] < at:
                selected[targets[index]] = previous
                index += 1
            previous = quote
            count += 1
    while index < len(targets):
        target = targets[index]
        selected[target] = (
            previous if previous is not None and target == previous.observed_at.at else None
        )
        index += 1
    return selected, {
        "sha256": digest.hexdigest(), "quote_count": count,
        "first_at": first, "last_at": previous.observed_at.at if previous else None,
    }


def paired_observation(row: dict, plan: Plan, selected: dict) -> dict:
    start, middle, end = row["times"]
    reasons = []
    if not plan.quotes_complete:
        reasons.append("quote_population_incomplete")
    if start < plan.calendar_start or end > plan.calendar_end:
        reasons.append("calendar_not_covered")
    for exclusion in plan.exclusions:
        if exclusion.start <= end and exclusion.end > start:
            reasons.append(exclusion.reason)
    if next_rollover_boundary(start - timedelta(microseconds=1), plan.server_ahead_of_ny_hours) <= end:
        reasons.append("rollover_excluded")
    quotes = [selected[t] for t in row["times"]]
    scheduled_exclusion = any(r in reasons for r in ("macro", "holiday", "rollover_excluded"))
    for target, quote in zip(row["times"], quotes, strict=True):
        if scheduled_exclusion:
            break
        if quote is None:
            reasons.append("quote_missing")
        elif target - quote.observed_at.at > timedelta(
            microseconds=int(plan.quote_max_age_seconds * 1_000_000)
        ):
            reasons.append("quote_stale")
    result = {k: v for k, v in row.items() if k != "times"}
    result.update({"start": start, "middle": middle, "end": end,
                   "quotes": [q.model_dump(mode="json") if q else None for q in quotes],
                   "excluded_reasons": sorted(set(reasons)), "directions": {}})
    if reasons:
        return result
    first, center, last = quotes
    pip = plan.instrument.pip_size
    execution_cost = plan.slippage_pips_per_side * 2 + plan.commission_pips_round_trip
    for direction, sign in (("LONG", Decimal(1)), ("SHORT", Decimal(-1))):
        values = {}
        for period, entry, exit_ in (("before", first, center), ("after", center, last)):
            gross = (((exit_.bid + exit_.ask) - (entry.bid + entry.ask)) / 2) * sign / pip
            net = (
                ((exit_.bid - entry.ask) if sign > 0 else (entry.bid - exit_.ask)) / pip
                - execution_cost
            )
            values[period] = {"mid_pips": gross, "net_pips": net}
        values["difference_net_pips"] = values["after"]["net_pips"] - values["before"]["net_pips"]
        result["directions"][direction] = values
    return result


def interval(values: list[Decimal | None], plan: Plan) -> list[Decimal] | None:
    """営業日単位の循環moving-block bootstrap。18区間をBonferroni補正する。"""
    if sum(v is not None for v in values) < 2 * plan.block_days:
        return None
    rng = random.Random(plan.seed)
    means = []
    count = len(values)
    for _ in range(plan.bootstrap_replicates):
        sample = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(values[(start + j) % count] for j in range(plan.block_days))
        measured = [v for v in sample[:count] if v is not None]
        if measured:
            means.append(sum(measured, Decimal(0)) / len(measured))
    means.sort()
    tail = Decimal("0.1") / FAMILY_SIZE / 2
    low = int(tail * (len(means) - 1))
    high = len(means) - 1 - low
    return [means[low], means[high]]


def summarize(rows: list[dict], plan: Plan) -> list[dict]:
    summaries = []
    for session in Session:
        candidates = [r for r in rows if r["session"] == session.value]
        included = [r for r in candidates if not r["excluded_reasons"]]
        reasons = Counter(reason for row in candidates for reason in row["excluded_reasons"])
        # カレンダーで予定除外した日は標本外。価格欠測・記録不足は判定を保留する。
        missing = any(reasons[r] for r in (
            "quote_population_incomplete", "calendar_not_covered", "quote_missing", "quote_stale",
            "data_outage",
        ))
        for direction in ("LONG", "SHORT"):
            before = [r["directions"][direction]["before"]["net_pips"]
                      if not r["excluded_reasons"] else None for r in candidates]
            after = [r["directions"][direction]["after"]["net_pips"]
                     if not r["excluded_reasons"] else None for r in candidates]
            difference = [a - b if a is not None and b is not None else None
                          for a, b in zip(after, before, strict=True)]
            metrics = {}
            for name, values in (("before", before), ("after", after), ("difference", difference)):
                measured = [v for v in values if v is not None]
                metrics[name] = {
                    "mean_net_pips": sum(measured, Decimal(0)) / len(measured) if measured else None,
                    "interval": interval(values, plan),
                }
            status = "inconclusive"
            if missing or len(included) < plan.planned_days:
                status = "hold"
            elif metrics["after"]["interval"][1] <= 0 or metrics["difference"]["interval"][1] <= 0:
                status = "stop"
            elif (metrics["after"]["interval"][0] >= plan.minimum_effect_pips
                  and metrics["difference"]["interval"][0] > 0):
                status = "confirmation_candidate"
            if plan.basis == "simulated":
                status = "synthetic_only"
            summaries.append({
                "session": session.value, "direction": direction, "eligible_days": len(candidates),
                "measured_days": len(included), "excluded_days": len(candidates) - len(included),
                "exclusion_counts": dict(reasons), "metrics": metrics, "status": status,
            })
    return summaries


def run(plan: Plan, quotes: Path) -> dict:
    planned = windows(plan)
    selected, source = sample_quotes(quotes, plan, [t for r in planned for t in r["times"]])
    rows = [paired_observation(row, plan, selected) for row in planned]
    return {
        "schema_version": "session_study_report_v1", "split": "exploratory",
        "profitability_established": False,
        "family_size": FAMILY_SIZE, "family_confidence": "0.90",
        "plan": plan.model_dump(mode="json"), "quote_source": source,
        "summary": summarize(rows, plan), "observations": rows,
    }


def markdown(report: dict) -> str:
    lines = ["# 時間帯別の探索診断", "", "候補の選別用です。収益性や未使用期間での再現性は未確認です。", "",
             f'時刻の由来: {report["plan"]["basis"]}。simulated は合成データの動作確認です。', "",
             "| 市場 | 方向 | 計測日 / 対象日 | 開始後の純pips平均 | 直前との差 | 状態 |",
             "| --- | --- | --- | --- | --- | --- |"]
    statuses = {"hold": "保留", "stop": "中止", "inconclusive": "判定不能",
                "confirmation_candidate": "未使用期間での確認候補", "synthetic_only": "合成データのみ"}
    for row in report["summary"]:
        lines.append(
            f'| {row["session"]} | {row["direction"]} | {row["measured_days"]} / '
            f'{row["eligible_days"]} | {row["metrics"]["after"]["mean_net_pips"]} | '
            f'{row["metrics"]["difference"]["mean_net_pips"]} | {statuses[row["status"]]} |'
        )
    lines.append("")
    for row in report["summary"]:
        if row["direction"] == "LONG" and row["exclusion_counts"]:
            lines.append(f'{row["session"]} の除外: {row["exclusion_counts"]}\n')
    lines.extend(["", "全18区間にBonferroni補正。日単位のmoving-block bootstrapによる近似区間です。",
                  "詳細な区間・除外理由・時刻由来・入力hashは report.json に保存しています。"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw_plan = args.plan.read_bytes()
        plan = Plan.model_validate_json(raw_plan)
        report = run(plan, args.quotes)
        report["plan_sha256"] = hashlib.sha256(raw_plan).hexdigest()
        report["git"] = git_state()
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "plan.json").write_bytes(raw_plan)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, default=str, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / "report.md").write_text(markdown(report), encoding="utf-8")
    except (OSError, ValueError, ValidationError) as error:
        print(f"入力または出力エラー: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
