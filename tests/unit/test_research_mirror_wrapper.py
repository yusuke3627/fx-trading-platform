"""実 SSH・DB を使わず、ラッパーの秘密情報と子プロセスの寿命を確認する。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trading.storage import research_mirror_tunnel

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "sync_research_db.sh"
_PASSWORD = "fictional-s3cret /'@"
_REMOTE_DSN = (
    "postgresql://mirror_user:fictional-s3cret%20%2F%27%40@localhost:5432/source_db"
    "?hostaddr=192.0.2.10&application_name=research"
)

_SSH_STUB = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

role = "tunnel" if "-N" in sys.argv else "credentials"
log = Path(os.environ["STUB_LOG"])
def record(event):
    with log.open("a") as stream:
        stream.write(json.dumps({"role": role, "event": event, "pid": os.getpid(),
                                 "args": sys.argv[1:],
                                 "has_source_env": "RESEARCH_SOURCE_DSN" in os.environ}) + "\n")
def stop(signum, frame):
    record("stopped")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record("started")
mode = os.environ.get("STUB_MODE", "")
if role == "tunnel":
    if mode == "tunnel_fails":
        raise SystemExit(255)
    if mode != "tunnel_wait":
        print("RESEARCH_TUNNEL_READY", flush=True)
    while True:
        time.sleep(0.01)
        if mode == "tunnel_dies" and Path(os.environ["STUB_WORKER_READY"]).exists():
            raise SystemExit(255)
elif mode == "credentials_fail":
    print(os.environ["STUB_REMOTE_DSN"], file=sys.stderr)
    raise SystemExit(255)
elif mode == "credentials_wait":
    while True:
        time.sleep(0.01)
else:
    print(os.environ["STUB_REMOTE_DSN"])
'''

_SYNC_STUB = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path
from psycopg.conninfo import conninfo_to_dict

log = Path(os.environ["STUB_LOG"])
options = conninfo_to_dict(os.environ["RESEARCH_SOURCE_DSN"])
expected = conninfo_to_dict(os.environ["STUB_REMOTE_DSN"])
def record(event):
    with log.open("a") as stream:
        stream.write(json.dumps({"role": "sync", "event": event, "pid": os.getpid(),
                                 "args": sys.argv[1:], "host": options["host"],
                                 "hostaddr": options["hostaddr"], "port": options["port"],
                                 "dbname": options["dbname"],
                                 "password_matches": options["password"] == expected["password"],
                                 "application_name": options["application_name"]}) + "\n")
def stop(signum, frame):
    record("stopped")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record("started")
Path(os.environ["STUB_WORKER_READY"]).touch()
if os.environ.get("STUB_MODE") in ("sync_wait", "tunnel_dies"):
    while True:
        time.sleep(0.01)
raise SystemExit(int(os.environ.get("STUB_SYNC_STATUS", "0")))
'''


@pytest.fixture
def wrapper_env(tmp_path: Path) -> dict[str, str]:
    pytest.importorskip("psycopg")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(f"#!{sys.executable}\n{_SSH_STUB}", encoding="utf-8")
    ssh.chmod(0o755)
    package = tmp_path / "trading"
    storage = package / "storage"
    storage.mkdir(parents=True)
    for directory in (package, storage):
        (directory / "__init__.py").write_text(
            "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n",
            encoding="utf-8",
        )
    (storage / "research_mirror.py").write_text(_SYNC_STUB, encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(
        "import socket\n"
        "class Reservation:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): pass\n"
        "    def bind(self, address): assert address == ('127.0.0.1', 0)\n"
        "    def getsockname(self): return ('127.0.0.1', 56321)\n"
        "socket.socket = Reservation\n",
        encoding="utf-8",
    )
    env = {"PATH": os.environ["PATH"]}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        PYTHONPATH=f"{tmp_path}{os.pathsep}{REPO_ROOT / 'src'}",
        PYTHONDONTWRITEBYTECODE="1",
        RESEARCH_PYTHON=sys.executable,
        RESEARCH_SSH_HOST="stub-host",
        STUB_REMOTE_DSN=_REMOTE_DSN,
        STUB_LOG=str(tmp_path / "calls.jsonl"),
        STUB_WORKER_READY=str(tmp_path / "worker.ready"),
    )
    return env


def _records(env: dict[str, str]) -> list[dict]:
    path = Path(env["STUB_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _wait_for_role(env: dict[str, str], role: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(item["role"] == role for item in _records(env)):
            return
        time.sleep(0.01)
    pytest.fail(f"{role} が起動しませんでした")


def _assert_all_stopped(env: dict[str, str]) -> None:
    for item in _records(env):
        with pytest.raises(ProcessLookupError):
            os.kill(item["pid"], 0)


def _assert_no_secret(result: subprocess.CompletedProcess, env: dict[str, str]) -> None:
    for output in (result.stdout, result.stderr, Path(env["STUB_LOG"]).read_text()):
        assert _PASSWORD not in output
        assert _REMOTE_DSN not in output
        assert "fictional-s3cret" not in output


def test_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def test_wrapper_forwards_args_and_only_gives_sync_the_tunnel_dsn(wrapper_env: dict) -> None:
    arguments = [
        "--symbols", "USDJPY", "EURUSD", "--chunk-size", "20", "--sleep-seconds", "0.2",
        "--max-rows", "100", "--target-dsn-env", "OTHER_RESEARCH_DSN",
    ]
    result = subprocess.run(
        ["bash", "-x", str(WRAPPER), *arguments], env=wrapper_env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    records = _records(wrapper_env)
    tunnel = next(item for item in records if item["role"] == "tunnel")
    credentials = next(item for item in records if item["role"] == "credentials")
    worker = next(item for item in records if item["role"] == "sync")
    assert tunnel["args"][-1] == "stub-host"
    assert credentials["args"][-2:] == ["stub-host", "$env:TRADING_DB_DSN"]
    assert "ExitOnForwardFailure=yes" in tunnel["args"]
    assert "PermitLocalCommand=yes" in tunnel["args"]
    assert tunnel["has_source_env"] is False
    assert credentials["has_source_env"] is False
    forward = tunnel["args"][tunnel["args"].index("-L") + 1]
    assert forward == f"127.0.0.1:{worker['port']}:localhost:5432"
    assert worker["port"] == "56321"
    assert worker["args"] == [
        "sync", "--source-dsn-env", "RESEARCH_SOURCE_DSN",
        "--target-dsn-env", "RESEARCH_DB_DSN", *arguments,
    ]
    assert worker["host"] == worker["hostaddr"] == "127.0.0.1"
    assert worker["dbname"] == "source_db"
    assert worker["password_matches"] is True
    assert worker["application_name"] == "research"
    _assert_all_stopped(wrapper_env)
    _assert_no_secret(result, wrapper_env)


@pytest.mark.parametrize(
    ("mode", "worker_status", "expected_status"),
    [("", "7", 7), ("credentials_fail", "0", 1),
     ("tunnel_fails", "0", 1), ("tunnel_dies", "0", 1)],
)
def test_wrapper_cleans_up_after_failures(
    wrapper_env: dict, mode: str, worker_status: str, expected_status: int,
) -> None:
    wrapper_env.update(STUB_MODE=mode, STUB_SYNC_STATUS=worker_status)
    result = subprocess.run(
        ["bash", str(WRAPPER)], env=wrapper_env, capture_output=True, text=True,
        timeout=10, check=False,
    )
    assert result.returncode == expected_status, result.stderr
    if mode in ("credentials_fail", "tunnel_fails"):
        assert not any(item["role"] == "sync" for item in _records(wrapper_env))
    _assert_all_stopped(wrapper_env)
    _assert_no_secret(result, wrapper_env)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize(
    ("mode", "role"),
    [("sync_wait", "sync"), ("credentials_wait", "credentials"), ("tunnel_wait", "tunnel")],
)
def test_shell_trap_cleans_up_on_interrupt(
    wrapper_env: dict, signum: int, mode: str, role: str,
) -> None:
    wrapper_env["STUB_MODE"] = mode
    child = subprocess.Popen(
        ["bash", str(WRAPPER)], env=wrapper_env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        _wait_for_role(wrapper_env, role)
        child.send_signal(signum)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 128 + signum, stderr
        _assert_all_stopped(wrapper_env)
        _assert_no_secret(subprocess.CompletedProcess([], child.returncode, stdout, stderr),
                          wrapper_env)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize("option", ["--source-dsn-env", "--source-dsn-env=OTHER", "--source"])
def test_wrapper_rejects_source_override_before_ssh(wrapper_env: dict, option: str) -> None:
    result = subprocess.run(
        ["bash", str(WRAPPER), option], env=wrapper_env, capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 1
    assert not _records(wrapper_env)


@pytest.mark.parametrize("raw_dsn", ["", "not a DSN", "host=localhost user=mirror_user"])
def test_bad_remote_dsn_is_not_printed(wrapper_env: dict, raw_dsn: str) -> None:
    wrapper_env["STUB_REMOTE_DSN"] = raw_dsn
    result = subprocess.run(
        ["bash", str(WRAPPER)], env=wrapper_env, capture_output=True, text=True,
        timeout=10, check=False,
    )
    assert result.returncode == 1
    assert "複製元の DSN" in result.stderr
    assert not any(item["role"] == "sync" for item in _records(wrapper_env))
    _assert_all_stopped(wrapper_env)


def test_signal_during_child_creation_still_registers_it(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = research_mirror_tunnel._Supervisor()
    child = object()

    def create(*args: object, **kwargs: object) -> object:
        supervisor.interrupt(signal.SIGTERM, None)
        return child

    monkeypatch.setattr(research_mirror_tunnel.subprocess, "Popen", create)
    with pytest.raises(research_mirror_tunnel._Interrupted):
        supervisor.start(["ssh"])
    assert supervisor.children == [child]


def test_help_does_not_open_ssh(wrapper_env: dict) -> None:
    result = subprocess.run(
        ["bash", str(WRAPPER), "--help"], env=wrapper_env, capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    assert "RESEARCH_SSH_HOST" in result.stdout
    assert not _records(wrapper_env)


@pytest.mark.parametrize("host", ["", "-oProxyCommand=bad", "host with spaces"])
def test_bad_ssh_host_does_not_open_ssh(wrapper_env: dict, host: str) -> None:
    wrapper_env["RESEARCH_SSH_HOST"] = host
    result = subprocess.run(
        ["bash", str(WRAPPER)], env=wrapper_env, capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 1
    assert not _records(wrapper_env)
