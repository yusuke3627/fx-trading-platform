"""Compare two research runs that differ only in one ablated parameter.

    python -m trading.backtest.ablation_compare \
        --with reports/h5_with/<run_id> \
        --without reports/h5_without/<run_id> \
        --param macro_confirmation_enabled \
        --seed 42

真偽値以外のパラメータも比較でき、腕ごとの期待値は --with-value / --without-value で指定する。
省略した場合の期待値はそれぞれ True / False。

This CLI's --seed controls only bootstrap resampling. Research seeds drive
execution shocks derived from stable per-order keys, so an extra ablation-leg
fill does not shift later shared orders. Orders whose identity changes, such as
their timestamp or side, receive different shocks.

Intervals resampling whole broker business days are shown for reference.
Judgments use the pre-registered i.i.d. intervals; adopting block intervals for
decisions requires an updated pre-registration.

未値付けの swap がある腕の損益・区間は参考値とし、その腕を含む判定を保留する。
これは carry 込み損益の欠損に対する共通条件で、個別の事前登録の採否条件とは別に適用する。
単腕の主判定でも ArmSummary.hold_reason を先に確認する。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trading.backtest.policy_event_study import (
    BOOTSTRAP_LEVEL,
    BOOTSTRAP_SAMPLES,
    bootstrap_interval,
)
from trading.backtest.research import parse_param_value
from trading.backtest.run_coverage import run_coverage
from trading.strategy.parameters import ParamValue

KEEP = "有効のまま維持（寄与あり）"
REMOVE = "無効にする（絞るだけで質が上がらない）"
UNDECIDED_SAMPLE = "判定不能（標本不足）。維持したまま再測定"
UNDECIDED_DIFFERENCE = "判定不能（差が検出できない）。維持したまま再測定"
DEFERRED_SWAP = "保留（未値付けの swap あり）。値付け後に再判定"
MIN_TRADES = 10
# H4 の時間切れ決済 ablation は有効側に 25 か月中 15 か月（60%）の末尾空白があり、
# H5 は両腕とも最終月まで建玉があった。その間を分ける比較基準として 10% を採る。
# 最後の建玉から period_to までの空白で比較を制限し、停止原因は断定しない。
TRAILING_BLACKOUT_MAX_RATIO = 0.1

COMPARABLE_FIELDS = (
    "git_commit",
    "git_dirty",
    "git_diff_sha256",
    "python_version",
    "environment",
    "symbol",
    "strategy_id",
    "strategy_version",
    "engine_version",
    "scenario",
    "seed",
    "tick_count",
    "period_from",
    "period_to",
    "warmup_days",
    "broker_server_ahead_of_ny_hours",
    "dataset_hash",
    "feature_dataset_hash",
    "swap_dataset_hash",
)

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
    entry_ats: list[datetime]


@dataclass(frozen=True)
class ArmSummary:
    count: int
    total: Decimal
    mean: Decimal
    hit_rate: float
    max_drawdown: Decimal
    low: float
    high: float
    block_low: float
    block_high: float
    blocks: int
    unpriced_rollovers: int

    @property
    def hold_reason(self) -> str | None:
        return DEFERRED_SWAP if self.unpriced_rollovers > 0 else None


def load_run(run_dir: Path) -> RunArtifacts:
    """Load carry-inclusive PnL aggregated per entry, including partial closes.

    ADR-016 requires overnight swap in the distribution; omitting it would
    overstate the expectancy of an arm that holds across rollover.
    """
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.json"
    trades_path = run_dir / "trades.csv"
    for path in (manifest_path, summary_path, trades_path):
        if not path.is_file():
            raise SystemExit(f"required run artifact is missing: {path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    pnls_by_entry: dict[str, Decimal] = {}
    entry_ats_by_entry: dict[str, datetime] = {}
    row_count = 0
    with trades_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if "entry_id" not in (reader.fieldnames or []):
            raise SystemExit("trades.csv has no entry_id; re-run with the current engine")
        if "entry_at" not in (reader.fieldnames or []):
            raise SystemExit("trades.csv has no entry_at; re-run with the current engine")
        for row in reader:
            entry_id = row["entry_id"]
            if not entry_id:
                raise SystemExit("trades.csv contains an empty entry_id")
            row_count += 1
            entry_ats_by_entry.setdefault(entry_id, datetime.fromisoformat(row["entry_at"]))
            pnls_by_entry[entry_id] = (
                pnls_by_entry.get(entry_id, Decimal(0))
                + Decimal(row["net_pnl"]) + Decimal(row["carry"])
            )
    summary_trades = int(summary["metrics"]["trades"])
    if row_count != summary_trades:
        raise SystemExit(
            f"trade count mismatch for {trades_path}: "
            f"actual rows={row_count}, summary metrics.trades={summary_trades}"
        )
    return RunArtifacts(
        manifest=manifest, metrics=summary["metrics"], pnls=list(pnls_by_entry.values()),
        entry_ats=list(entry_ats_by_entry.values()),
    )


def _period(manifest: dict) -> tuple[datetime, datetime] | None:
    if "period_from" not in manifest or "period_to" not in manifest:
        return None
    return (
        datetime.fromisoformat(manifest["period_from"]),
        datetime.fromisoformat(manifest["period_to"]),
    )


def _matches_param_value(actual: object, expected: ParamValue) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    return type(actual) is type(expected) and actual == expected


def verify_comparable(
    with_: RunArtifacts,
    without: RunArtifacts,
    param: str,
    *,
    with_value: ParamValue = True,
    without_value: ParamValue = False,
) -> None:
    """Require identical reproduction inputs outside the ablated parameter.

    config_sha256 is excluded because the parameter override necessarily changes
    it. created_at and run_id identify executions rather than comparable inputs.
    The engine marks open positions instead of force-closing them, so a run that
    ends with a position has a right-censored trades.csv and is not comparable.
    A command still in flight at the end drops its candidate from the sample the
    same way.
    """
    if _matches_param_value(with_value, without_value):
        raise SystemExit(
            f"runs are not comparable: {param} の期待値が両腕で同じです: "
            f"with={with_value!r}, without={without_value!r}"
        )
    reasons = []
    for field in COMPARABLE_FIELDS:
        with_field_value = with_.manifest.get(field)
        without_field_value = without.manifest.get(field)
        if with_field_value != without_field_value:
            reasons.append(
                f"{field}: with={with_field_value!r}, without={without_field_value!r}"
            )

    coverage_details = {
        "with": "with coverage unavailable",
        "without": "without coverage unavailable",
    }
    trailing_blackout_reasons = []
    for arm, run, expected in (("with", with_, with_value), ("without", without, without_value)):
        git_commit = run.manifest.get("git_commit")
        if not git_commit or git_commit == "unknown":
            reasons.append(
                f"{arm} git_commit={git_commit!r}; the run has no reproducible "
                "source state (git unavailable or run outside the repository)"
            )
        resolved = run.manifest.get("resolved_parameters", {})
        resolved_value = resolved.get(param)
        if not _matches_param_value(resolved_value, expected):
            reasons.append(
                f"{arm} resolved_parameters.{param} must be "
                f"{expected!r}, got {resolved_value!r}; "
                "instrument-specific parameters override defaults"
            )
        open_positions = run.metrics.get("open_positions_at_end")
        if open_positions != "0":
            reasons.append(
                f"{arm} open_positions_at_end={open_positions!r}; "
                "re-run with a period end where the book is flat"
            )
        pending = run.metrics.get("pending_commands_at_end")
        if pending != "0":
            reasons.append(
                f"{arm} pending_commands_at_end={pending!r}; "
                "re-run with a period end where no command is in flight"
            )
        period = _period(run.manifest)
        if period is None:
            reasons.append(
                f"{arm} manifest has no period_from/period_to; "
                "re-run with the current engine"
            )
            continue
        period_from, period_to = period
        outside_period = [
            at for at in run.entry_ats if not period_from <= at < period_to
        ]
        if outside_period:
            reasons.append(
                f"{arm} has {len(outside_period)} entry_at outside "
                f"[{period_from.isoformat()}, {period_to.isoformat()}): "
                f"first={outside_period[0].isoformat()}"
            )
            continue
        coverage = run_coverage(run.entry_ats, period_from, period_to)
        first_at = coverage.first_trade_at
        last_at = coverage.last_trade_at
        coverage_details[arm] = (
            f"{arm} first_trade_at={first_at.isoformat() if first_at else None} "
            f"last_trade_at={last_at.isoformat() if last_at else None} "
            f"empty_months={len(coverage.empty_months)}/{coverage.months_in_period}"
        )
        if (
            last_at is not None
            and period_to - last_at > TRAILING_BLACKOUT_MAX_RATIO * (period_to - period_from)
        ):
            ratio = (period_to - last_at) / (period_to - period_from)
            trailing_blackout_reasons.append(
                f"{arm} last_trade_at={last_at.isoformat()} is "
                f"{coverage.trailing_blackout_days:.1f} days before "
                f"period_to={period_to.isoformat()} "
                f"({ratio:.1%} of the period, limit {TRAILING_BLACKOUT_MAX_RATIO:.0%}); "
                "the trailing gap exceeds the comparison limit."
            )

    with_overrides = dict(with_.manifest.get("param_overrides", {}))
    without_overrides = dict(without.manifest.get("param_overrides", {}))
    if (
        param in with_overrides
        and not _matches_param_value(with_overrides[param], with_value)
    ):
        reasons.append(
            f"with param_overrides.{param} must be omitted or {with_value!r}"
        )
    if not _matches_param_value(without_overrides.get(param), without_value):
        reasons.append(f"without param_overrides.{param} must be {without_value!r}")

    with_overrides.pop(param, None)
    without_overrides.pop(param, None)
    if with_overrides != without_overrides:
        reasons.append(
            "non-ablation param_overrides differ: "
            f"with={with_overrides!r}, without={without_overrides!r}"
        )

    reasons.extend(
        f"{reason} coverage: {coverage_details['with']}; {coverage_details['without']}"
        for reason in trailing_blackout_reasons
    )
    if reasons:
        raise SystemExit("runs are not comparable:\n- " + "\n- ".join(reasons))


def arm_summary(
    pnls: Sequence[Decimal], blocks: Sequence[date], max_drawdown: Decimal, seed: int,
    *, unpriced_rollovers: int,
) -> ArmSummary:
    count = len(pnls)
    total = sum(pnls, Decimal(0))
    mean = total / count if count else Decimal("NaN")
    hit_rate = sum(pnl > 0 for pnl in pnls) / count if count else float("nan")
    low, high = bootstrap_interval([float(pnl) for pnl in pnls], seed)
    block_low, block_high = block_bootstrap_interval([float(pnl) for pnl in pnls], blocks, seed)
    return ArmSummary(
        count=count,
        total=total,
        mean=mean,
        hit_rate=hit_rate,
        max_drawdown=max_drawdown,
        low=low,
        high=high,
        block_low=block_low,
        block_high=block_high,
        blocks=len(set(blocks)),
        unpriced_rollovers=unpriced_rollovers,
    )


def broker_day(label: datetime) -> date:
    """entry_at は broker ラベル軸なので、その暦日が営業日（NY 17:00 = 深夜）になる。"""
    return label.astimezone(UTC).date()


def _daily_pnls(pnls: Sequence[float], blocks: Sequence[date]) -> list[list[float]]:
    by_day: dict[date, list[float]] = {}
    for pnl, day in zip(pnls, blocks, strict=True):
        by_day.setdefault(day, []).append(pnl)
    return [by_day[day] for day in sorted(by_day)]


def block_bootstrap_interval(
    pnls: Sequence[float], blocks: Sequence[date], seed: int
) -> tuple[float, float]:
    daily_pnls = _daily_pnls(pnls, blocks)
    if len(daily_pnls) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(
            pnl for day in rng.choices(daily_pnls, k=len(daily_pnls)) for pnl in day
        )
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    tail = (1.0 - BOOTSTRAP_LEVEL) / 2.0
    low = means[int(tail * (len(means) - 1))]
    high = means[int((1.0 - tail) * (len(means) - 1))]
    return (low, high)


def block_difference_interval(
    with_pnls: Sequence[float],
    with_blocks: Sequence[date],
    without_pnls: Sequence[float],
    without_blocks: Sequence[date],
    seed: int,
) -> tuple[float, float]:
    with_days = _daily_pnls(with_pnls, with_blocks)
    without_days = _daily_pnls(without_pnls, without_blocks)
    if len(with_days) < 2 or len(without_days) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    differences = sorted(
        statistics.fmean(pnl for day in rng.choices(with_days, k=len(with_days)) for pnl in day)
        - statistics.fmean(
            pnl for day in rng.choices(without_days, k=len(without_days)) for pnl in day
        )
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    tail = (1.0 - BOOTSTRAP_LEVEL) / 2.0
    low = differences[int(tail * (len(differences) - 1))]
    high = differences[int((1.0 - tail) * (len(differences) - 1))]
    return (low, high)


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
    """Defer incomplete carry before applying the pre-registered decision table."""
    if with_.hold_reason or without.hold_reason:
        return DEFERRED_SWAP
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


def report(
    with_: RunArtifacts,
    without: RunArtifacts,
    param: str,
    seed: int,
    *,
    with_value: ParamValue = True,
    without_value: ParamValue = False,
) -> str:
    verify_comparable(with_, without, param, with_value=with_value, without_value=without_value)
    with_blocks = [broker_day(at) for at in with_.entry_ats]
    without_blocks = [broker_day(at) for at in without.entry_ats]
    with_summary = arm_summary(
        with_.pnls, with_blocks, Decimal(with_.metrics["max_drawdown"]), seed,
        unpriced_rollovers=int(with_.metrics["unpriced_rollovers"]),
    )
    without_summary = arm_summary(
        without.pnls, without_blocks, Decimal(without.metrics["max_drawdown"]), seed,
        unpriced_rollovers=int(without.metrics["unpriced_rollovers"]),
    )
    difference = difference_interval(
        [float(pnl) for pnl in with_.pnls],
        [float(pnl) for pnl in without.pnls],
        seed,
    )
    block_difference = block_difference_interval(
        [float(pnl) for pnl in with_.pnls], with_blocks,
        [float(pnl) for pnl in without.pnls], without_blocks,
        seed,
    )

    lines: list[str] = [f"比較: {param} with={with_value!r} → without={without_value!r}"]
    for label, run, summary in (
        ("with", with_, with_summary), ("without", without, without_summary),
    ):
        lines.append(f"{label}:")
        lines.extend(
            f"  {field:<22} {_manifest_value(run.manifest, field)}"
            for field in MANIFEST_FIELDS
        )
        if summary.hold_reason:
            lines.append(
                f"  {'hold_reason':<22} {summary.hold_reason}。この腕の損益・区間は参考値"
            )
        period_from, period_to = _period(run.manifest)
        coverage = run_coverage(run.entry_ats, period_from, period_to)
        lines.extend(
            f"  {field:<22} {value}"
            for field, value in (
                (
                    "first_trade_at",
                    coverage.first_trade_at.isoformat() if coverage.first_trade_at else None,
                ),
                (
                    "last_trade_at",
                    coverage.last_trade_at.isoformat() if coverage.last_trade_at else None,
                ),
                (
                    "months_with_trades",
                    f"{coverage.months_with_trades}/{coverage.months_in_period}",
                ),
            )
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
            f"{'carry_total':<28} {with_.metrics['carry_total']:>22} {without.metrics['carry_total']:>22}",
            f"{'unpriced_rollovers':<28} {with_.metrics['unpriced_rollovers']:>22} {without.metrics['unpriced_rollovers']:>22}",
            (
                f"{'mean CI90 [low, high]':<28} "
                f"{f'[{_format_float(with_summary.low)}, {_format_float(with_summary.high)}]':>22} "
                f"{f'[{_format_float(without_summary.low)}, {_format_float(without_summary.high)}]':>22}"
            ),
            f"{'blocks':<28} {with_summary.blocks:>22} {without_summary.blocks:>22}",
            (
                f"{'expectancy_ci90_block':<28} "
                f"{f'[{_format_float(with_summary.block_low)}, {_format_float(with_summary.block_high)}]':>22} "
                f"{f'[{_format_float(without_summary.block_low)}, {_format_float(without_summary.block_high)}]':>22}"
            ),
            "",
            (
                "difference of means (with - without): "
                f"{with_summary.mean - without_summary.mean} "
                f"CI90 [{_format_float(difference[0])}, {_format_float(difference[1])}] "
                f"seed={seed}"
            ),
            (
                "difference of means (with - without) block "
                f"CI90 [{_format_float(block_difference[0])}, {_format_float(block_difference[1])}] "
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
    parser.add_argument(
        "--param", required=True,
        help="比較するパラメータ名（真偽値以外も指定可。既定: with=True, without=False）",
    )
    parser.add_argument(
        "--with-value", type=parse_param_value, default=True,
        help="with 側の期待値（既定: True）",
    )
    parser.add_argument(
        "--without-value", type=parse_param_value, default=False,
        help="without 側の期待値（既定: False）",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(report(
        load_run(args.with_run), load_run(args.without_run), args.param, args.seed,
        with_value=args.with_value, without_value=args.without_value,
    ))


if __name__ == "__main__":
    main()
