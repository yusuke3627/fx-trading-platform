"""研究専用 DB への、再開可能なテキスト COPY。DSN は環境変数からのみ読む。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from trading.storage.postgres import (
    _STREAM_SETTLE_TIMEOUT_SECONDS,
    _TICK_ADVISORY_LOCK_CLASS_ID,
)

TABLES = ("market_ticks", "macro_observations", "events", "swap_snapshots")
MARKER = "fx-trading-platform:research-mirror:v1"
_MIRROR_LOCK = (0x4D495252, 1)


class MirrorError(RuntimeError):
    """接続情報を含まない、利用者に表示できるエラー。"""


@dataclass(frozen=True)
class MirrorOptions:
    symbols: tuple[str, ...] = ("USDJPY",)
    chunk_size: int = 100_000
    sleep_seconds: float = 0.2
    max_rows: int | None = None

    def __post_init__(self) -> None:
        if not self.symbols or any(not symbol.strip() for symbol in self.symbols):
            raise MirrorError("対象通貨を指定してください。")
        if len(set(self.symbols)) != len(self.symbols):
            raise MirrorError("対象通貨が重複しています。")
        if self.chunk_size <= 0 or (self.max_rows is not None and self.max_rows <= 0):
            raise MirrorError("チャンク幅と行数上限は正の整数にしてください。")
        if not math.isfinite(self.sleep_seconds) or self.sleep_seconds < 0:
            raise MirrorError("待ち時間は有限の非負数にしてください。")


@dataclass
class MirrorReport:
    rows: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TABLES, 0))
    ceiling: int | None = None


def _connect(dsn: str, *, read_only: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    try:
        # search_path と日時・文字コードを接続元の既定設定に依存させない。
        conn.execute("SET search_path TO pg_catalog, public")
        conn.execute("SET DateStyle TO ISO")
        conn.execute("SET TimeZone TO 'UTC'")
        conn.execute("SET client_encoding TO 'UTF8'")
        conn.execute("SET default_transaction_isolation TO 'read committed'")
        if read_only:
            conn.execute("SET default_transaction_read_only TO on")
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def _exclusive_target(target: psycopg.Connection) -> Iterator[None]:
    acquired = target.execute("SELECT pg_try_advisory_lock(%s, %s)", _MIRROR_LOCK).fetchone()[0]
    if not acquired:
        raise MirrorError("複製先で別の初期化・同期が実行中です。")
    try:
        yield
    finally:
        target.execute("SELECT pg_advisory_unlock(%s, %s)", _MIRROR_LOCK)


def _require_marker(target: psycopg.Connection) -> None:
    marker = target.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = current_database()"
    ).fetchone()[0]
    if marker != MARKER:
        raise MirrorError("複製先に研究専用 DB の印がありません。空の新設 DB を init してください。")


def _require_distinct_database(source: psycopg.Connection) -> None:
    # target が同じキーを保持中。同一 DB なら別名・別ポート・SSH 経由でも取得できない。
    # pg_control_system() の管理権限や、DSN の文字列比較には依存しない。
    acquired = source.execute("SELECT pg_try_advisory_lock(%s, %s)", _MIRROR_LOCK).fetchone()[0]
    if not acquired:
        raise MirrorError("複製元と複製先が同じ DB、または複製元で別の同期が実行中です。")
    source.execute("SELECT pg_advisory_unlock(%s, %s)", _MIRROR_LOCK)


def _check_schema(target: psycopg.Connection) -> None:
    present = target.execute(
        "SELECT bool_and(to_regclass('public.' || name) IS NOT NULL) "
        "FROM unnest(%s::text[]) AS name", (list(TABLES),),
    ).fetchone()[0]
    if not present:
        raise MirrorError("対象4表がありません。新設 DB に migration を適用してください。")


def initialize(target_dsn: str) -> None:
    with _connect(target_dsn) as target, _exclusive_target(target), target.transaction():
        target.execute("SET LOCAL lock_timeout = '5s'")
        _check_schema(target)
        target.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
            sql.SQL(", ").join(sql.Identifier("public", name) for name in TABLES)
        ))
        for table in TABLES:
            if target.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(
                sql.Identifier("public", table)
            )).fetchone()[0]:
                raise MirrorError("対象4表にデータがあります。印は空の新設 DB にしか付けられません。")
        database = target.execute("SELECT current_database()").fetchone()[0]
        target.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(database), sql.Literal(MARKER)
        ))


def _columns(conn: psycopg.Connection, table: str) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
        (f"public.{table}",),
    ).fetchall()


def _pin_ticks(source: psycopg.Connection, symbols: tuple[str, ...]) -> int | None:
    ceiling = source.execute("SELECT max(id) FROM public.market_ticks").fetchone()[0]
    # id の採番順とコミット順は一致しない。天井を先に読み、各通貨の既存 writer を
    # 待って即解放する。後続 writer はロック取得後に採番するので天井より上になる。
    # stream_between と同じく、全 writer がこの手順を使い、既存 tick を変更しない前提。
    for symbol in symbols:
        with source.transaction():
            source.execute("SELECT set_config('lock_timeout', %s, true)",
                           (f"{int(_STREAM_SETTLE_TIMEOUT_SECONDS * 1000)}ms",))
            source.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                           (_TICK_ADVISORY_LOCK_CLASS_ID, symbol))
    return ceiling


def _copy_table(
    source: psycopg.Connection, target: psycopg.Connection, table: str,
    columns: list[str], select: sql.Composed, params: tuple = (),
) -> int:
    with source.cursor() as reader, target.cursor() as writer:
        with (
            reader.copy(sql.SQL("COPY ({}) TO STDOUT (FORMAT TEXT)").format(select),
                        params) as outgoing,
            writer.copy(sql.SQL("COPY {} ({}) FROM STDIN (FORMAT TEXT)").format(
                sql.Identifier("public", table), sql.SQL(", ").join(map(sql.Identifier, columns)),
            )) as incoming,
        ):
            for block in outgoing:
                incoming.write(block)
        # COPY 完了時のサーバー件数なので、改行を含むテキストや trigger の行抑止も扱える。
        if reader.rowcount != writer.rowcount or reader.rowcount < 0:
            raise MirrorError("COPY の複製元・複製先の行数が一致しません。チャンクを取り消します。")
        return writer.rowcount


def synchronize(
    source_dsn: str, target_dsn: str, options: MirrorOptions | None = None,
    *, report: MirrorReport | None = None,
) -> MirrorReport:
    options = options if options is not None else MirrorOptions()
    report = report if report is not None else MirrorReport()
    with _connect(target_dsn) as target, _exclusive_target(target):
        _require_marker(target)
        with _connect(source_dsn, read_only=True) as source:
            _require_distinct_database(source)
            columns = {}
            for table in TABLES:
                source_columns = _columns(source, table)
                if not source_columns or source_columns != _columns(target, table):
                    raise MirrorError("複製元と複製先の列構成が一致しません。migration を確認してください。")
                columns[table] = [name for name, _ in source_columns]
            report.ceiling = _pin_ticks(source, options.symbols)
            chunk_done = False
            for symbol in options.symbols:
                position = target.execute(
                    "SELECT coalesce(max(id), 0) FROM public.market_ticks WHERE symbol = %s",
                    (symbol,),
                ).fetchone()[0]
                while report.ceiling is not None and position < report.ceiling:
                    remaining = (options.max_rows - report.rows["market_ticks"]
                                 if options.max_rows is not None else options.chunk_size)
                    if remaining == 0:
                        break
                    if chunk_done and options.sleep_seconds:
                        time.sleep(options.sleep_seconds)
                    upper = min(position + options.chunk_size, report.ceiling)
                    select = sql.SQL(
                        "SELECT {} FROM public.market_ticks "
                        "WHERE symbol = %s AND id > %s AND id <= %s ORDER BY id LIMIT %s"
                    ).format(sql.SQL(", ").join(map(sql.Identifier, columns["market_ticks"])))
                    with source.transaction(), target.transaction():
                        count = _copy_table(source, target, "market_ticks", columns["market_ticks"],
                                            select, (symbol, position, upper, remaining))
                    report.rows["market_ticks"] += count
                    position = upper
                    chunk_done = True
            # 小さい3表は同じ source snapshot から読み、target も3表まとめて原子的に置換。
            replaced = {}
            with source.transaction(), target.transaction():
                source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                for table in TABLES[1:]:
                    target.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("public", table)))
                    select = sql.SQL("SELECT {} FROM {}").format(
                        sql.SQL(", ").join(map(sql.Identifier, columns[table])),
                        sql.Identifier("public", table),
                    )
                    # 自己参照 FK は単一 COPY 文の終了時に検査される。順序による分割はしない。
                    replaced[table] = _copy_table(source, target, table, columns[table], select)
            report.rows.update(replaced)
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # 誤って DSN を引数に渡されても、argparse のエラーへ値を転載しない。
        self.exit(2, "引数が不正です。--help で指定方法を確認してください。\n")


def _dsn_from_env(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not os.environ.get(name):
        raise MirrorError("DSN を持つ環境変数名を指定してください。")
    return os.environ[name]


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="対象4表が存在し、空の DB に印を付ける")
    init_parser.add_argument("--target-dsn-env", required=True)
    sync_parser = commands.add_parser("sync", help="研究 DB へ同期する")
    sync_parser.add_argument("--source-dsn-env", required=True)
    sync_parser.add_argument("--target-dsn-env", required=True)
    sync_parser.add_argument("--symbols", nargs="+", default=["USDJPY"])
    sync_parser.add_argument("--chunk-size", type=int, default=100_000, help="id 範囲の幅")
    sync_parser.add_argument("--sleep-seconds", type=float, default=0.2)
    sync_parser.add_argument("--max-rows", type=int, help="今回コピーする tick 行数の上限（全通貨合計）")
    args = parser.parse_args(argv)
    started = time.monotonic()
    report = MirrorReport()
    status, code = "ok", 0
    try:
        target_dsn = _dsn_from_env(args.target_dsn_env)
        if args.command == "init":
            initialize(target_dsn)
        else:
            options = MirrorOptions(tuple(args.symbols), args.chunk_size,
                                    args.sleep_seconds, args.max_rows)
            synchronize(_dsn_from_env(args.source_dsn_env), target_dsn, options, report=report)
    except MirrorError as exc:
        print(str(exc), file=sys.stderr)
        status, code = "failed", 1
    except psycopg.Error as exc:
        print(f"DB 操作に失敗しました（SQLSTATE: {exc.sqlstate or '接続失敗'}）。", file=sys.stderr)
        status, code = "failed", 1
    except KeyboardInterrupt:
        status, code = "interrupted", 130
    except Exception:  # noqa: BLE001 -- CLI 境界で入力値を含む traceback の漏出を防ぐ。
        # psycopg 以外の接続パラメータ例外にも入力値が含まれ得る。
        print("同期処理に失敗しました。接続設定と実行環境を確認してください。", file=sys.stderr)
        status, code = "failed", 1
    elapsed = time.monotonic() - started
    print(json.dumps({"status": status, "rows": report.rows, "ceiling": report.ceiling,
                      "elapsed_seconds": elapsed,
                      "rows_per_second": sum(report.rows.values()) / elapsed if elapsed else 0}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
