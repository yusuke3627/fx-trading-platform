"""Compare research runs with and without one strategy confirmation leg.

    python -m trading.backtest.ablation_compare \
        --with reports/h5_with/<run_id> \
        --without reports/h5_without/<run_id> \
        --seed 42
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading.backtest.policy_event_study import (
    BOOTSTRAP_LEVEL,
    BOOTSTRAP_SAMPLES,
    bootstrap_interval,
)

KEEP = "確認レッグを維持（寄与あり）"
REMOVE = "確認レッグを外す（絞るだけで質が上がらない）"
UNDECIDED_SAMPLE = "判定不能（標本不足）。維持したまま再測定"
UNDECIDED_DIFFERENCE = "判定不能（差が検出できない）。維持したまま再測定"
MIN_TRADES = 10

MANIFEST_FIELDS = (
    "run_id",
    "git_commit",
    "git_dirty",
    "dataset_hash",
    "feature_dataset_hash",
    "period_from",
    "period_to",
    "param_overrides",
)


@dataclass(frozen=True)
class RunArtifacts:
    manifest: dict
    metrics: dict[str, str]
    pnls: list[Decimal]


@dataclass(frozen=True)
class ArmSummary:
    count: int
    total: Decimal
    mean: Decimal
    hit_rate: float
    max_drawdown: Decimal
    low: float
    high: float


def load_run(run_dir: Path) -> RunArtifacts:
    """Load the three artifacts that define one comparison arm."""
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.json"
    trades_path = run_dir / "trades.csv"
    for path in (manifest_path, summary_path, trades_path):
        if not path.is_file():
            raise SystemExit(f"required run artifact is missing: {path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    with trades_path.open(newline="", encoding="utf-8") as source:
        pnls = [Decimal(row["net_pnl"]) for row in csv.DictReader(source)]
    return RunArtifacts(manifest=manifest, metrics=summary["metrics"], pnls=pnls)


def arm_summary(
    pnls: Sequence[Decimal], max_drawdown: Decimal, seed: int
) -> ArmSummary:
    count = len(pnls)
    total = sum(pnls, Decimal(0))
    mean = total / count if count else Decimal("NaN")
    hit_rate = sum(pnl > 0 for pnl in pnls) / count if count else float("nan")
    low, high = bootstrap_interval([float(pnl) for pnl in pnls], seed)
    return ArmSummary(
        count=count,
        total=total,
        mean=mean,
        hit_rate=hit_rate,
        max_drawdown=max_drawdown,
        low=low,
        high=high,
    )


def difference_interval(
    with_pnls: Sequence[float], without_pnls: Sequence[float], seed: int
) -> tuple[float, float]:
    """CI90 for independent resamples of with-minus-without mean PnL."""
    if len(with_pnls) < 2 or len(without_pnls) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    differences = sorted(
        statistics.fmean(rng.choices(with_pnls, k=len(with_pnls)))
        - statistics.fmean(rng.choices(without_pnls, k=len(without_pnls)))
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    tail = (1.0 - BOOTSTRAP_LEVEL) / 2.0
    low = differences[int(tail * (len(differences) - 1))]
    high = differences[int((1.0 - tail) * (len(differences) - 1))]
    return (low, high)


def judge(
    with_: ArmSummary,
    without: ArmSummary,
    difference: tuple[float, float],
) -> str:
    """Apply the pre-registered decision table from top to bottom."""
    low, _ = difference
    if with_.count > 0 and without.count > 0:
        if with_.mean > without.mean and low > 0:
            return KEEP
        if without.count >= 2 * with_.count and without.mean >= with_.mean:
            return REMOVE
    if with_.count < MIN_TRADES or without.count < MIN_TRADES:
        return UNDECIDED_SAMPLE
    return UNDECIDED_DIFFERENCE


def _format_float(value: float) -> str:
    return f"{value:.6g}"


def _manifest_value(manifest: dict, field: str) -> str:
    if field == "param_overrides":
        return json.dumps(manifest.get(field, {}), ensure_ascii=False, sort_keys=True)
    return str(manifest.get(field))


def report(with_: RunArtifacts, without: RunArtifacts, seed: int) -> str:
    with_summary = arm_summary(
        with_.pnls, Decimal(with_.metrics["max_drawdown"]), seed
    )
    without_summary = arm_summary(
        without.pnls, Decimal(without.metrics["max_drawdown"]), seed
    )
    difference = difference_interval(
        [float(pnl) for pnl in with_.pnls],
        [float(pnl) for pnl in without.pnls],
        seed,
    )

    lines: list[str] = []
    for label, run in (("with", with_), ("without", without)):
        lines.append(f"{label}:")
        lines.extend(
            f"  {field:<22} {_manifest_value(run.manifest, field)}"
            for field in MANIFEST_FIELDS
        )
    lines.extend(
        [
            "",
            f"{'metric':<28} {'with':>22} {'without':>22}",
            f"{'trades':<28} {with_summary.count:>22} {without_summary.count:>22}",
            f"{'net_pnl_total':<28} {with_summary.total!s:>22} {without_summary.total!s:>22}",
            f"{'expectancy(mean)':<28} {with_summary.mean!s:>22} {without_summary.mean!s:>22}",
            f"{'hit_rate':<28} {_format_float(with_summary.hit_rate):>22} {_format_float(without_summary.hit_rate):>22}",
            f"{'max_drawdown':<28} {with_summary.max_drawdown!s:>22} {without_summary.max_drawdown!s:>22}",
            (
                f"{'mean CI90 [low, high]':<28} "
                f"{f'[{_format_float(with_summary.low)}, {_format_float(with_summary.high)}]':>22} "
                f"{f'[{_format_float(without_summary.low)}, {_format_float(without_summary.high)}]':>22}"
            ),
            "",
            (
                "difference of means (with - without): "
                f"{with_summary.mean - without_summary.mean} "
                f"CI90 [{_format_float(difference[0])}, {_format_float(difference[1])}] "
                f"seed={seed}"
            ),
            f"verdict: {judge(with_summary, without_summary, difference)}",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="compare two research ablation runs")
    parser.add_argument("--with", dest="with_run", type=Path, required=True)
    parser.add_argument("--without", dest="without_run", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(report(load_run(args.with_run), load_run(args.without_run), args.seed))


if __name__ == "__main__":
    main()
