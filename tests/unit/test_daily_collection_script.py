"""日次 cron が CLI の収集元を取りこぼしていないこと。

collector に `--source` を足しても `scripts/collect_daily.sh` の一覧に入れ
忘れると、その分岐は本番で一度も走らない。系列が空のまま増えないので、
気づくのは正規化が窓を満たせなくなった後になる。
"""
import re
from pathlib import Path

from trading.data.macro.collector import SOURCES

REPO_ROOT = Path(__file__).resolve().parents[2]
DAILY_SCRIPT = REPO_ROOT / "scripts" / "collect_daily.sh"


def test_the_daily_script_runs_every_macro_source() -> None:
    loop = re.search(
        r"^for source_name in (.+); do$",
        DAILY_SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert loop is not None, "collect_daily.sh の収集元ループが見つからない"

    assert set(loop.group(1).split()) == set(SOURCES)
