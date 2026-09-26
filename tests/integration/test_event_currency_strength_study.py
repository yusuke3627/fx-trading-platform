"""H7 の macro 抽出と範囲限定 tick 書き出し。使い捨て DB だけで実行する。"""
from __future__ import annotations

import csv
import gzip
import json
import os
from datetime import date, timedelta
from uuid import uuid4

import pytest

from trading.backtest import event_currency_strength_study as h7

DSN = os.environ.get("TRADING_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")


@pytest.fixture
def database():
    import psycopg

    suffix = uuid4().hex
    plan = h7.Plan(
        study_version="integration_h7",
        symbols={"usdjpy": f"TEST_UJ_{suffix}", "eurusd": f"TEST_EU_{suffix}"},
        usdjpy_pip_size="0.01", series=tuple(f"test_{suffix}_{i}" for i in range(4)),
        stages={"explore": {"start": "2025-01-01", "end": "2025-12-31"},
                "confirm": {"start": "2024-01-01", "end": "2024-12-31"}},
        excluded_dates=(), bootstrap_seed=71, bootstrap_samples=10,
        confirm_extra_cost_pips="0",
    )
    with psycopg.connect(DSN) as conn:
        try:
            yield conn, plan
        finally:
            conn.rollback()
            conn.execute("DELETE FROM macro_observations WHERE series = ANY(%s)",
                         (list(plan.series),))
            conn.execute("DELETE FROM market_ticks WHERE symbol = ANY(%s)",
                         ([plan.symbols.usdjpy, plan.symbols.eurusd],))
            conn.commit()


def test_events_cli_first_period_release_and_coincident_series(database, tmp_path):
    conn, plan = database
    t0 = h7.release_at(date(2025, 3, 7), plan)
    rows = [(plan.series[0], "period_a", t0),
            (plan.series[0], "period_a", t0 + timedelta(days=14)),
            (plan.series[1], "period_b", t0),
            (plan.series[2], "period_c", t0 - timedelta(minutes=1)),
            # 範囲外の初回公表を捨ててから min を取ると、改定がイベントになってしまう。
            (plan.series[3], "period_d", h7.release_at(date(2023, 12, 1), plan)),
            (plan.series[3], "period_d", t0 + timedelta(days=7))]
    for series, period, known_at in rows:
        conn.execute(
            "INSERT INTO macro_observations (id, series, observation_period, value, source, known_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid4(), series, period, "1", "TEST", known_at),
        )
    conn.commit()
    plan_path, output = tmp_path / "plan.json", tmp_path / "events.json"
    h7.write_json(plan_path, plan.model_dump(mode="json"))
    assert h7.main(["events", "--plan", str(plan_path), "--output", str(output)]) == 0
    result = h7.load_events(output, plan, h7.sha256(plan_path))
    assert len(result.stages.explore.events) == 1
    assert result.stages.explore.events[0].series == plan.series[:2]
    assert len(result.excluded_release_times) == 1
    assert result.stages.explore.excluded[0].day == date(2023, 12, 1)


def test_export_ranges_order_strings_and_id_ceiling(database, tmp_path):
    import psycopg

    writer, plan = database
    t0 = h7.release_at(date(2025, 3, 7), plan)
    events = h7.build_events([(plan.series[0], "period_a", t0)], plan, "plan_hash")
    windows = events.stages.explore.windows
    run_id = uuid4()

    def insert(symbol, instant, bid="100.0100", ask="100.0300"):
        return writer.execute(
            "INSERT INTO market_ticks "
            "(symbol, event_time, bid, ask, received_at, source, ingestion_run) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (symbol, instant, bid, ask, t0 + timedelta(days=100), "TEST", run_id),
        ).fetchone()[0]

    expected = []
    for symbol in (plan.symbols.usdjpy, plan.symbols.eurusd):
        for window in reversed(windows):
            start = h7.broker_at(window.since, plan)
            end = h7.broker_at(window.until, plan)
            insert(symbol, start - timedelta(microseconds=1))
            id_ = insert(symbol, start)
            expected.append([symbol, start.isoformat(), str(id_), "100.0100", "100.0300"])
            id_ = insert(symbol, start, "100.0200", "100.0400")
            expected.append([symbol, start.isoformat(), str(id_), "100.0200", "100.0400"])
            insert(symbol, end)
    writer.commit()
    ceiling = writer.execute("SELECT max(id) FROM market_ticks").fetchone()[0]
    writer.commit()
    added_ids = []

    class InsertAfterCeiling(psycopg.Connection):
        def execute(self, query, params=None, **kwargs):
            result = super().execute(query, params, **kwargs)
            if "max(id)" in query:
                added_ids.append(insert(plan.symbols.usdjpy, h7.broker_at(t0, plan)))
                writer.commit()
            return result

    ticks = tmp_path / "ticks.csv.gz"
    with InsertAfterCeiling.connect(DSN) as reader:
        manifest = h7.export_ticks(reader, plan, events, "explore", ticks, "events_hash")
    with gzip.open(ticks, "rt", newline="") as source:
        rows = list(csv.reader(source))
    assert rows[0] == h7.CSV_FIELDS
    assert rows[1:] == sorted(expected, key=lambda r: (r[0], r[1], int(r[2])))
    assert added_ids[0] > ceiling
    assert manifest["max_id"] == ceiling
    assert manifest["rows_by_symbol"] == {plan.symbols.usdjpy: 6, plan.symbols.eurusd: 6}
    assert manifest["sha256"] == h7.sha256(ticks)
    assert manifest["events_sha256"] == "events_hash"
    assert json.loads(h7.manifest_path(ticks).read_text()) == manifest
