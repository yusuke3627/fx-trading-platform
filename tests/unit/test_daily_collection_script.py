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


def test_the_daily_script_runs_every_policy_collector() -> None:
    """policy 配下の CLI も同じ取りこぼし方をする。

    主な意見は会合の約 8 営業日後に公表され、訂正版も後から出る。日次から
    外れていると初回の backfill 以降まったく増えず、PIT アーカイブが静かに
    古いまま残る。増えないことは、それを使う feature を作るまで現れない。
    """
    script = DAILY_SCRIPT.read_text(encoding="utf-8")
    invoked = set(re.findall(r"-m (trading\.data\.policy\.[\w.]+)", script))

    assert invoked == {
        "trading.data.policy.collector",
        "trading.data.policy.opinions_collector",
    }
