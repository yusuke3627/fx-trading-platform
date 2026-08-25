"""Vertical-slice backtest CLI.

    python -m trading.backtest.run --env backtest --ticks 5000 --scenario normal

Runs the scripted probe strategy over a seeded synthetic bid/ask series and
writes a report directory. This is the reproducibility harness, not a
research result: real research runs arrive with recorded tick archives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from trading.backtest.costs import STRESS_SCENARIOS
from trading.backtest.data import dataset_hash, synthetic_ticks
from trading.backtest.engine import ENGINE_VERSION, BacktestEngine, ScriptedStrategy
from trading.backtest.report import write_report
from trading.config import load_config
from trading.data.policy.risk_windows import central_bank_calendar
from trading.domain.instrument import FillingMode, InstrumentSpec
from trading.domain.position import PositionDirection
from trading.strategy.base import StrategyConfig

# Dataset identity is fixed: changing it must change the dataset hash, never
# silently reuse the old one.
DATASET_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def synthetic_usdjpy_spec(symbol: str) -> InstrumentSpec:
    """Instrument definition of the synthetic dataset (per-dataset input,
    not a broker hardcode: recorded datasets carry the broker's spec)."""
    return InstrumentSpec(
        symbol=symbol,
        digits=3,
        pip_size=Decimal("0.01"),
        contract_size=Decimal(1000),
        volume_min=Decimal(1000),
        volume_step=Decimal(1000),
        volume_max=Decimal(1_000_000),
        stop_level_points=0,
        accepted_filling_modes=frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    )


def git_state() -> dict:
    """Reproduction inputs: commit, and — because uncommitted changes make a
    commit hash alone unreproducible — a dirty flag plus a hash of the
    working-tree delta when dirty."""

    def _run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout

    try:
        commit = _run("rev-parse", "HEAD").strip()
        status = _run("status", "--porcelain")
        state = {"git_commit": commit, "git_dirty": bool(status.strip())}
        if state["git_dirty"]:
            digest = hashlib.sha256((status + _run("diff", "HEAD")).encode())
            # `diff HEAD` excludes untracked files, so their CONTENT goes
            # into the hash too — same paths with different contents must
            # never produce the same reproduction hash.
            untracked = _run("ls-files", "--others", "--exclude-standard")
            for name in sorted(untracked.splitlines()):
                digest.update(name.encode())
                digest.update(Path(name).read_bytes())
            state["git_diff_sha256"] = digest.hexdigest()
        return state
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"git_commit": "unknown", "git_dirty": None}


def main() -> None:
    parser = argparse.ArgumentParser(description="vertical-slice backtest")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--ticks", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--scenario", default=None, choices=sorted(STRESS_SCENARIOS))
    parser.add_argument("--out", default="reports")
    args = parser.parse_args()

    config = load_config(args.env)
    seed = args.seed if args.seed is not None else config.simulator.seed
    scenario = args.scenario or config.simulator.scenario
    # The configured latency must actually reach the simulator: the manifest
    # hashes the config, so a run must not LOOK latency-adjusted without
    # being it.
    costs = replace(STRESS_SCENARIOS[scenario], latency_ms=config.simulator.latency_ms)
    symbol = config.market.primary_instruments[0]
    spec = synthetic_usdjpy_spec(symbol)

    ticks = synthetic_ticks(spec=spec, start=DATASET_START, count=args.ticks, seed=seed)
    # LONG early, flip to SHORT past the middle: exercises OPEN, the
    # ticket-referenced CLOSE, the reversal OPEN and (path-dependent)
    # protection fills in one run.
    plan = {
        args.ticks // 10: PositionDirection.LONG,
        (args.ticks * 6) // 10: PositionDirection.SHORT,
    }
    engine = BacktestEngine(
        risk_config=config.risk,
        spec=spec,
        costs=costs,
        seed=seed,
        strategy_factory=lambda: ScriptedStrategy(plan),
        strategy_config=StrategyConfig(
            strategy_id=ScriptedStrategy.strategy_id,
            enabled=True,
            instruments=[symbol],
        ),
        # The same calendar live grades against. A replay that ignored it would
        # let a strategy trade through a decision the live path refuses.
        event_risk=central_bank_calendar(config),
    )
    result = engine.run(ticks)

    manifest = {
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        **git_state(),
        "environment": args.env,
        "strategy_id": ScriptedStrategy.strategy_id,
        "strategy_version": ScriptedStrategy.strategy_version,
        "engine_version": ENGINE_VERSION,
        "scenario": scenario,
        "seed": seed,
        "tick_count": args.ticks,
        "dataset_start": DATASET_START.isoformat(),
        "dataset_hash": dataset_hash(ticks),
        "config_sha256": hashlib.sha256(
            config.model_dump_json().encode()
        ).hexdigest(),
        "python_version": sys.version.split()[0],
    }
    run_dir = write_report(result, manifest, Path(args.out))

    print(json.dumps({"run_dir": str(run_dir), **result.metrics}, indent=2))


if __name__ == "__main__":
    main()
