"""実 CLI を別プロセスで起動し、DB 境界以外は既存 Risk/OMS/Engine を通す。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from trading.backtest.execution_ensemble import INPUT_FIELDS

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "execution_ensemble"


def test_research_cli_repeats_real_pipeline_without_database_or_broker(tmp_path):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "HOME", "TMPDIR", "TEMP", "TMP")
           if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(FIXTURE), str(Path.cwd()))),
               ENSEMBLE_FIXTURE_DSN="synthetic-fixture-no-network")
    summaries = []
    samples = []
    for _ in range(2):
        result = subprocess.run([
            sys.executable, "-m", "trading.backtest.execution_ensemble",
            "--dsn-env", "ENSEMBLE_FIXTURE_DSN", "--strategy", "ensemble_probe",
            "--from", "2026-01-05T10:00:00+00:00", "--to", "2026-01-05T10:10:00+00:00",
            "--seeds", "7", "8", "--scenarios", "normal", "spread_x2",
            "--purpose", "合成データで既存 Risk/OMS の反復を確認", "--risk-basis", "研究用設定",
            "--out", str(tmp_path),
        ], capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        out = Path(summary.pop("ensemble_dir"))
        results = json.loads((out / "results.json").read_text())
        assert all(trial["status"] == "succeeded" for trial in results["trials"])
        assert all(int(trial["metrics"]["fills"]) > 0 for trial in results["trials"])
        assert results["common_inputs"]["tick_count"] == 600
        assert all(field in results["common_inputs"] for field in INPUT_FIELDS)
        summaries.append(summary)
        samples.append([trial["metrics"] for trial in results["trials"]])
    assert summaries[0] == summaries[1]
    assert samples[0] == samples[1]
    assert summaries[0]["status"] == "complete"
    assert samples[0][0]["net_pnl"] != samples[0][1]["net_pnl"]
