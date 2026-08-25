"""Backtest run artifacts.

PostgreSQL becomes the source of truth once the DB is wired in; until then
every run still writes a human-readable directory:

    reports/<run_id>/
        manifest.json   -- reproduction inputs (commit, config, dataset, seed)
        summary.json    -- metrics + rejection counts
        trades.json     -- fill-by-fill record
        equity.json     -- equity curve
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from trading.backtest.engine import BacktestResult


def write_report(result: BacktestResult, manifest: dict, out_dir: Path) -> Path:
    run_dir = out_dir / str(manifest["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "manifest.json").write_text(_dumps(manifest), encoding="utf-8")
    (run_dir / "summary.json").write_text(
        _dumps(
            {
                "symbol": result.symbol,
                "metrics": result.metrics,
                "risk_rejections": [
                    {"at": at.isoformat(), "codes": list(codes)}
                    for at, codes in result.risk_rejections
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "trades.json").write_text(
        _dumps([_jsonable(asdict(f)) for f in result.fills]), encoding="utf-8"
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
