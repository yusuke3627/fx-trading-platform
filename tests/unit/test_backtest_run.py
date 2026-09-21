"""Manifest reproduction inputs of the backtest CLI."""
import os
import subprocess
from pathlib import Path

import pytest

from trading.backtest.run import git_state


@pytest.fixture
def scratch_repo(tmp_path, monkeypatch):
    """Isolated git repo as cwd. GIT_* variables are scrubbed first: when the
    test suite itself runs inside a git hook (pre-push), inherited GIT_DIR /
    GIT_INDEX_FILE would otherwise redirect every git call — including the
    ones inside git_state() — to the REAL repository."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)

    def _git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    _git("init")
    (tmp_path / ".gitignore").write_text(
        (Path(__file__).resolve().parents[2] / ".gitignore").read_text()
    )
    (tmp_path / "tracked.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_git_state_hashes_untracked_file_contents(scratch_repo):
    # `git diff HEAD` never shows untracked files: two trees with the same
    # untracked path but different contents must hash differently, or the
    # manifest cannot reproduce the executed code.
    (scratch_repo / "untracked.py").write_text("VALUE = 1\n")
    first = git_state()
    assert first["git_dirty"] is True

    (scratch_repo / "untracked.py").write_text("VALUE = 2\n")
    second = git_state()
    assert first["git_diff_sha256"] != second["git_diff_sha256"]


def test_git_state_clean_tree_has_no_diff_hash(scratch_repo):
    state = git_state()
    assert state["git_dirty"] is False
    assert "git_diff_sha256" not in state


@pytest.mark.parametrize("dirty", [False, True])
def test_runtime_logs_do_not_change_reproduction_state(scratch_repo, dirty):
    if dirty:
        (scratch_repo / "untracked.py").write_text("VALUE = 1\n")
    before = git_state()
    for name in ("bars-backfill.log", "progress.log", "study.log", "new-run.log"):
        log = scratch_repo / name
        log.write_text("started\n")
        assert git_state() == before
        log.write_text("started\ncompleted\n")
        assert git_state() == before
        log.unlink()
        assert git_state() == before


def test_tracked_log_changes_remain_part_of_reproduction_state(scratch_repo):
    log = scratch_repo / "fixture.log"
    log.write_text("original fixture\n")
    subprocess.run(["git", "add", "--force", "fixture.log"], check=True)
    before = git_state()
    log.write_text("changed fixture\n")
    after = git_state()
    assert before["git_dirty"] is True
    assert after["git_dirty"] is True
    assert before["git_diff_sha256"] != after["git_diff_sha256"]
