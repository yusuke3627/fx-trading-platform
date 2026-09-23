"""研究 DB 同期の SSH 接続と、秘密情報を持つ子プロセスの管理。"""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import time
from types import FrameType

SOURCE_DSN_ENV = "RESEARCH_SOURCE_DSN"
_READY = b"RESEARCH_TUNNEL_READY\n"
_SSH_OPTIONS = [
    "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
    "-o", "ControlMaster=no", "-o", "ControlPath=none",
    "-o", "ForkAfterAuthentication=no",
]


class TunnelError(Exception):
    """秘密情報を含まない、利用者向けのエラー。"""


class _Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _tunnel_dsn(dsn: str, port: int) -> str:
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        options = conninfo_to_dict(dsn.strip())
    except ProgrammingError:
        raise TunnelError("複製元の DSN を解析できません。") from None
    if not options.get("dbname") or not options.get("user"):
        raise TunnelError("複製元の DSN に DB 名またはユーザー名がありません。")
    # hostaddr は host より優先されるため、両方をトンネルへ固定する。
    options.update(host="127.0.0.1", hostaddr="127.0.0.1", port=str(port))
    return make_conninfo(**options)


class _Supervisor:
    def __init__(self) -> None:
        self.children: list[subprocess.Popen] = []
        self.signum: int | None = None

    def interrupt(self, signum: int, _frame: FrameType | None) -> None:
        # Popen の途中で例外を投げず、生成した子を必ず cleanup の対象にする。
        self.signum = signum

    def check(self, tunnel: subprocess.Popen | None = None) -> None:
        if self.signum is not None:
            raise _Interrupted(self.signum)
        if tunnel is not None and tunnel.poll() is not None:
            raise TunnelError("SSH トンネルが終了しました。")

    def start(self, args: list[str], **kwargs: object) -> subprocess.Popen:
        self.check()
        child = subprocess.Popen(args, start_new_session=True, **kwargs)
        self.children.append(child)
        self.check()
        return child

    def close(self) -> None:
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdout is not None:
                child.stdout.close()


def _wait_ready(supervisor: _Supervisor, tunnel: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    output = b""
    while time.monotonic() < deadline:
        supervisor.check(tunnel)
        if select.select([tunnel.stdout], [], [], 0.1)[0]:
            output += os.read(tunnel.stdout.fileno(), len(_READY))
            if output == _READY:
                return
            if not _READY.startswith(output):
                raise TunnelError("SSH トンネルの開始を確認できません。")
    raise TunnelError("SSH トンネルの開始がタイムアウトしました。")


def _sync(supervisor: _Supervisor, arguments: list[str]) -> int:
    if arguments in (["--help"], ["-h"]):
        print("使用方法: scripts/sync_research_db.sh [同期オプション]\n"
              "RESEARCH_SSH_HOST: SSH ホスト（既定 fxvps）\n"
              "RESEARCH_PYTHON: Python 実行ファイル（既定 .venv/bin/python）\n"
              "同期先: --target-dsn-env ENV_NAME（既定 RESEARCH_DB_DSN）\n"
              "--symbols / --chunk-size / --sleep-seconds / --max-rows は同期 CLI に渡します。")
        return 0
    if any(
        arg.startswith("--") and "--source-dsn-env".startswith(arg.split("=", 1)[0])
        for arg in arguments
    ):
        raise TunnelError("--source-dsn-env は SSH ラッパーが設定します。")
    host = os.environ.get("RESEARCH_SSH_HOST", "fxvps")
    if not host or host.startswith("-") or any(char.isspace() for char in host):
        raise TunnelError("RESEARCH_SSH_HOST に有効な SSH ホスト名を指定してください。")

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    tunnel = supervisor.start(
        ["ssh", *_SSH_OPTIONS, "-N", "-L", f"127.0.0.1:{port}:localhost:5432",
         "-o", "ExitOnForwardFailure=yes", "-o", "PermitLocalCommand=yes",
         "-o", "LocalCommand=echo RESEARCH_TUNNEL_READY", host],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    _wait_ready(supervisor, tunnel)
    credentials = supervisor.start(
        ["ssh", *_SSH_OPTIONS, "-o", "PermitLocalCommand=no", host, "$env:TRADING_DB_DSN"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while True:
        supervisor.check(tunnel)
        try:
            raw_dsn, _ = credentials.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise TunnelError("複製元の DSN 取得がタイムアウトしました。") from None
    if credentials.returncode:
        raise TunnelError("SSH から複製元の DSN を取得できませんでした。")
    try:
        source_dsn = _tunnel_dsn(raw_dsn.decode("utf-8-sig"), port)
    except UnicodeError:
        raise TunnelError("複製元の DSN を解析できません。") from None
    child_env = os.environ.copy()
    child_env[SOURCE_DSN_ENV] = source_dsn
    worker = supervisor.start(
        [sys.executable, "-m", "trading.storage.research_mirror", "sync",
         "--source-dsn-env", SOURCE_DSN_ENV, "--target-dsn-env", "RESEARCH_DB_DSN",
         *arguments],
        env=child_env, stdin=subprocess.DEVNULL,
    )
    while True:
        supervisor.check(tunnel)
        try:
            status = worker.wait(timeout=0.1)
            return status if status >= 0 else 128 - status
        except subprocess.TimeoutExpired:
            pass


def main(arguments: list[str] | None = None) -> int:
    supervisor = _Supervisor()
    previous = {
        signum: signal.signal(signum, supervisor.interrupt)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        return _sync(supervisor, sys.argv[1:] if arguments is None else arguments)
    except _Interrupted as exc:
        return 128 + exc.signum
    except TunnelError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ImportError):
        print("SSH ラッパーを実行できません。SSH と Python の db extra を確認してください。",
              file=sys.stderr)
        return 1
    finally:
        supervisor.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
