"""BOJ「主な意見」を声明の告知日時付きで収集する CLI。

    pip install '.[db,pdf]'
    python -m trading.data.policy.opinions_collector --env demo \
        --meetings config/policy_meetings.yaml

保存先は config.storage.dsn_env で指定された環境変数の DSN。
取得件数は主な意見 PDF の取得成功数（重複や抽出失敗を含む）で、声明を含まない。
訂正版の件数は保存件数の内数で、known_at に取得完了時刻を付けた保存を数える。
会合ごとの失敗を報告し、残りを処理してから非ゼロで終了する。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.http import HttpTransport
from trading.data.policy.meetings import DEFAULT_MEETINGS_PATH, PolicyMeeting, load_meetings
from trading.data.policy.opinions import (
    BOJ_SUMMARY_OF_OPINIONS_RAW,
    event_from_opinions,
    extract_pdf_text,
    japanese_statement_pdf_url,
    opinions_url,
    publication_from_statement,
)
from trading.storage.repository import EventRepository


@dataclass(frozen=True)
class CollectionResult:
    fetched: int
    stored: int
    revised: int
    unpublished: int
    failures: tuple[str, ...]


def collect_opinions(
    meetings: Iterable[PolicyMeeting],
    repository: EventRepository,
    *,
    fetch: Callable[[str], bytes],
    clock: Clock,
    extract_text: Callable[[bytes], str] = extract_pdf_text,
) -> CollectionResult:
    """I/O を注入して会合単位で処理する。冪等性は保存先の raw archive 契約に従う。"""
    fetched = stored = revised = unpublished = 0
    failures: list[str] = []
    for meeting in meetings:
        if meeting.bank != "BOJ":
            continue
        stage = "声明の取得・公表日時の解析"
        try:
            statement = fetch(japanese_statement_pdf_url(meeting.decision_date))
            published_at = publication_from_statement(
                extract_text(statement), meeting.decision_date
            )
            if published_at > clock.now():
                unpublished += 1
                continue

            stage = "主な意見の取得"
            raw = fetch(opinions_url(meeting.decision_date))
            retrieved_at = clock.now()
            fetched += 1
            stage = "最新 raw ハッシュの取得"
            latest_hash = repository.latest_raw_hash(
                BOJ_SUMMARY_OF_OPINIONS_RAW, opinions_url(meeting.decision_date)
            )
            if latest_hash == hashlib.sha256(raw).hexdigest():
                continue
            is_revision = latest_hash is not None
            stage = "主な意見の本文抽出"
            event = event_from_opinions(
                meeting,
                raw=raw,
                text=extract_text(raw),
                published_at=published_at,
                retrieved_at=retrieved_at,
                is_revision=is_revision,
            )
            stage = "raw archive の保存"
            if repository.insert_raw_archive(event, require_initial=not is_revision):
                stored += 1
                revised += is_revision
        except Exception as exc:  # noqa: BLE001
            # 会合単位のジョブ境界。成功分を残し、失敗理由と非ゼロ終了を返す。
            failures.append(f"BOJ {meeting.decision_date} {stage}: {type(exc).__name__}: {exc}")
    return CollectionResult(fetched, stored, revised, unpublished, tuple(failures))


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="BOJ 主な意見の PIT 収集")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--meetings", type=Path, default=DEFAULT_MEETINGS_PATH)
    args = parser.parse_args()

    config = load_config(args.env)
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")
    meetings = load_meetings(args.meetings)

    # db extra は接続時だけ必要。import やロジックのテストでは読み込まない。
    from trading.storage.postgres import PostgresEventRepository, connect

    with connect(dsn) as connection:
        result = collect_opinions(
            meetings,
            PostgresEventRepository(connection),
            fetch=HttpTransport().get_bytes,
            clock=SystemClock(),
        )
    for failure in result.failures:
        print(failure, file=sys.stderr)
    print(
        f"boj-opinions: 取得 {result.fetched} 件・保存 {result.stored} 件"
        f"（うち訂正版 {result.revised} 件）・"
        f"未公表 {result.unpublished} 件・失敗 {len(result.failures)} 件"
    )
    if result.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
