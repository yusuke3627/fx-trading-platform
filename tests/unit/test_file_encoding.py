"""Config and data files are read as UTF-8 whatever the platform locale says.

Python falls back to the locale encoding when none is given, so a loader that
omits it works on a UTF-8 developer machine and fails on the trading host,
whose Japanese Windows default is cp932. Nothing in the code shows the
difference, which is why this runs the loaders in a subprocess under an ASCII
locale — the same failure, reproducible from any platform.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# One entry point per file that carries non-ASCII. base.yaml is ASCII, so the
# environment overlay is what makes load_config a real test here; load_coverage
# is the call that actually failed on the trading host.
LOAD_EVERY_FILE = """
from trading.config import load_config
from trading.data.intervention.episodes import load_episodes
from trading.data.policy.meetings import load_coverage

load_config("backtest")
load_coverage()
load_episodes()
"""


def test_loaders_do_not_depend_on_the_platform_locale():
    result = subprocess.run(
        [sys.executable, "-c", LOAD_EVERY_FILE],
        cwd=REPO_ROOT,
        # PYTHONUTF8=0 keeps UTF-8 mode from papering over the locale, which is
        # what the trading host's cp932 default does not do.
        env={
            **os.environ,
            "LC_ALL": "C",
            "PYTHONUTF8": "0",
            "PYTHONPATH": str(REPO_ROOT / "src"),
        },
        capture_output=True,
        text=True,
        # The traceback is the failure message, so a raised CalledProcessError
        # would hide what actually broke.
        check=False,
    )

    assert result.returncode == 0, result.stderr
