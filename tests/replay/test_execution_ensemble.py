"""実 CLI と軽量な代替コマンドで、反復結果と並列制御を確認する。"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from itertools import pairwise
from pathlib import Path

import pytest

from trading.backtest import execution_ensemble as ensemble
from trading.backtest.execution_ensemble import INPUT_FIELDS
from trading.config import load_config

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "execution_ensemble"


def test_research_cli_repeats_real_pipeline_without_database_or_broker(tmp_path):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "HOME", "TMPDIR", "TEMP", "TMP")
           if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(FIXTURE), str(Path.cwd()))),
               ENSEMBLE_FIXTURE_DSN="synthetic-fixture-no-network")
    summaries = []
    samples = []
    for extra in ([], [], ["--max-parallel", "1"], ["--max-parallel", "2"]):
        result = subprocess.run([
            sys.executable, "-m", "trading.backtest.execution_ensemble",
            "--dsn-env", "ENSEMBLE_FIXTURE_DSN", "--strategy", "ensemble_probe",
            "--from", "2026-01-05T10:00:00+00:00", "--to", "2026-01-05T10:10:00+00:00",
            "--seeds", "7", "8", "--scenarios", "normal", "spread_x2",
            "--purpose", "合成データで既存 Risk/OMS の反復を確認", "--risk-basis", "研究用設定",
            "--out", str(tmp_path), *extra,
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
    assert all(summary == summaries[0] for summary in summaries[1:])
    assert all(sample == samples[0] for sample in samples[1:])
    assert summaries[0]["status"] == "complete"
    assert samples[0][0]["net_pnl"] != samples[0][1]["net_pnl"]


def stub_plan(out):
    trials = []
    for scenario in ("normal", "spread_x2"):
        for seed in (11, 22, 33):
            trial_out = out / f"{len(trials) + 1:04d}-{scenario}-seed-{seed}"
            trials.append({
                "scenario": scenario, "seed": seed, "out": str(trial_out),
                "command": [sys.executable, str(FIXTURE / "trial.py"), "--out", str(trial_out)],
            })
    return {
        "schema_version": 1, "git_state": ensemble.git_state(),
        "expected_manifest": {"symbol": "USDJPY"},
        "config": load_config("backtest").model_dump(mode="json"),
        "scenarios": ["normal", "spread_x2"], "seeds": [11, 22, 33],
        "risk_mode": "research", "purpose": "軽量コマンドで並列制御を確認",
        "risk_basis": "合成入力", "amount_currency": "JPY",
        "pnl_quantile": "0.05", "drawdown_quantile": "0.95", "trials": trials,
    }


@pytest.mark.parametrize("failure", [None, "exit", "metrics", "fingerprint", "spawn"])
def test_default_explicit_serial_and_parallel_write_identical_artifacts(tmp_path, failure):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    if failure == "exit":
        plan["trials"][1]["command"] += ["--exit-code", "7"]
    elif failure == "metrics":
        plan["trials"][1]["command"] += ["--invalid-metrics"]
    elif failure == "fingerprint":
        plan["trials"][1]["command"] += ["--fingerprint", "b"]
    elif failure == "spawn":
        plan["trials"][1]["command"] = [str(tmp_path / "missing-executable")]
    snapshots = []
    for options in ({}, {"max_parallel": 1}, {"max_parallel": 3}):
        summary = ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", **options)
        assert summary["status"] == ("incomplete" if failure else "complete")
        assert summary == json.loads((out / "summary.json").read_text())
        snapshots.append({
            str(path.relative_to(out)): path.read_bytes() for path in out.rglob("*")
            if path.is_file() and path.name not in {"started.json", "finished.json"}
        })
        if options.get("max_parallel", 1) == 1:
            started = [trial for trial in plan["trials"]
                       if (Path(trial["out"]) / "started.json").exists()]
            for previous, following in pairwise(started):
                end = json.loads((Path(previous["out"]) / "finished.json").read_text())["time"]
                start = json.loads((Path(following["out"]) / "started.json").read_text())["time"]
                assert end < start
        shutil.rmtree(out)
    assert snapshots[0] == snapshots[1] == snapshots[2]


@pytest.mark.parametrize("options", [{}, {"max_parallel": 1}])
def test_serial_preserves_checkpoint_and_validation_timing(monkeypatch, tmp_path, capsys, options):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    events = []
    write_json = ensemble._write_json
    validate = ensemble._validate_trial

    def checkpoint(path, payload):
        write_json(path, payload)
        if path.name == "results.json":
            events.append(tuple(trial["status"] for trial in payload["trials"]))
            for trial in payload["trials"]:
                trial_out = Path(trial["out"])
                if trial["status"] == "planned":
                    assert not trial_out.exists()
                elif trial["status"] == "running":
                    assert trial_out.is_dir()
                    assert not (trial_out / "stdout.json").exists()
                    assert "returncode" not in trial

    def validate_trial(*args):
        events.append("validate")
        return validate(*args)

    def git_state():
        events.append("git")
        return plan["git_state"]

    monkeypatch.setattr(ensemble, "_write_json", checkpoint)
    monkeypatch.setattr(ensemble, "_validate_trial", validate_trial)
    monkeypatch.setattr(ensemble, "git_state", git_state)
    ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", **options)
    expected = [("planned",) * 6]
    for index in range(6):
        expected += [
            ("succeeded",) * index + ("running",) + ("planned",) * (5 - index),
            "validate", "git",
            ("succeeded",) * (index + 1) + ("planned",) * (5 - index),
        ]
    expected.append(("succeeded",) * 6)
    assert events == expected
    assert capsys.readouterr().err.splitlines() == [
        f"{trial['scenario']} seed={trial['seed']}" for trial in plan["trials"]
    ]


def test_parallel_limit_refills_slots_and_serializes_checkpoints(monkeypatch, tmp_path, capsys):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    release = tmp_path / "release"
    plan["trials"][0]["command"] += ["--wait-for", str(release)]
    completed = 0
    snapshots = []
    write_json = ensemble._write_json
    validate = ensemble._validate_trial

    def git_state():
        nonlocal completed
        assert threading.current_thread() is threading.main_thread()
        completed += 1
        if completed == 2:
            release.touch()
        return plan["git_state"]

    def checkpoint(path, payload):
        assert threading.current_thread() is threading.main_thread()
        write_json(path, payload)
        assert json.loads(path.read_text()) == payload
        if path.name == "results.json":
            snapshots.append(json.loads(path.read_text()))

    def validate_trial(*args):
        assert threading.current_thread() is threading.main_thread()
        return validate(*args)

    monkeypatch.setattr(ensemble, "git_state", git_state)
    monkeypatch.setattr(ensemble, "_write_json", checkpoint)
    monkeypatch.setattr(ensemble, "_validate_trial", validate_trial)
    summary = ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
    assert summary["status"] == "complete"
    assert completed == 6
    assert all(trial["status"] == "planned" for trial in snapshots[0]["trials"])
    assert all(trial["status"] == "succeeded" for trial in snapshots[-1]["trials"])
    assert capsys.readouterr().err.splitlines() == [
        f"{trial['scenario']} seed={trial['seed']}" for trial in plan["trials"]
    ]
    events = []
    for trial in plan["trials"]:
        trial_out = Path(trial["out"])
        events.extend([
            (json.loads((trial_out / "started.json").read_text())["time"], 1),
            (json.loads((trial_out / "finished.json").read_text())["time"], -1),
        ])
    running = peak = 0
    for _, change in sorted(events):
        running += change
        peak = max(peak, running)
    assert running == 0
    assert peak == 2
    first_end = json.loads((Path(plan["trials"][0]["out"]) / "finished.json").read_text())["time"]
    third_start = json.loads((Path(plan["trials"][2]["out"]) / "started.json").read_text())["time"]
    assert third_start < first_end


@pytest.mark.parametrize("first_failure", [None, "exit", "metrics", "git", "spawn"])
def test_parallel_baseline_uses_first_success_in_plan_order(monkeypatch, tmp_path, first_failure):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    release = tmp_path / "release"
    baseline_index = 0 if first_failure is None else 1
    differing_index = baseline_index + 1
    plan["trials"][baseline_index]["command"] += ["--wait-for", str(release)]
    plan["trials"][differing_index]["command"] += ["--fingerprint", "b"]
    if first_failure == "exit":
        plan["trials"][0]["command"] += ["--exit-code", "7"]
    elif first_failure == "metrics":
        plan["trials"][0]["command"] += ["--invalid-metrics"]
    elif first_failure == "spawn":
        plan["trials"][0]["command"] = [str(tmp_path / "missing-executable")]
    completed = 0

    def git_state():
        nonlocal completed
        completed += 1
        if completed == (2 if first_failure in {"metrics", "git"} else 1):
            release.touch()
        if first_failure == "git" and completed == 1:
            return {**plan["git_state"], "git_commit": "changed"}
        return plan["git_state"]

    monkeypatch.setattr(ensemble, "git_state", git_state)
    summary = ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
    results = json.loads((out / "results.json").read_text())
    baseline_end = json.loads(
        (Path(plan["trials"][baseline_index]["out"]) / "finished.json").read_text()
    )["time"]
    differing_end = json.loads(
        (Path(plan["trials"][differing_index]["out"]) / "finished.json").read_text()
    )["time"]
    assert differing_end < baseline_end
    assert results["common_inputs"]["dataset_hash"] == "a" * 64
    assert results["trials"][baseline_index]["status"] == "succeeded"
    assert results["trials"][differing_index]["error"] == "trial input mismatch: dataset_hash"
    if first_failure:
        assert results["trials"][0]["status"] == "failed"
        if first_failure == "exit":
            assert results["trials"][0]["returncode"] == 7
        elif first_failure == "git":
            assert results["trials"][0]["error"] == "git state changed during the ensemble"
    assert all(trial["status"] in {"succeeded", "failed"} for trial in results["trials"])
    assert summary["status"] == "incomplete"
    assert summary["scenarios"]["normal"]["failed"] == (2 if first_failure else 1)
    assert summary["scenarios"]["normal"]["distribution"] is None
    assert summary["scenarios"]["spread_x2"]["status"] == "complete"


def test_parallel_child_failure_does_not_stop_other_trials(tmp_path):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    plan["trials"][0]["command"] += ["--exit-code", "7"]
    summary = ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
    results = json.loads((out / "results.json").read_text())
    assert results["trials"][0]["status"] == "failed"
    assert results["trials"][0]["returncode"] == 7
    assert all(trial["status"] == "succeeded" for trial in results["trials"][1:])
    assert summary["status"] == "incomplete"
    assert summary["scenarios"]["normal"]["failed"] == 1
    assert summary["scenarios"]["normal"]["distribution"] is None
    assert summary["scenarios"]["spread_x2"]["status"] == "complete"


def test_parallel_git_check_uses_completion_time_while_baseline_is_pending(monkeypatch, tmp_path):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    release = tmp_path / "release"
    plan["trials"][0]["command"] += ["--wait-for", str(release)]
    completed = 0

    def git_state():
        nonlocal completed
        completed += 1
        if completed == 1:
            release.touch()
            return plan["git_state"]
        return {**plan["git_state"], "git_commit": "changed"}

    monkeypatch.setattr(ensemble, "git_state", git_state)
    summary = ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
    results = json.loads((out / "results.json").read_text())
    assert results["trials"][1]["status"] == "succeeded"
    for index in (0, 2, 3, 4, 5):
        assert results["trials"][index]["error"] == "git state changed during the ensemble"
    assert summary["status"] == "incomplete"


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_parallel_finally_stops_children_on_checkpoint_failure(monkeypatch, tmp_path):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    for trial in plan["trials"]:
        trial["command"] += ["--wait-for", str(tmp_path / "never-released")]
    write_json = ensemble._write_json
    popen = ensemble.subprocess.Popen
    children = []
    failed = False

    def start_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def checkpoint(path, payload):
        nonlocal failed
        if path.name == "results.json" and payload["trials"][1]["status"] == "running" and not failed:
            started = Path(plan["trials"][0]["out"]) / "started.json"
            deadline = time.monotonic() + 15
            while not started.exists():
                assert time.monotonic() < deadline, "fixture child did not start"
                time.sleep(0.01)
            failed = True
            raise OSError("synthetic checkpoint failure")
        write_json(path, payload)

    monkeypatch.setattr(ensemble, "_write_json", checkpoint)
    monkeypatch.setattr(ensemble.subprocess, "Popen", start_child)
    try:
        with pytest.raises(OSError, match="synthetic checkpoint failure"):
            ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
        assert children and all(child.poll() is not None for child in children)
        assert json.loads((out / "summary.json").read_text())["status"] == "incomplete"
        assert all(not Path(trial["out"]).exists() for trial in plan["trials"][1:])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_parallel_interrupt_marks_completed_but_unvalidated_trials_failed(monkeypatch, tmp_path):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    plan.update(trials=plan["trials"][:2], scenarios=["normal"], seeds=[11, 22])
    release = tmp_path / "release"
    plan["trials"][0]["command"] += ["--wait-for", str(release)]

    def git_state():
        release.touch()
        return plan["git_state"]

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ensemble, "git_state", git_state)
    monkeypatch.setattr(ensemble, "_validate_trial", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ensemble.run_ensemble(plan, out, research_dsn="synthetic-only", max_parallel=2)
    results = json.loads((out / "results.json").read_text())
    assert results["common_inputs"] is None
    assert all(trial["returncode"] == 0 for trial in results["trials"])
    assert all(trial["status"] == "failed" and trial["error"] == "interrupted"
               for trial in results["trials"])
    assert json.loads((out / "summary.json").read_text())["status"] == "incomplete"


@pytest.mark.skipif(sys.platform == "win32", reason="親だけへの SIGINT 送信は POSIX で検証")
@pytest.mark.parametrize(("max_parallel", "ignore_term", "during_spawn", "repeat_interrupt"), [
    (None, False, False, False), (1, True, False, False),
    (2, False, False, False), (2, True, False, False), (2, False, True, False),
    (2, True, False, True),
])
def test_interrupt_stops_children_and_preserves_unstarted_trials(
    tmp_path, max_parallel, ignore_term, during_spawn, repeat_interrupt,
):
    out = tmp_path / "ensemble"
    plan = stub_plan(out)
    for trial in plan["trials"]:
        trial["command"] += ["--wait-for", str(tmp_path / "never-released")]
        if ignore_term:
            trial["command"] += ["--ignore-term"]
    plan_path = tmp_path / "input-plan.json"
    plan_path.write_text(json.dumps(plan))
    child_pids = []
    driver = """
