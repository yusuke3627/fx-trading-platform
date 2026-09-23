"""並列制御の検証用。DB に接続せず、固定した小さな研究成果物を出力する。"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--wait-for", type=Path)
parser.add_argument("--exit-code", type=int, default=0)
parser.add_argument("--fingerprint", default="a")
parser.add_argument("--invalid-metrics", action="store_true")
parser.add_argument("--ignore-term", action="store_true")
args = parser.parse_args()
if args.ignore_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
plan = json.loads((args.out.parent / "plan.json").read_text(encoding="utf-8"))
trial = next(trial for trial in plan["trials"] if trial["out"] == str(args.out))
assert os.environ[plan["config"]["storage"]["dsn_env"]] == "synthetic-only"
assert json.loads((args.out.parent / "results.json").read_text(encoding="utf-8"))["trials"][
    plan["trials"].index(trial)
]["status"] == "running"
(args.out / "started.tmp").write_text(json.dumps({"pid": os.getpid(), "time": time.perf_counter_ns()}), encoding="utf-8")
(args.out / "started.tmp").replace(args.out / "started.json")
if args.wait_for:
    deadline = time.monotonic() + 30
    while not args.wait_for.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"fixture was not released: {args.wait_for}")
        time.sleep(0.01)
if args.exit_code:
    print("synthetic failure", file=sys.stderr)
else:
    run_dir = args.out / "run"
    run_dir.mkdir()
    manifest = {
        **plan["expected_manifest"], "run_id": "run", "created_at": "fixture",
        "seed": trial["seed"], "scenario": trial["scenario"], "tick_count": 100,
        "dataset_hash": args.fingerprint * 64,
        "feature_dataset_hash": "a" * 64, "swap_dataset_hash": "a" * 64,
    }
    metrics = {
        "initial_equity": "1000000", "realized_pnl": "-1", "unrealized_pnl": "0",
        "net_pnl": "-1", "final_equity": "999999", "max_drawdown": "1", "carry_total": "0",
        "fills": "2", "trades": "1", "unpriced_rollovers": "0", "open_positions_at_end": "0",
        "pending_commands_at_end": "0",
    }
    if args.invalid_metrics:
        metrics["net_pnl"] = "NaN"
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({"symbol": "USDJPY", "metrics": metrics}), encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir)}))
(args.out / "finished.json").write_text(json.dumps({"time": time.perf_counter_ns()}), encoding="utf-8")
raise SystemExit(args.exit_code)
