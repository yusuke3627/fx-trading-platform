"""保存済み注文を執行品質CLIの入力へ出力する。発注・時刻補間は行わない。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, TypeAdapter

from trading.backtest.execution_quality_study import (
    BASES,
    Basis,
    FillObservation,
    OrderObservation,
    QuoteObservation,
    Stamp,
    StateObservation,
    StudyInput,
)
from trading.domain.instrument import InstrumentSpec
from trading.domain.order import CommandState
from trading.oms.state_machine import can_transition


def _complete_history(command: dict, states: list[dict]) -> bool:
    if not states or [row["state_revision"] for row in states] != list(
        range(command["state_revision"] + 1)
    ):
        return False
    if (states[0]["state"] != CommandState.CREATED
            or states[0]["changed_at"] != command["created_at"]
            or states[-1]["state"] != command["state"]
            or states[-1]["quantity"] != command["quantity"]):
        return False
    if any(row["changed_at"] is None for row in states):
        return False
    return all(
        a["changed_at"] <= b["changed_at"] and can_transition(a["state"], b["state"])
        for a, b in pairwise(states)
    )


def build_input(
    rows: dict[str, Any], *, start: datetime, end: datetime, basis: Basis,
    instruments: tuple[InstrumentSpec, ...], horizons: tuple[Decimal, ...],
    quote_max_age: Decimal, provenance: str, quote_provenance: str | None = None,
) -> tuple[StudyInput, dict[str, Any]]:
    if basis == "observed_utc" and end > rows["extracted_at"]:
        raise ValueError("観測終了はDBの抽出snapshot時刻以前にしてください")

    def stamp(value: datetime | None) -> Stamp | None:
        return Stamp(at=value, basis=basis) if value is not None else None

    histories: dict[str, list[dict]] = defaultdict(list)
    fills: dict[str, list[dict]] = defaultdict(list)
    for row in rows["states"]:
        histories[str(row["command_id"])].append(row)
    for row in rows["fills"]:
        fills[str(row["execution_command_id"])].append(row)
    orders = []
    decisions_outside_window = 0
    for command in rows["commands"]:
        order_id = str(command["id"])
        history = histories[order_id]
        complete = _complete_history(command, history)
        visible = [row for row in history if row["changed_at"] is not None and row["changed_at"] <= end]
        final_state, quantity = command["state"], command["quantity"]
        # 状態時計とDB保存時計は同一とは限らない。完全な履歴は常に状態時計で
        # 切り出し、不完全な履歴で終了後の変更が見えている場合は復元を拒否する。
        future_state = any(row["changed_at"] is not None and row["changed_at"] > end for row in history)
        changed_at = command.get("state_changed_at")
        future_state = future_state or (changed_at is not None and changed_at > end)
        if complete:
            if not visible:
                raise ValueError(f"注文 {order_id} の終了時点の状態を復元できません。観測窓を広げてください")
            final_state, quantity = visible[-1]["state"], visible[-1]["quantity"]
        elif future_state or (basis == "observed_utc" and command["updated_at"] > end):
            raise ValueError(f"注文 {order_id} の終了時点の状態を復元できません。観測窓を広げてください")
        elif basis != "observed_utc" and not (
            history and history[-1]["state_revision"] == command["state_revision"]
            and history[-1]["state"] == command["state"]
            and history[-1]["quantity"] == command["quantity"]
            and changed_at is not None and history[-1]["changed_at"] == changed_at
        ):
            raise ValueError(f"注文 {order_id} の現在状態の時計を確認できません。状態観測が必要です")
        decision_at = command["decision_at"]
        if decision_at is not None and not start <= decision_at <= end:
            decisions_outside_window += 1
            decision_at = None
        order_fills = fills[order_id]
        if any(row["side"] != command["side"] or row["origin"] != "COMMAND" for row in order_fills):
            raise ValueError(f"注文 {order_id} と fill の方向・由来が一致しません")
        orders.append(OrderObservation(
            order_id=order_id, symbol=command["symbol"], side=command["side"],
            quantity=quantity, final_state=final_state, created_at=stamp(command["created_at"]),
            decision_at=stamp(decision_at),
            # broker_request_started_at は呼出し予定の記録。実際の送信境界の
            # 保存経路が無いため、sent_at と response_at は欠測のままにする。
            history_complete=complete,
            states=tuple(StateObservation(state=row["state"], at=stamp(row["changed_at"]))
                         for row in visible),
            fills_complete=False,
            fills=tuple(FillObservation(
                fill_id=str(row["id"]), quantity=row["quantity"], price=row["price"],
                broker_time=row["broker_time"], received_at=stamp(row["received_at"]),
                executed_at=None,
            ) for row in order_fills),
        ))
    data = StudyInput(
        schema_version="execution_quality_v1",
        population_description=(
            "PostgreSQL保存済みexecution_commands全状態、created_atの両端を含む範囲。"
            "口座による絞り込みなし。orders_completeは保存行の抽出完全性で、"
            "broker上の全注文や過去の削除・未保存を証明しない。"
            "時刻由来は利用者が指定: " + provenance + "。"
            + ("quote採用根拠の申告: " + quote_provenance
               + "。申告は時計同期・現行価格の証明ではありません。"
               if quote_provenance else "quoteの価格由来は未確認のため欠測。")
        ),
        orders_complete=rows["counts"]["unlinked_command_fills"] == 0,
        window_start=stamp(start), window_end=stamp(end),
        horizons_seconds=horizons, quote_max_age_seconds=quote_max_age,
        instruments=instruments, orders=tuple(orders),
        quotes=tuple(QuoteObservation(
            symbol=row["symbol"], observed_at=stamp(row["received_at"]),
            bid=row["bid"], ask=row["ask"],
        ) for row in rows["quotes"] if quote_provenance),
        # Shadow と実執行の停止試行を識別する記録は現行スキーマにない。
        blocked_entries_complete=False,
    )
    evidence = {
        "schema_version": "execution_quality_export_v1",
        "extracted_at": rows["extracted_at"].isoformat(),
        "snapshot": "repeatable_read/read_only",
        "source_counts": rows["counts"],
        "exported_orders": len(data.orders),
        "final_states": dict(Counter(order.final_state for order in orders)),
        "complete_state_histories": sum(order.history_complete for order in orders),
        "decision_timestamps_outside_window": decisions_outside_window,
        "time_basis": basis,
        "provenance": provenance,
        "quote_provenance": quote_provenance,
        "quotes_included": bool(quote_provenance),
        "provenance_limitation": "由来は利用者の申告であり、実データの時計同期・現行quoteを証明しません。",
        "missing": ["input_received_at", "sent_at", "response_at", "executed_at",
                    "fills_completeness", "blocked_entries_completeness"],
        "quote_policy": "received_at; identical prices at the same instant collapsed; conflicting prices rejected",
    }
    return data, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", required=True, help="接続先を入れた環境変数名。暗黙の接続先は使わない")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--time-basis", required=True, choices=BASES)
    parser.add_argument("--provenance", required=True, help="保存元と時計の対応を確認した根拠")
    parser.add_argument("--quote-provenance", help="受信時刻の価格を現行quoteとして扱う根拠の申告。省略時はquote欠測")
    parser.add_argument("--instruments", required=True, type=Path, help="InstrumentSpecのJSON配列")
    parser.add_argument("--horizon-seconds", required=True, type=Decimal, action="append")
    parser.add_argument("--quote-max-age-seconds", required=True, type=Decimal)
    parser.add_argument("--max-rows", type=int, default=100_000, help="各種類の抽出件数上限。超過時は中止")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        start = TypeAdapter(AwareDatetime).validate_python(args.start)
        end = TypeAdapter(AwareDatetime).validate_python(args.end)
        if start >= end or args.max_rows < 1:
            raise ValueError("開始は終了より前、max_rowsは正数にしてください")
        if not args.provenance.strip() or (args.quote_provenance is not None and not args.quote_provenance.strip()):
            raise ValueError("由来の根拠は空欄にできません")
        age = args.quote_max_age_seconds
        if not age.is_finite() or age < 0:
            raise ValueError("quoteの最大経過秒は有限の非負数にしてください")
        if any(not h.is_finite() or h <= 0 for h in args.horizon_seconds):
            raise ValueError("horizonは有限の正数にしてください")
        if args.output_dir.exists():
            raise ValueError("出力先は未作成のディレクトリを指定してください")
        dsn = os.environ.get(args.dsn_env)
        if not dsn:
            raise ValueError("指定した接続先の環境変数が未設定です")
        spec_bytes = args.instruments.read_bytes()
        instruments = TypeAdapter(tuple[InstrumentSpec, ...]).validate_json(spec_bytes)
        # db extra の無い offline 研究環境でも入力モデルを import できる。
        import psycopg

        from trading.storage.execution_observations import PostgresExecutionObservationReader
        from trading.storage.postgres import connect

        try:
            with connect(dsn) as conn:
                rows = PostgresExecutionObservationReader(conn).read(
                    start, end, start - timedelta(microseconds=int(age * 1_000_000)),
                    max_rows=args.max_rows, include_quotes=bool(args.quote_provenance),
                )
        except psycopg.Error as exc:
            # 接続例外にはDSN等が入り得るため、例外本文は出力しない。
            print(f"DBの読出しに失敗しました: {type(exc).__name__}", file=sys.stderr)
            return 2
        data, evidence = build_input(
            rows, start=start, end=end, basis=args.time_basis, instruments=instruments,
            horizons=tuple(args.horizon_seconds), quote_max_age=age, provenance=args.provenance,
            quote_provenance=args.quote_provenance,
        )
        payload = (data.model_dump_json(indent=2) + "\n").encode()
        evidence["input_sha256"] = hashlib.sha256(payload).hexdigest()
        evidence["instruments_sha256"] = hashlib.sha256(spec_bytes).hexdigest()
        args.output_dir.mkdir(parents=True)
        (args.output_dir / "input.json").write_bytes(payload)
        (args.output_dir / "export.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    except (OSError, ValueError, OverflowError, ImportError) as exc:
        print(f"執行観測の出力に失敗: {exc}", file=sys.stderr)
        return 2
    print(f"保存済み注文 {len(data.orders)} 件を出力: {args.output_dir / 'input.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