import json
import signal
import sys
import time
from pathlib import Path
from trading.backtest import execution_ensemble as ensemble

if sys.argv[6] == "repeat":
    original_wait = ensemble.subprocess.Popen.wait

    def paused_wait(self, timeout=None):
        if timeout is not None:
            Path(sys.argv[7]).touch()
            deadline = time.monotonic() + 15
            while not Path(sys.argv[8]).exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup was not released")
                time.sleep(0.01)
        return original_wait(self, timeout=timeout)

    ensemble.subprocess.Popen.wait = paused_wait

if sys.argv[3] == "during-spawn":
    popen = ensemble.subprocess.Popen

    class ObservedStop(ensemble.Event):
        def set(self):
            super().set()
            Path(sys.argv[5]).touch()

    ensemble.Event = ObservedStop

    def paused_spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        deadline = time.monotonic() + 15
        while not Path(sys.argv[4]).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture spawn was not released")
            time.sleep(0.01)
        return process

    ensemble.subprocess.Popen = paused_spawn

options = {} if sys.argv[9] == "None" else {"max_parallel": int(sys.argv[9])}
previous_sigint = signal.getsignal(signal.SIGINT)
try:
    ensemble.run_ensemble(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]),
                          research_dsn="synthetic-only", **options)
finally:
    assert signal.getsignal(signal.SIGINT) == previous_sigint
