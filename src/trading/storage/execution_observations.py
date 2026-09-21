"""執行品質の抽出に必要な保存行を、一つの読み取り専用 snapshot で読む。"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


class PostgresExecutionObservationReader:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def read(
        self, start: datetime, end: datetime, quote_start: datetime,
        *, max_rows: int, include_quotes: bool = False,
    ) -> dict[str, Any]:
        # 呼出し専用の未使用接続を受け取る。既存 transaction の分離レベルを
        # 黙って引き継がず、全クエリが同じ snapshot を見るようにする。
        with self._conn.transaction():
            self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            extracted_at = self._conn.execute("SELECT transaction_timestamp() AS at").fetchone()["at"]
            params = {"start": start, "end": end, "quote_start": quote_start}
            command_filter = "c.created_at >= %(start)s AND c.created_at <= %(end)s"
            queries = {
                "commands": f"""
                    SELECT c.*, s.generated_at AS decision_at
                    FROM execution_commands c
                    LEFT JOIN position_intents i ON i.id = c.intent_id
                    LEFT JOIN strategy_signals s ON s.id = i.signal_id
                    WHERE {command_filter} ORDER BY c.created_at, c.id
                """,
                "states": f"""
                    SELECT o.* FROM execution_state_observations o
                    JOIN execution_commands c ON c.id = o.command_id
                    WHERE {command_filter} ORDER BY o.command_id, o.id
                """,
                "fills": f"""
                    SELECT f.* FROM fills f
                    JOIN execution_commands c ON c.id = f.execution_command_id
                    WHERE {command_filter} AND f.received_at <= %(end)s
                    ORDER BY f.received_at, f.id
                """,
                "quotes": f"""
                    SELECT DISTINCT t.symbol, t.received_at, t.bid, t.ask
                    FROM market_ticks t
                    WHERE t.received_at >= %(quote_start)s AND t.received_at <= %(end)s
                    AND t.symbol IN (SELECT c.symbol FROM execution_commands c
                                     WHERE {command_filter})
                    ORDER BY t.symbol, t.received_at, t.bid, t.ask
                """,
            }
            rows: dict[str, Any] = {"extracted_at": extracted_at, "counts": {}}
            for name, query in queries.items():
                if name == "quotes" and not include_quotes:
                    rows[name] = []
                    rows["counts"][name] = 0
                    continue
                # LIMIT + 1 は過大な期間を黙って切り捨てず拒否するため。
                values = self._conn.execute(
                    query + " LIMIT %(limit)s", {**params, "limit": max_rows + 1}
                ).fetchall()
                if len(values) > max_rows:
                    raise ValueError(f"{name} が max_rows={max_rows} を超えました。期間を狭めてください")
                rows[name] = values
                rows["counts"][name] = len(values)
            rows["counts"]["unlinked_command_fills"] = self._conn.execute(
                """SELECT count(*) AS count FROM fills
                   WHERE origin = 'COMMAND' AND execution_command_id IS NULL
                     AND received_at >= %(start)s AND received_at <= %(end)s""", params,
            ).fetchone()["count"]
            return rows
