"""保存済み観測の執行品質を測る。DB・Broker・LLMには接続しない。

入力契約と取得元の限界は docs/research/2026-09-21-execution-quality-observations.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from trading.domain.instrument import InstrumentSpec
from trading.domain.order import CommandState, ExecutionSide
from trading.domain.position import PositionAction
from trading.oms.state_machine import can_transition

Basis = Literal["observed_utc", "simulated", "reconstructed"]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
TERMINAL = {CommandState.FILLED, CommandState.REJECTED, CommandState.CANCELLED, CommandState.EXPIRED}
WATCHED = {CommandState.UNKNOWN, CommandState.PARTIAL_FILL}
BASES = ("observed_utc", "simulated", "reconstructed")
LATENCIES = ("input_to_decision", "decision_to_send", "send_to_response",
             "send_to_first_execution", "send_to_first_fill_received")


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Stamp(Record):
    at: AwareDatetime
    basis: Basis

    @field_validator("at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class StateObservation(Record):
    state: CommandState
    at: Stamp
    # PARTIAL_FILL は broker の終端証拠がある場合だけ終了と扱う。
    terminal_evidence: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def evidence_for_partial(self) -> StateObservation:
        if self.terminal_evidence and self.state is not CommandState.PARTIAL_FILL:
            raise ValueError("terminal_evidence は PARTIAL_FILL の終端証拠だけに使います")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL or self.terminal_evidence is not None


class FillObservation(Record):
    fill_id: str = Field(min_length=1)
    quantity: Positive
    price: Positive
    # broker_time は別時計の原記録。時間差の計算には使わない。
    broker_time: AwareDatetime | None = None
    received_at: Stamp | None = None
    executed_at: Stamp | None = None


class OrderObservation(Record):
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: ExecutionSide
    quantity: Positive
    final_state: CommandState
    created_at: Stamp
    input_received_at: Stamp | None = None
    decision_at: Stamp | None = None
    sent_at: Stamp | None = None
    response_at: Stamp | None = None
    history_complete: bool = False
    fills_complete: bool = False
    states: tuple[StateObservation, ...] = ()
    fills: tuple[FillObservation, ...] = ()


class QuoteObservation(Record):
    symbol: str
    observed_at: Stamp
    bid: Positive
    ask: Positive

    @model_validator(mode="after")
    def ordered_prices(self) -> QuoteObservation:
        if self.bid > self.ask:
            raise ValueError("bid は ask 以下である必要があります")
        return self


class BlockedEntry(Record):
    event_id: str = Field(min_length=1)
    symbol: str
    at: Stamp
    action: PositionAction
    reason: Literal["NO_UNKNOWN_ORDERS", "ACCOUNT_RECONCILED", "NO_POSITION_MISMATCH",
                    "NO_UNTRACKED_FILL"]
    related_order_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def new_risk_only(self) -> BlockedEntry:
        if self.action not in (PositionAction.OPEN, PositionAction.INCREASE):
            raise ValueError("新規リスクの停止には OPEN / INCREASE だけを記録します")
        return self


QuoteIndex = dict[tuple[str, Basis], list[QuoteObservation]]


class StudyInput(Record):
    schema_version: Literal["execution_quality_v1"]
    population_description: str = Field(min_length=1)
    orders_complete: bool
    window_start: Stamp
    window_end: Stamp
    horizons_seconds: tuple[Positive, ...] = Field(min_length=1)
    quote_max_age_seconds: Nonnegative
    instruments: tuple[InstrumentSpec, ...] = Field(min_length=1)
    orders: tuple[OrderObservation, ...]
    quotes: tuple[QuoteObservation, ...] = ()
    blocked_entries_complete: bool = False
    blocked_entries: tuple[BlockedEntry, ...] = ()

    @model_validator(mode="after")
    def identities(self) -> StudyInput:
        if self.window_start.basis != self.window_end.basis:
            raise ValueError("観測窓の始終は同じ由来にしてください")
        if self.window_start.at >= self.window_end.at:
            raise ValueError("観測窓の終了は開始より後にしてください")
        for horizon in self.horizons_seconds:
            micros = horizon * 1_000_000
            if micros != micros.to_integral_value():
                raise ValueError("horizon はマイクロ秒精度で指定してください")
            if horizon > seconds(datetime.max.replace(tzinfo=UTC) - self.window_end.at):
                raise ValueError("horizon が日時の上限を超えます")
        for values, name in (
            ([s.symbol for s in self.instruments], "symbol"),
            ([o.order_id for o in self.orders], "order_id"),
            ([f.fill_id for o in self.orders for f in o.fills], "fill_id"),
            ([e.event_id for e in self.blocked_entries], "event_id"),
            (list(self.horizons_seconds), "horizon"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} が重複しています")
        symbols = {s.symbol for s in self.instruments}
        for spec in self.instruments:
            if not spec.pip_size.is_finite() or spec.pip_size <= 0:
                raise ValueError("pip_size は有限の正数にしてください")
        if any(o.symbol not in symbols for o in self.orders):
            raise ValueError("注文の InstrumentSpec がありません")
        if any(q.symbol not in symbols for q in self.quotes):
            raise ValueError("quote の InstrumentSpec がありません")
        quote_keys = [(q.symbol, q.observed_at.basis, q.observed_at.at) for q in self.quotes]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("同一 symbol・由来・受信時刻の quote が重複しています")
        ids = {o.order_id for o in self.orders}
        for event in self.blocked_entries:
            if event.symbol not in symbols or not set(event.related_order_ids) <= ids:
                raise ValueError("停止記録に未知の symbol / order_id があります")
            if len(event.related_order_ids) != len(set(event.related_order_ids)):
                raise ValueError("停止記録の related_order_ids が重複しています")
            if not self.window_start.at <= event.at.at <= self.window_end.at:
                raise ValueError("停止記録が観測窓の外にあります")
        return self


def seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000


def duration(start: Stamp | None, end: Stamp | None) -> dict[str, Any]:
    if start is None or end is None:
        return {"status": "missing_timestamp", "seconds": None, "basis": None}
    if start.basis != end.basis:
        return {"status": "mixed_basis", "seconds": None, "basis": None}
    if end.at < start.at:
        return {"status": "invalid_chronology", "seconds": None, "basis": start.basis}
    return {"status": "ok", "seconds": seconds(end.at - start.at), "basis": start.basis}


def order_errors(order: OrderObservation, data: StudyInput) -> list[str]:
    errors = []
    stamps = [order.created_at, order.input_received_at, order.decision_at,
              order.sent_at, order.response_at]
    stamps += [s.at for s in order.states]
    stamps += [t for f in order.fills for t in (f.executed_at, f.received_at)]
    # 窓は正規化済み UTC 値の共通抽出範囲で、状態間の時計順序とは分ける。
    if any(t and not data.window_start.at <= t.at <= data.window_end.at for t in stamps):
        errors.append("timestamp_outside_window")
    if any(s.at.basis == order.created_at.basis and s.at.at < order.created_at.at
           for s in order.states):
        errors.append("state_before_creation")
    previous_by_basis: dict[Basis, datetime] = {}
    for state in order.states:
        previous = previous_by_basis.get(state.at.basis)
        if previous is not None and state.at.at < previous:
            errors.append("state_history_not_ordered")
        previous_by_basis[state.at.basis] = state.at.at
    if any(s.terminal for s in order.states[:-1]):
        errors.append("state_after_terminal")
    if order.history_complete and order.states and order.states[-1].state != order.final_state:
        errors.append("final_state_mismatch")
    if order.history_complete and (
        not order.states or order.states[0].state is not CommandState.CREATED
        or order.states[0].at != order.created_at
    ):
        errors.append("complete_history_requires_creation")
    if order.history_complete and any(
        not can_transition(a.state, b.state) for a, b in zip(order.states, order.states[1:])
    ):
        errors.append("invalid_state_transition")
    filled = sum((f.quantity for f in order.fills), Decimal(0))
    if filled > order.quantity:
        errors.append("overfilled")
    if order.fills_complete and order.final_state is CommandState.FILLED and filled != order.quantity:
        errors.append("filled_state_quantity_mismatch")
    if (order.sent_at and order.created_at.basis == order.sent_at.basis
            and order.sent_at.at < order.created_at.at):
        errors.append("send_before_creation")
    for fill in order.fills:
        for stamp in (fill.received_at, fill.executed_at):
            if stamp and stamp.basis == order.created_at.basis and stamp.at < order.created_at.at:
                errors.append("fill_before_creation")
            if (stamp and order.sent_at and stamp.basis == order.sent_at.basis
                    and stamp.at < order.sent_at.at):
                errors.append("fill_before_send")
        if (fill.executed_at and fill.received_at
                and fill.executed_at.basis == fill.received_at.basis
                and fill.executed_at.at > fill.received_at.at):
            errors.append("execution_after_receipt")
    return sorted(set(errors))


def index_quotes(quotes: tuple[QuoteObservation, ...]) -> QuoteIndex:
    index: QuoteIndex = {}
    for quote in quotes:
        index.setdefault((quote.symbol, quote.observed_at.basis), []).append(quote)
    for group in index.values():
        group.sort(key=lambda q: q.observed_at.at)
    return index


def quote_at(
    data: StudyInput, index: QuoteIndex, symbol: str, stamp: Stamp,
) -> tuple[QuoteObservation | None, str]:
    if stamp.at > data.window_end.at:
        return None, "right_censored"
    quotes = index.get((symbol, stamp.basis), [])
    position = bisect_right(quotes, stamp.at, key=lambda q: q.observed_at.at) - 1
    if position < 0:
        return None, "missing_quote"
    quote = quotes[position]
    if seconds(stamp.at - quote.observed_at.at) > data.quote_max_age_seconds:
        return None, "stale_quote"
    return quote, "ok"


def fill_metrics(
    order: OrderObservation, fill: FillObservation, spec: InstrumentSpec, data: StudyInput,
    quote_index: QuoteIndex,
) -> dict[str, Any]:
    sign = Decimal(1) if order.side is ExecutionSide.BUY else Decimal(-1)
    quote, status = quote_at(data, quote_index, order.symbol, order.decision_at) if order.decision_at else (
        None, "missing_decision_timestamp"
    )
    slippage = None
    fill_stamp = fill.executed_at or fill.received_at
    if quote and (fill_stamp is None or fill_stamp.basis != order.decision_at.basis):
        quote, status = None, "missing_or_mixed_fill_basis"
    if quote and fill_stamp.at < order.decision_at.at:
        quote, status = None, "invalid_chronology"
    if quote:
        reference = quote.ask if order.side is ExecutionSide.BUY else quote.bid
        slippage = sign * (fill.price - reference) / spec.pip_size
    marks = []
    for horizon in data.horizons_seconds:
        row: dict[str, Any] = {"horizon_seconds": horizon, "basis": None,
                               "mid_pips": None, "executable_pips": None,
                               "executable_quote_amount": None, "quote_observed_at": None}
        if fill.executed_at is None:
            row["status"] = "missing_execution_timestamp"
        else:
            target = fill.executed_at.model_copy(update={
                "at": fill.executed_at.at + timedelta(microseconds=int(horizon * 1_000_000)),
            })
            future, row["status"] = quote_at(data, quote_index, order.symbol, target)
            row["basis"] = target.basis
            if future:
                mid = (future.bid + future.ask) / 2
                executable = future.bid if order.side is ExecutionSide.BUY else future.ask
                change = sign * (executable - fill.price)
                row.update(mid_pips=sign * (mid - fill.price) / spec.pip_size,
                           executable_pips=change / spec.pip_size,
                           executable_quote_amount=change * fill.quantity,
                           quote_observed_at=future.observed_at.at.isoformat())
        marks.append(row)
    return {"fill_id": fill.fill_id, "quantity": fill.quantity, "price": fill.price,
            "broker_time": fill.broker_time.isoformat() if fill.broker_time else None,
            "received_at": fill.received_at.model_dump(mode="json") if fill.received_at else None,
            "executed_at": fill.executed_at.model_dump(mode="json") if fill.executed_at else None,
            "send_to_execution": duration(order.sent_at, fill.executed_at),
            "send_to_fill_received": duration(order.sent_at, fill.received_at),
            "decision_slippage": {"status": status, "adverse_pips": slippage,
                                  "basis": order.decision_at.basis if order.decision_at else None},
            "markouts": marks}


def pending_intervals(order: OrderObservation, data: StudyInput) -> dict[str, Any]:
    if not order.history_complete:
        return {"status": "incomplete_state_history", "intervals": []}
    if not order.fills_complete:
        return {"status": "incomplete_fill_history", "intervals": []}
    if any(f.received_at is None for f in order.fills):
        return {"status": "missing_fill_receipt_timestamp", "intervals": []}
    intervals = []
    for index, state in enumerate(order.states):
        if state.state not in WATCHED or state.terminal:
            continue
        censored = index == len(order.states) - 1
        end = data.window_end if censored else order.states[index + 1].at
        span = duration(state.at, end)
        row = {"state": state.state.value, "start": state.at.model_dump(mode="json"),
               "end": end.model_dump(mode="json"), "right_censored": censored,
               **span, "remaining_quantity_seconds": None}
        fills = [f for f in order.fills if f.received_at is not None]
        if span["status"] == "ok" and any(f.received_at.basis != state.at.basis for f in fills):
            row["status"] = "mixed_basis"
        if row["status"] == "ok":
            remaining = order.quantity - sum((f.quantity for f in fills
                                               if f.received_at.at <= state.at.at), Decimal(0))
            cursor, area = state.at.at, Decimal(0)
            for fill in sorted(fills, key=lambda f: f.received_at.at):
                if state.at.at < fill.received_at.at < end.at:
                    area += remaining * seconds(fill.received_at.at - cursor)
                    remaining -= fill.quantity
                    cursor = fill.received_at.at
            area += remaining * seconds(end.at - cursor)
            row["remaining_quantity_seconds"] = area
        intervals.append(row)
    return {"status": "ok" if all(r["status"] == "ok" for r in intervals) else "incomplete",
            "intervals": intervals}


def distribution(values: list[Decimal]) -> dict[str, Any]:
    ordered = sorted(values)
    def percentile(percent: Decimal) -> Decimal | None:
        if not ordered:
            return None
        position = Decimal(len(ordered) - 1) * percent
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {"count": len(values), "p50": percentile(Decimal("0.5")),
            "p95": percentile(Decimal("0.95")), "max": max(values, default=None)}


def summarize(rows: list[dict[str, Any]], data: StudyInput, spec: InstrumentSpec) -> dict[str, Any]:
    selected = [row for row in rows if row["symbol"] == spec.symbol]
    latency = {}
    for name in LATENCIES:
        samples = [r["latencies"][name] if r["status"] == "ok" else
                   {"status": "invalid_order", "seconds": None, "basis": None} for r in selected]
        compared = sum(v["status"] == "ok" for v in samples)
        latency[name] = {"orders_total": len(selected),
                         "orders_compared": compared,
                         "coverage_over_orders": Decimal(compared) / len(selected) if selected else None,
                         "statuses": dict(Counter(v["status"] for v in samples)),
                         "by_basis": {basis: distribution([v["seconds"] for v in samples
                                      if v["status"] == "ok" and v["basis"] == basis])
                                      for basis in BASES}}
    fills = [f for r in selected if r["status"] == "ok" for f in r["fills"]]
    markouts = []
    for horizon in data.horizons_seconds:
        samples = [(f, m) for f in fills for m in f["markouts"] if m["horizon_seconds"] == horizon]
        for basis in BASES:
            eligible = [(f, m) for f, m in samples if m["status"] == "ok" and m["basis"] == basis]
            quantity = sum((f["quantity"] for f, _ in eligible), Decimal(0))
            orders_compared = sum(any(m["status"] == "ok" and m["basis"] == basis
                                     and m["horizon_seconds"] == horizon
                                     for f in r["fills"] for m in f["markouts"])
                                  for r in selected if r["status"] == "ok")
            statuses = Counter("other_basis" if m["basis"] is not None and m["basis"] != basis
                               else m["status"] for _, m in samples)
            invalid_fills = sum(r["known_fills_count"] for r in selected if r["status"] != "ok")
            if invalid_fills:
                statuses["invalid_order"] += invalid_fills
            markouts.append({"horizon_seconds": horizon, "basis": basis,
                             "fills_total": len(samples) + invalid_fills, "compared": len(eligible),
                             "orders_total": len(selected), "orders_compared": orders_compared,
                             "coverage_over_orders": (Decimal(orders_compared) / len(selected)
                                                      if selected else None),
                             "statuses": dict(statuses),
                             "compared_quantity": quantity,
                             "weighted_executable_pips": (
                                 sum((f["quantity"] * m["executable_pips"] for f, m in eligible),
                                     Decimal(0)) / quantity if quantity else None)})
    pending = []
    for state in sorted(WATCHED, key=lambda s: s.value):
        for basis in BASES:
            intervals = [i for r in selected if r["status"] == "ok"
                         for i in r["pending"]["intervals"]
                         if i["state"] == state.value and i["basis"] == basis and i["status"] == "ok"]
            pending.append({"state": state.value, "basis": basis, "intervals": len(intervals),
                            "right_censored": sum(i["right_censored"] for i in intervals),
                            "seconds": sum((i["seconds"] for i in intervals), Decimal(0)),
                            "remaining_quantity_seconds": sum(
                                (i["remaining_quantity_seconds"] for i in intervals), Decimal(0))})
    blocked = [e for e in data.blocked_entries if e.symbol == spec.symbol]
    pending_compared = sum(r["pending"]["status"] == "ok" for r in selected)
    return {"symbol": spec.symbol, "base_currency": spec.base_currency.value,
            "quote_currency": spec.quote_currency.value, "pip_size": spec.pip_size,
            "orders_total": len(selected), "invalid_orders": sum(r["status"] != "ok" for r in selected),
            "final_states": dict(Counter(r["final_state"] for r in selected)),
            "known_fills_total": sum(r["known_fills_count"] for r in selected),
            "orders_without_known_fills": sum(r["known_fills_count"] == 0 for r in selected),
            "incomplete_fill_histories": sum(not o.fills_complete for o in data.orders
                                              if o.symbol == spec.symbol),
            "latencies_seconds": latency, "markouts": markouts, "pending": pending,
            "pending_statuses": dict(Counter(r["pending"]["status"] for r in selected)),
            "pending_orders_compared": pending_compared,
            "pending_coverage_over_orders": (Decimal(pending_compared) / len(selected)
                                             if selected else None),
            "blocked_entries_observed": len(blocked),
            "blocked_entries_count": len(blocked) if data.blocked_entries_complete else None,
            "blocked_entries_by_basis": dict(Counter(e.at.basis for e in blocked))}


def measure(data: StudyInput) -> dict[str, Any]:
    specs = {s.symbol: s for s in data.instruments}
    quote_index = index_quotes(data.quotes)
    rows = []
    for order in data.orders:
        errors = order_errors(order, data)
        latencies = {"input_to_decision": duration(order.input_received_at, order.decision_at),
                     "decision_to_send": duration(order.decision_at, order.sent_at),
                     "send_to_response": duration(order.sent_at, order.response_at)}
        for field, name in (("executed_at", "send_to_first_execution"),
                            ("received_at", "send_to_first_fill_received")):
            stamps = [getattr(f, field) for f in order.fills if getattr(f, field) is not None]
            latencies[name] = duration(order.sent_at, min(stamps, key=lambda t: t.at, default=None))
            if len(stamps) != len(order.fills) or not order.fills_complete:
                latencies[name] = {"status": "incomplete_fill_timestamps", "seconds": None,
                                   "basis": None}
            elif len({s.basis for s in stamps}) > 1:
                latencies[name] = {"status": "mixed_basis", "seconds": None, "basis": None}
        errors += [f"{name}:invalid_chronology" for name, value in latencies.items()
                   if value["status"] == "invalid_chronology"]
        row = {"order_id": order.order_id, "symbol": order.symbol, "side": order.side.value,
               "quantity": order.quantity, "final_state": order.final_state.value,
               "status": "invalid" if errors else "ok", "errors": errors,
               "known_fills_count": len(order.fills), "latencies": latencies,
               "fills": [] if errors else [fill_metrics(order, f, specs[order.symbol], data, quote_index)
                                            for f in order.fills],
               "pending": {"status": "invalid_order", "intervals": []} if errors
               else pending_intervals(order, data)}
        rows.append(row)
    return {"schema_version": "execution_quality_report_v1",
            "population_description": data.population_description,
            "orders_complete": data.orders_complete, "orders_total": len(rows),
            "window_start": data.window_start.model_dump(mode="json"),
            "window_end": data.window_end.model_dump(mode="json"),
            "horizons_seconds": data.horizons_seconds,
            "quote_max_age_seconds": data.quote_max_age_seconds,
            "blocked_entries_complete": data.blocked_entries_complete,
            "symbols": [summarize(rows, data, s) for s in data.instruments],
            "orders": rows,
            "blocked_entries": [e.model_dump(mode="json") for e in data.blocked_entries],
            "limitations": [
                "入力に含まれる注文の集計。母集団の完全性は入力者の宣言で、ツールは証明しません。",
                "broker_time は別時計です。executed_at がなければ約定遅延と markout は欠測です。",
                ("markout は executed_at + horizon 以前の最新 quote を使用します。"
                 "執行可能側は BUY が bid、SELL が ask、価格改善が正です。"),
                "markout は約定方向の価格差で、実現損益ではありません。手数料・carry は含みません。",
                "数量×秒は受信済み fill を差し引いた未約定観測数量の滞留で、拘束資金ではありません。",
                "右打切り区間は観測終了までの下限です。由来・symbol をまたいで合計しません。",
                "実測の較正や戦略の収益性、実運用への昇格を判定するレポートではありません。",
            ]}


def markdown(report: dict[str, Any]) -> str:
    lines = ["# 執行品質の観測", "", f"入力注文: {report['orders_total']} 件。",
             f"母集団: {report['population_description']}",
             f"注文母集団が完全という入力宣言: {report['orders_complete']}",
             f"観測窓: {report['window_start']} → {report['window_end']}",
             f"事前指定 horizon（秒）: {', '.join(map(str, report['horizons_seconds']))}",
             f"quote の最大経過秒: {report['quote_max_age_seconds']}", ""]
    for symbol in report["symbols"]:
        lines += [f"## {symbol['symbol']}", "",
                  (f"注文 {symbol['orders_total']} / 不整合 {symbol['invalid_orders']} / "
                  f"既知の fill {symbol['known_fills_total']} / "
                  f"fill 記録なし {symbol['orders_without_known_fills']}"),
                  f"最終状態: {json.dumps(symbol['final_states'], ensure_ascii=False)}", "",
                  "| 遅延区間 | 由来 | 比較件数 / 全注文 | p50 秒 | p95 秒 |",
                  "|---|---|---:|---:|---:|"]
        for name, metric in symbol["latencies_seconds"].items():
            for basis, stats in metric["by_basis"].items():
                lines.append(f"| {name} | {basis} | {stats['count']} / {symbol['orders_total']} "
                             f"| {stats['p50']} | {stats['p95']} |")
        lines += ["", "| markout 秒 | 由来 | 比較注文 / 全注文 | 比較 / 既知 fill | 数量加重 pips（有利が正） |",
                  "|---:|---|---:|---:|---:|"]
        for row in symbol["markouts"]:
            lines.append(f"| {row['horizon_seconds']} | {row['basis']} | {row['orders_compared']} / "
                         f"{symbol['orders_total']} | {row['compared']} / "
                         f"{symbol['known_fills_total']} | {row['weighted_executable_pips']} |")
        lines += ["", (f"未確定区間を評価できる履歴: {symbol['pending_orders_compared']} / "
                       f"{symbol['orders_total']} 注文。計測状態: {symbol['pending_statuses']}"), "",
                  f"| 状態 | 由来 | 区間数 / 右打切り | 秒 | {symbol['base_currency']} 数量×秒 |",
                  "|---|---|---:|---:|---:|"]
        for row in symbol["pending"]:
            lines.append(f"| {row['state']} | {row['basis']} | {row['intervals']} / "
                         f"{row['right_censored']} | {row['seconds']} | "
                         f"{row['remaining_quantity_seconds']} |")
        lines += ["", (f"新規リスク停止: 観測 {symbol['blocked_entries_observed']} 件。"
                  f"完全な件数: {symbol['blocked_entries_count']}（None はログ不足）。"), ""]
    lines += ["## 注文ごとの欠測・不整合", ""]
    for order in report["orders"]:
        missing = [f"{k}={v['status']}" for k, v in order["latencies"].items() if v["status"] != "ok"]
        for fill in order["fills"]:
            missing += [f"{fill['fill_id']}:{k}={fill[k]['status']}" for k in
                        ("send_to_execution", "send_to_fill_received", "decision_slippage")
                        if fill[k]["status"] != "ok"]
            missing += [f"{fill['fill_id']}:markout({m['horizon_seconds']})={m['status']}"
                        for m in fill["markouts"] if m["status"] != "ok"]
        lines.append(f"- {order['order_id']}: {order['final_state']}; "
                     f"{', '.join(order['errors'] + missing) or '段階時刻の欠測なし'}; "
                     f"履歴={order['pending']['status']}")
    lines += ["", "## 解釈の限界", ""] + [f"- {v}" for v in report["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        data = StudyInput.model_validate_json(raw)
        report = measure(data)
        report["input_sha256"] = hashlib.sha256(raw).hexdigest()
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8",
        )
        (args.output_dir / "report.md").write_text(markdown(report), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"執行品質の集計に失敗: {exc}", file=sys.stderr)
        return 2
    print(f"{len(data.orders)} 件を集計: {args.output_dir / 'report.md'}")
    return 1 if any(o["status"] == "invalid" for o in report["orders"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
