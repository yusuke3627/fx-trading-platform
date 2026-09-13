"""Backtest run artifacts.

PostgreSQL becomes the source of truth once the DB is wired in; until then
every run still writes a human-readable directory:

    reports/<run_id>/
        manifest.json   -- reproduction inputs (commit, config, dataset, seed)
        summary.json    -- metrics + rejection counts
        trades.json     -- fill-by-fill record
        trades.csv      -- round-trip record (one row per closed quantity)
        equity.json     -- equity curve

Coverage describes entry times in closed trade records.
Risk rejections record all failed codes per decision, so codes can co-occur, e.g.
minimum-lot violations when both position-count and quantity limits are reached.
Do not interpret the sum of code counts as independent rejection reasons.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from trading.backtest.engine import BacktestResult
from trading.backtest.run_coverage import run_coverage


def write_report(result: BacktestResult, manifest: dict, out_dir: Path) -> Path:
    run_dir = out_dir / str(manifest["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "manifest.json").write_text(_dumps(manifest), encoding="utf-8")
    summary = {
        "symbol": result.symbol,
        "metrics": result.metrics,
        "risk_rejections": [
            {"at": at.isoformat(), "codes": list(codes)}
            for at, codes in result.risk_rejections
        ],
    }
    if "period_from" in manifest and "period_to" in manifest:
        coverage = run_coverage(
            (trade.entry_at for trade in result.trades),
            datetime.fromisoformat(manifest["period_from"]),
            datetime.fromisoformat(manifest["period_to"]),
        )
        summary["coverage"] = {
            "first_trade_at": (
                coverage.first_trade_at.isoformat() if coverage.first_trade_at else None
            ),
            "last_trade_at": (
                coverage.last_trade_at.isoformat() if coverage.last_trade_at else None
            ),
            "months_with_trades": coverage.months_with_trades,
            "months_in_period": coverage.months_in_period,
            "empty_months": list(coverage.empty_months),
            "trailing_blackout_days": coverage.trailing_blackout_days,
        }
    (run_dir / "summary.json").write_text(
        _dumps(summary),
        encoding="utf-8",
    )
    (run_dir / "trades.json").write_text(
        _dumps([_jsonable(asdict(f)) for f in result.fills]), encoding="utf-8"
    )
    with (run_dir / "trades.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(
            [
                "entry_id",
                "strategy_id",
                "symbol",
                "entry_at",
                "exit_at",
                "direction",
                "quantity",
                "entry_price",
                "exit_price",
                "net_pnl",
                "carry",
                "reason",
            ]
        )
        writer.writerows(
            [
                trade.entry_id,
                trade.strategy_id,
                trade.symbol,
                trade.entry_at.isoformat(),
                trade.exit_at.isoformat(),
                trade.direction,
                str(trade.quantity),
                str(trade.entry_price),
                str(trade.exit_price),
                str(trade.net_pnl),
                str(trade.carry),
                trade.reason,
            ]
            for trade in result.trades
        )
    (run_dir / "equity.json").write_text(
        _dumps([[at.isoformat(), str(equity)] for at, equity in result.equity_curve]),
        encoding="utf-8",
    )
    return run_dir


def _jsonable(mapping: dict) -> dict:
    return {k: _scalar(v) for k, v in mapping.items()}


def _scalar(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _dumps(payload) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
