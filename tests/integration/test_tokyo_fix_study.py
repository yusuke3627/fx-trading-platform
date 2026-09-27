"""使い捨て DB で H10 quotes の絞り込み、境界、DST、読み取り専用接続を確認する。"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import make_tick, tokyo_fix_data, tokyo_fix_plan
from trading.backtest import tokyo_fix_study as h10

DSN = os.environ.get("TRADING_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="使い捨てDBの TRADING_DB_DSN が必要")


def test_quotes_source_id_windows_dst_and_readonly(tmp_path, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from trading.storage.postgres import PostgresMarketTickRepository

    symbol = "TEST_H10_" + uuid4().hex[:8]
    monkeypatch.setitem(h10.POINT_SCALES, symbol, Decimal("0.001"))
    bounds = {"start": "2020-03-06", "end": "2020-11-02"}
    plan = tokyo_fix_plan(symbol=symbol, fetch_range=bounds, main=bounds,
                          post=bounds, calibration=bounds,
                          oanda=bounds | {"max_tick_id": 1, "source": "MT5",
                                          "server_ahead_of_ny_hours": 7})
    connect = psycopg.connect
    with connect(DSN, row_factory=dict_row) as writer:
        repository = PostgresMarketTickRepository(writer)

        def insert(when, bid, source="MT5"):
            tick = make_tick(str(bid), str(bid + Decimal(".01")), time=when, symbol=symbol)
            repository.insert_many([tick], source=source, ingestion_run=uuid4())
            return writer.execute(
                "SELECT id FROM market_ticks WHERE symbol = %s AND event_time = %s AND bid = %s",
                (symbol, when, bid),
            ).fetchone()["id"]

        expected = {}
        # 実 UTC からの変換の期待値は、別途固定したラベル時刻で確かめる。
        offsets = {date(2020, 3, 6): 2, date(2020, 3, 9): 3,
                   date(2020, 10, 30): 3, date(2020, 11, 2): 2}
        try:
            for day, offset in offsets.items():
                slots = {}
                for index, slot in enumerate(h10.SLOTS):
                    local = {"entry": (30, 0), "exit": (52, 0), "paper_entry": (50, 0),
                             "paper_switch": (55, 0), "paper_exit": (59, 0)}[slot]
                    start = datetime(day.year, day.month, day.day, offset, *local, tzinfo=UTC)
                    bid = Decimal(100 + index)
                    insert(start - timedelta(microseconds=1), bid + 1)
                    insert(start, bid + 2, "TEST_OTHER")
                    first_id = insert(start, bid)
                    insert(start, bid + Decimal(".001"))
                    insert(start + timedelta(seconds=59), bid + 3)
                    last_id = insert(start + timedelta(seconds=59), bid + 4)
                    insert(start + timedelta(seconds=60), bid + 5)
                    slots[slot] = last_id if slot == "paper_exit" else first_id
                expected[day] = slots
            cap = writer.execute(
                "SELECT max(id) AS max_id FROM market_ticks WHERE symbol = %s", (symbol,),
            ).fetchone()["max_id"]
            # より新しい id は、終了窓の最後にあっても選ばない。
            for day, offset in offsets.items():
                insert(datetime(day.year, day.month, day.day, offset, 59, 59, 999999, tzinfo=UTC),
                       Decimal(200))
            writer.commit()
            plan = plan.model_copy(update={"oanda": plan.oanda.model_copy(update={"max_tick_id": cap})})
            path = tmp_path / "plan.json"
            path.write_text(plan.model_dump_json())
            holiday, payload, _ = tokyo_fix_data(plan)
            directory = tmp_path / "data"
            h10.fetch(path, directory,
                      retrieve=lambda url: holiday if url == plan.holiday_csv_url else payload,
                      sleep=lambda _: None)
            calls = []

            def readonly_connect(dsn, **kwargs):
                assert dsn == DSN
                assert kwargs == {"options": "-c default_transaction_read_only=on"}
                conn = connect(dsn, **kwargs)
                assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
                calls.append(True)
                return conn

            monkeypatch.setattr(psycopg, "connect", readonly_connect)
            output = tmp_path / "quotes.json"
            assert h10.main(["quotes", "--plan", str(path), "--data-dir", str(directory),
                             "--output", str(output)]) == 0
            result = h10.QuoteFile.model_validate_json(output.read_bytes())
            assert calls == [True]
            assert result.max_tick_id == cap
            for day, slots in expected.items():
                assert {slot: int(q.id) for slot, q in result.quotes[day].items()} == slots
                assert result.quotes[day]["entry"].event_time.hour == offsets[day]
            assert all(q is None for q in result.quotes[date(2020, 3, 10)].values())
            raw = json.loads(output.read_bytes())
            assert all(isinstance(value, str) for value in raw["quotes"]["2020-03-06"]["entry"].values())
        finally:
            writer.rollback()
            writer.execute("DELETE FROM market_ticks WHERE symbol = %s", (symbol,))
            writer.commit()
