"""保存済みの足から特徴量の IC・分位・月次安定性を診断する研究 CLI。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import NormalDist, fmean, pstdev, stdev
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from trading.backtest.research import BAR_CSV_HEADER
from trading.backtest.run import git_state
from trading.data.market.bars import bucket_start
from trading.domain.instrument import InstrumentSpec
from trading.domain.market import TIMEFRAME_SECONDS, Bar
from trading.indicators import DEFAULT_BAR_COUNT
from trading.indicators.atr import atr
from trading.indicators.ema import ema_series


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Period(Record):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> Period:
        if self.start >= self.end:
            raise ValueError("期間の start は end より前にしてください")
        return self


class Feature(Record):
    id: str = Field(min_length=1, pattern=r"\S")


class RangePosition(Feature):
    kind: Literal["range_position"]
    lookback: int = Field(ge=1, strict=True)


class EmaSlopeAtr(Feature):
    kind: Literal["ema_slope_atr"]
    ema_period: int = Field(ge=1, strict=True)
    slope_lookback: int = Field(ge=1, strict=True)


class DistanceFromEmaAtr(Feature):
    kind: Literal["distance_from_ema_atr"]
    ema_period: int = Field(ge=1, strict=True)


class MomentumAtr(Feature):
    kind: Literal["momentum_atr"]
    lookback: int = Field(ge=1, strict=True)


FeatureSpec = Annotated[
    RangePosition | EmaSlopeAtr | DistanceFromEmaAtr | MomentumAtr,
    Field(discriminator="kind"),
]
PositiveInt = Annotated[int, Field(ge=1, strict=True)]


class Plan(Record):
    schema_version: Literal["feature_screen_v1"]
    population_description: str = Field(min_length=1, pattern=r"\S")
    basis: Literal["replay_bars", "simulated"]
    instrument: InstrumentSpec
    timeframe: str
    explore: Period
    confirm: Period
    max_gap_hours: float = Field(gt=0)
    indicator_bars: int = Field(default=DEFAULT_BAR_COUNT, ge=1, strict=True)
    atr_period: int = Field(default=14, ge=1, strict=True)
    normalization_window: int = Field(ge=20, strict=True)
    entry_z: float = Field(gt=0)
    features: tuple[FeatureSpec, ...] = Field(min_length=1)
    horizons: tuple[PositiveInt, ...] = Field(min_length=1)
    round_trip_cost_pips: Decimal = Field(gt=0)
    cost_rationale: str = Field(min_length=1, pattern=r"\S")
    quantiles: int = Field(ge=2, le=10, strict=True)
    min_samples: int = Field(ge=30, strict=True)
    min_month_samples: int = Field(ge=5, strict=True)
    staircase_min: float = Field(ge=0, le=1)
    month_sign_min: float = Field(ge=0, le=1)
    confidence: float = Field(default=0.90, ge=0.5, le=0.999)

    @model_validator(mode="after")
    def valid(self) -> Plan:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("timeframe は TIMEFRAME_SECONDS のキーを指定してください")
        if self.explore.end > self.confirm.start:
            raise ValueError("探索期間の終了は確認期間の開始以前にしてください")
        if len({f.id for f in self.features}) != len(self.features):
            raise ValueError("特徴量の id は一意にしてください")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons は重複のない昇順にしてください")
        pip = self.instrument.pip_size
        if not pip.is_finite() or pip <= 0:
            raise ValueError("pip_size は有限の正の値が必要です")
        for name, value in (("pip_size", pip), ("round_trip_cost_pips", self.round_trip_cost_pips)):
            if not math.isfinite(float(value)) or float(value) == 0:
                raise ValueError(f"{name} が統計計算の数値範囲を超えています")
        for feature in self.features:
            if isinstance(feature, RangePosition):
                required = feature.lookback + 1
            elif isinstance(feature, MomentumAtr):
                required = max(feature.lookback + 1, self.atr_period + 1)
            elif isinstance(feature, EmaSlopeAtr):
                required = max(feature.ema_period + feature.slope_lookback, self.atr_period + 1)
            else:
                required = max(feature.ema_period, self.atr_period + 1)
            if self.indicator_bars < required:
                raise ValueError(f"{feature.id}: indicator_bars は {required} 以上が必要です")
        return self


def load_bars(path: Path, plan: Plan) -> tuple[list[Bar], dict]:
    bars: list[Bar] = []
    digest = hashlib.sha256()
    step = timedelta(seconds=TIMEFRAME_SECONDS[plan.timeframe])
    with path.open("rb") as source:
        header = source.readline()
        digest.update(header)
        if header.decode("utf-8").rstrip("\r\n") != BAR_CSV_HEADER.rstrip("\n"):
            raise ValueError("CSV ヘッダが research.BAR_CSV_HEADER と一致しません")
        for number, raw in enumerate(source, start=2):
            digest.update(raw)
            try:
                row = next(csv.reader([raw.decode("utf-8")], strict=True))
                if len(row) != 6:
                    raise ValueError("CSV は 6 列が必要です")
                start = datetime.fromisoformat(row[0])
                if start.tzinfo is None or start.utcoffset() is None:
                    raise ValueError("start は timezone-aware にしてください")
                if bars and start <= bars[-1].start:
                    raise ValueError("start は重複のない昇順にしてください")
                if bucket_start(start, plan.timeframe) != start:
                    raise ValueError("start が時間足の境界に揃っていません")
                prices = [Decimal(value) for value in row[1:5]]
                if any(not value.is_finite() or value <= 0 for value in prices):
                    raise ValueError("OHLC は有限の正の値が必要です")
                if any(not math.isfinite(float(value)) or float(value) == 0 for value in prices):
                    raise ValueError("OHLC が指標計算の数値範囲を超えています")
                open_, high, low, close = prices
                if high < max(open_, close) or low > min(open_, close):
                    raise ValueError("OHLC の高安が矛盾しています")
                volume = int(row[5])
                if volume < 0:
                    raise ValueError("tick_volume は非負の整数が必要です")
                bars.append(Bar(
                    symbol=plan.instrument.symbol, timeframe=plan.timeframe, start=start,
                    open=open_, high=high, low=low, close=close, tick_volume=volume,
                    # CSV に実観測時刻はない。指標用の仮置きで、PIT 判定には使わない。
                    known_at=start + step,
                ))
            except (ValueError, InvalidOperation, csv.Error, OverflowError) as error:
                raise ValueError(f"CSV {number} 行目: {error}") from error
    return bars, {
        "sha256": digest.hexdigest(), "bar_count": len(bars),
        "first_start": bars[0].start if bars else None,
        "last_start": bars[-1].start if bars else None,
    }


def split_segments(bars: Sequence[Bar], max_gap_hours: float) -> list[tuple[int, int]]:
    """各 segment の [開始 index, 終了 index) を返す。"""
    if not bars:
        return []
    starts = [0]
    starts.extend(i for i in range(1, len(bars))
                  if (bars[i].start - bars[i - 1].start).total_seconds() > max_gap_hours * 3600)
    return list(zip(starts, starts[1:] + [len(bars)], strict=True))


def feature_values(
    bars: Sequence[Bar], plan: Plan, segments: Sequence[tuple[int, int]],
) -> dict[str, list[float | None]]:
    values: dict[str, list[float | None]] = {f.id: [None] * len(bars) for f in plan.features}
    needs_atr = any(not isinstance(f, RangePosition) for f in plan.features)
    for start, end in segments:
        for t in range(start + plan.indicator_bars - 1, end):
            window = bars[t - plan.indicator_bars + 1:t + 1]
            atr_t = atr(window, plan.atr_period) if needs_atr else None
            emas: dict[int, list[float]] = {}
            for feature in plan.features:
                raw = None
                if isinstance(feature, RangePosition):
                    past = bars[t - feature.lookback:t]
                    high, low = max(b.high for b in past), min(b.low for b in past)
                    if high != low:
                        raw = float((2 * bars[t].close - high - low) / (high - low))
                elif atr_t is not None and atr_t > 0:
                    if isinstance(feature, MomentumAtr):
                        raw = float(bars[t].close - bars[t - feature.lookback].close) / atr_t
                    else:
                        if feature.ema_period not in emas:
                            emas[feature.ema_period] = ema_series(
                                [float(b.close) for b in window], feature.ema_period,
                            )
                        ema = emas[feature.ema_period]
                        if isinstance(feature, EmaSlopeAtr):
                            raw = (ema[-1] - ema[-1 - feature.slope_lookback]) / atr_t
                        else:
                            raw = (float(bars[t].close) - ema[-1]) / atr_t
                if raw is not None and not math.isfinite(raw):
                    raise ValueError(f"{feature.id}: 足 index {t} の特徴量が数値範囲を超えています")
                values[feature.id][t] = raw
    return values


def normalize(
    values: Sequence[float | None], window_size: int, segments: Sequence[tuple[int, int]],
) -> list[float | None]:
    scores: list[float | None] = [None] * len(values)
    for start, end in segments:
        window: deque[float] = deque(maxlen=window_size)
        for t in range(start, end):
            value = values[t]
            if value is None:
                continue
            if len(window) == window_size:
                deviation = pstdev(window)
                if deviation > 0:
                    scores[t] = (value - fmean(window)) / deviation
                    if not math.isfinite(scores[t]):
                        raise ValueError(f"足 index {t} の Z が数値範囲を超えています")
            window.append(value)
    return scores


@dataclass(frozen=True)
class Sample:
    index: int
    close_time: datetime
    z: float
    return_pips: float


def select_samples(
    bars: Sequence[Bar], scores: Sequence[float | None], horizon: int, period: Period,
    plan: Plan, segments: Sequence[tuple[int, int]],
) -> list[Sample]:
    samples: list[Sample] = []
    step = TIMEFRAME_SECONDS[plan.timeframe]
    for start, end in segments:
        for t in range(start, end - horizon):
            z = scores[t]
            if z is None or (samples and t - samples[-1].index < horizon):
                continue
            if (bars[t + horizon].start - bars[t].start).total_seconds() != horizon * step:
                continue
            if period.start <= bars[t].close_time and bars[t + horizon].close_time <= period.end:
                return_pips = float(
                    (bars[t + horizon].close - bars[t].close) / plan.instrument.pip_size,
                )
                if not math.isfinite(return_pips):
                    raise ValueError(f"足 index {t} の pips 換算が数値範囲を超えています")
                samples.append(Sample(
                    index=t, close_time=bars[t].close_time, z=z,
                    return_pips=return_pips,
                ))
    return samples


def ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for i in ordered[start:end]:
            result[i] = rank
        start = end
    return result


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) < 2:
        return None
    mean_x, mean_y = fmean(x), fmean(y)
    dx, dy = [v - mean_x for v in x], [v - mean_y for v in y]
    xx, yy = math.fsum(v * v for v in dx), math.fsum(v * v for v in dy)
    if xx == 0 or yy == 0:
        return None
    correlation = math.fsum(a * b for a, b in zip(dx, dy, strict=True)) / math.sqrt(xx * yy)
    return max(-1.0, min(1.0, correlation))


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    return pearson(ranks(x), ranks(y))


def critical_value(confidence: float, family_size: int) -> float | None:
    if family_size == 0:
        return None
    return NormalDist().inv_cdf(1 - (1 - confidence) / (2 * family_size))


def ic_interval(ic: float | None, n: int, z_crit: float | None) -> list[float] | None:
    if ic is None or n <= 3 or z_crit is None:
        return None
    if abs(ic) == 1:
        return [ic, ic]
    center = math.atanh(ic)
    width = z_crit * 1.06 / math.sqrt(n - 3)
    return [math.tanh(center - width), math.tanh(center + width)]


def sign(value: float) -> int:
    return (value > 0) - (value < 0)


def summarize(samples: Sequence[Sample], plan: Plan, z_crit: float | None) -> dict:
    zs = [s.z for s in samples]
    returns = [s.return_pips for s in samples]
    ic = spearman(zs, returns)
    sigma = pstdev(returns) if returns else None
    entries = [s for s in samples if abs(s.z) >= plan.entry_z]
    mean_abs_z = fmean(abs(s.z) for s in entries) if entries else None
    entry_mean = (fmean(sign(s.z) * sign(ic) * s.return_pips for s in entries)
                  if entries and ic is not None else None)
    cost = float(plan.round_trip_cost_pips)
    quantiles: list[dict] = []
    groups: list[list[Sample]] = [[] for _ in range(plan.quantiles)]
    for p, sample in enumerate(sorted(samples, key=lambda s: (s.z, s.close_time))):
        groups[p * plan.quantiles // len(samples)].append(sample)
    for q, group in enumerate(groups):
        quantiles.append({
            "quantile": q + 1, "n": len(group),
            "z_min": group[0].z if group else None, "z_max": group[-1].z if group else None,
            "mean_return_pips": fmean(s.return_pips for s in group) if group else None,
        })
    filled = [q for q in quantiles if q["n"]]
    staircase = spearman([q["quantile"] for q in filled],
                         [q["mean_return_pips"] for q in filled])
    by_month: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        by_month[sample.close_time.strftime("%Y-%m")].append(sample)
    months = []
    cumulative = 0.0
    for month, group in sorted(by_month.items()):
        if len(group) < plan.min_month_samples:
            continue
        month_ic = spearman([s.z for s in group], [s.return_pips for s in group])
        if month_ic is not None:
            cumulative += month_ic
        months.append({"month": month, "n": len(group), "ic": month_ic,
                       "cumulative_ic": cumulative})
    valid_months = [m["ic"] for m in months if m["ic"] is not None]
    month_mean = fmean(valid_months) if valid_months else None
    month_std = stdev(valid_months) if len(valid_months) > 1 else None
    return {
        "n": len(samples), "ic": ic, "pearson": pearson(zs, returns),
        "ic_interval": ic_interval(ic, len(samples), z_crit),
        "sigma_pips": sigma, "mean_return_pips": fmean(returns) if returns else None,
        "entry_n": len(entries), "mean_abs_z": mean_abs_z,
        "entry_mean_pips": entry_mean,
        "entry_net_pips": entry_mean - cost if entry_mean is not None else None,
        "break_even_ic": cost / (sigma * mean_abs_z) if sigma and mean_abs_z else None,
        "quantiles": quantiles, "staircase": staircase, "months": months,
        "month_count": len(valid_months), "month_ic_mean": month_mean,
        "month_ic_t": month_mean / (month_std / math.sqrt(len(valid_months)))
        if month_std else None,
        "month_sign_share": fmean(sign(v) == sign(ic) for v in valid_months)
        if valid_months and ic is not None else None,
    }


def verdict(stats: dict, plan: Plan, *, explore: dict | None = None) -> tuple[str, str]:
    if plan.basis == "simulated":
        return "synthetic_only", "合成データのため、統計は動作確認に限ります"
    if explore is not None and explore["status"] != "candidate":
        return "not_evaluated", "探索期間で candidate にならなかったため判定対象外です"
    if stats["n"] < plan.min_samples:
        return "insufficient", f'標本数 {stats["n"]} が min_samples {plan.min_samples} 未満です'
    if stats["entry_n"] == 0:
        return "insufficient", "entry_z を満たすエントリー標本がありません"
    undetected = "not_confirmed" if explore is not None else "not_detected"
    ic, interval = stats["ic"], stats["ic_interval"]
    if ic is None or interval is None:
        return undetected, "IC または信頼区間を算出できません"
    if explore is not None and sign(ic) != sign(explore["ic"]):
        return "not_confirmed", "IC の符号が探索期間と一致しません"
    if interval[0] <= 0 <= interval[1]:
        return undetected, "IC の信頼区間が 0 を含みます"
    if abs(ic) < stats["break_even_ic"]:
        return "below_cost", "IC の絶対値が損益分岐 IC を下回ります"
    if explore is None:
        if stats["month_count"] == 0:
            return "unstable", "IC を算出できる月がありません"
        if stats["staircase"] is None:
            return "unstable", "分位の階段の単調性を算出できません"
        if stats["staircase"] * sign(ic) < plan.staircase_min:
            return "unstable", "IC の符号で揃えた分位の階段が staircase_min を下回ります"
        if stats["month_sign_share"] < plan.month_sign_min:
            return "unstable", "月次 IC の符号一致率が month_sign_min を下回ります"
        return "candidate", "探索期間の検出・コスト・安定性の条件を満たしました"
    return "confirmed", "確認期間の符号・検出・コストの条件を満たしました"


def run(plan: Plan, path: Path) -> dict:
    bars, source = load_bars(path, plan)
    segments = split_segments(bars, plan.max_gap_hours)
    source["segments"] = [
        {"start_index": start, "end_index_exclusive": end, "bar_count": end - start,
         "first_start": bars[start].start, "last_start": bars[end - 1].start}
        for start, end in segments
    ]
    raw = feature_values(bars, plan, segments)
    family_size = {"explore": len(plan.features) * len(plan.horizons), "confirm": 0}
    z_crit = {"explore": critical_value(plan.confidence, family_size["explore"])}
    cells = []
    scores = {f.id: normalize(raw[f.id], plan.normalization_window, segments) for f in plan.features}
    for feature in plan.features:
        for horizon in plan.horizons:
            samples = select_samples(bars, scores[feature.id], horizon, plan.explore, plan, segments)
            stats = summarize(samples, plan, z_crit["explore"])
            stats["status"], stats["reason"] = verdict(stats, plan)
            cells.append({"feature_id": feature.id, "horizon": horizon, "explore": stats})
    family_size["confirm"] = sum(c["explore"]["status"] == "candidate" for c in cells)
    z_crit["confirm"] = critical_value(plan.confidence, family_size["confirm"])
    for cell in cells:
        samples = select_samples(bars, scores[cell["feature_id"]], cell["horizon"],
                                 plan.confirm, plan, segments)
        stats = summarize(samples, plan, z_crit["confirm"])
        stats["status"], stats["reason"] = verdict(stats, plan, explore=cell["explore"])
        cell["confirm"] = stats
    return {
        "schema_version": "feature_screen_report_v1", "profitability_established": False,
        "plan": plan.model_dump(mode="json"), "bars": source,
        "family_size": family_size, "z_crit": z_crit, "cells": cells,
        "ic_decay": [{"feature_id": f.id, "horizons": [
            {"horizon": c["horizon"], "explore": c["explore"]["ic"], "confirm": c["confirm"]["ic"]}
            for c in cells if c["feature_id"] == f.id
        ]} for f in plan.features],
    }


def markdown(report: dict) -> str:
    def fmt(value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# 特徴量スクリーン", "", "前提の選別用で、収益性・将来の再現性は示しません。", "",
             f'basis: `{report["plan"]["basis"]}`', ""]
    if report["plan"]["basis"] == "simulated":
        lines.extend(["合成データの動作確認です。全セルの判定は synthetic_only です。", ""])
    lines.extend(["時刻は broker ラベル軸です。CSV に実観測時刻はなく、known_at は復元していません。",
                  "算出できない統計は —（JSON では null）です。", ""])
    for period, label in (("explore", "探索"), ("confirm", "確認")):
        bounds = report["plan"][period]
        lines.extend([f"## {label}期間", "", f'{bounds["start"]} 〜 {bounds["end"]}', "",
                      (f'Bonferroni 族: {report["family_size"][period]}、'
                       f'z_crit: {fmt(report["z_crit"][period])}'), "",
                      "| 特徴量 | horizon | n | IC | 信頼区間 | 損益分岐 IC | staircase | 月次符号一致率 | 判定 | 理由 |",
                      "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
        for cell in report["cells"]:
            stats = cell[period]
            interval = stats["ic_interval"]
            ci = " 〜 ".join(fmt(v) for v in interval) if interval is not None else "—"
            values = [cell["feature_id"], cell["horizon"], stats["n"], stats["ic"], ci,
                      stats["break_even_ic"], stats["staircase"], stats["month_sign_share"],
                      stats["status"], stats["reason"]]
            lines.append("| " + " | ".join(fmt(v) for v in values) + " |")
        lines.append("")
    lines.extend(["## IC 減衰", "", "| 特徴量 | horizon | 探索 IC | 確認 IC |",
                  "| --- | --- | --- | --- |"])
    for feature in report["ic_decay"]:
        for row in feature["horizons"]:
            lines.append("| " + " | ".join(fmt(v) for v in (
                feature["feature_id"], row["horizon"], row["explore"], row["confirm"],
            )) + " |")
    lines.extend(["", "## 分位", "", "| 特徴量 | horizon | 期間 | 分位 | n | Z 最小 | Z 最大 | 平均 pips |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"])
    for cell in report["cells"]:
        for period, label in (("explore", "探索"), ("confirm", "確認")):
            for q in cell[period]["quantiles"]:
                lines.append("| " + " | ".join(fmt(v) for v in (
                    cell["feature_id"], cell["horizon"], label, q["quantile"], q["n"],
                    q["z_min"], q["z_max"], q["mean_return_pips"],
                )) + " |")
    lines.extend(["", "信頼区間は Spearman IC の Fisher 変換に 1.06 を掛けた近似です。",
                  "確認の族が 0 のときは補正区間を算出せず、確認判定を行いません。",
                  "入力 hash・月次 IC と累積和・その他の統計は report.json に保存しています。"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw_plan = args.plan.read_bytes()
        plan = Plan.model_validate_json(raw_plan)
        report = run(plan, args.bars)
        report["plan_sha256"] = hashlib.sha256(raw_plan).hexdigest()
        report["git"] = git_state()
        serialized = json.dumps(report, default=str, ensure_ascii=False, indent=2, allow_nan=False)
        rendered = markdown(report)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "plan.json").write_bytes(raw_plan)
        (args.output_dir / "report.json").write_text(serialized + "\n", encoding="utf-8")
        (args.output_dir / "report.md").write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, ValidationError) as error:
        print(f"入力または出力エラー: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