"""
    spawn_release = tmp_path / "spawn-release"
    stopping_marker = tmp_path / "stopping"
    cleanup_marker = tmp_path / "cleanup"
    cleanup_release = tmp_path / "cleanup-release"
    process = subprocess.Popen([
        sys.executable, "-c", driver,
        str(plan_path), str(out),
        "during-spawn" if during_spawn else "running", str(spawn_release), str(stopping_marker),
        "repeat" if repeat_interrupt else "once", str(cleanup_marker), str(cleanup_release),
        str(max_parallel),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        started_count = 1 if during_spawn else (max_parallel or 1)
        started_paths = [Path(trial["out"]) / "started.json"
                         for trial in plan["trials"][:max_parallel or 1]]
        while sum(path.exists() for path in started_paths) < started_count:
            assert process.poll() is None
            assert time.monotonic() < deadline, "fixture children did not start"
            time.sleep(0.01)
        started_indices = [index for index, path in enumerate(started_paths) if path.exists()]
        child_pids = [json.loads(started_paths[index].read_text())["pid"] for index in started_indices]
        process.send_signal(signal.SIGINT)
        if during_spawn:
            while not stopping_marker.exists():
                assert process.poll() is None
                assert time.monotonic() < deadline, "parent did not handle SIGINT"
                time.sleep(0.01)
        spawn_release.touch()
        if repeat_interrupt:
            while not cleanup_marker.exists():
                assert process.poll() is None
                assert time.monotonic() < deadline, "parent did not start cleanup"
                time.sleep(0.01)
            process.send_signal(signal.SIGINT)
            cleanup_release.touch()
        _, stderr = process.communicate(timeout=15)
        assert process.returncode != 0
        assert "KeyboardInterrupt" in stderr
        assert "AssertionError" not in stderr
        assert all(not pid_exists(pid) for pid in child_pids)
        results = json.loads((out / "results.json").read_text())
        summary = json.loads((out / "summary.json").read_text())
        assert [trial["status"] for trial in results["trials"]] == [
            "failed" if index in started_indices else "planned" for index in range(6)
        ]
        assert all(results["trials"][index]["error"] == "interrupted" for index in started_indices)
        assert sum(path.exists() for path in started_paths) == started_count
        assert all(not Path(trial["out"]).exists() for trial in plan["trials"][2:])
        assert summary["status"] == "incomplete"
        assert all(group["distribution"] is None for group in summary["scenarios"].values())
    finally:
        for pid in child_pids:
            if pid_exists(pid):
                os.kill(pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.communicate()
