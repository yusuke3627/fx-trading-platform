"""使い捨てDBで H8 wedges の SQL・raw payload・読み取り専用接続を確認する。"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from tests.support import carry_data, carry_plan
from trading.backtest import carry_study as h8
from trading.data.swap.collector import build_snapshot

DSN = os.environ.get("TRADING_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="使い捨てDBの TRADING_DB_DSN が必要")


def test_wedges_seven_pairs_raw_point_as_of_and_time_gap(tmp_path, monkeypatch):
    import psycopg

    from trading.storage.postgres import PostgresEventRepository, PostgresSwapSnapshotRepository

    base_plan = carry_plan()
    suffix = uuid4().hex[:8]
    plan = base_plan.model_copy(update={"currencies": tuple(
        c.model_copy(update={"oanda_symbol": c.oanda_symbol + suffix}) for c in base_plan.currencies
    )})
    data = carry_data(plan)
    for c in plan.currencies:
        data[c.fx_series][date(2020, 7, 1)] = Decimal(100) if not c.oanda_foreign_is_base else Decimal(1)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    data_dir = tmp_path / "data"

    def retrieve(url):
        series = parse_qs(urlparse(url).query)["id"][0]
        return (f"observation_date,{series}\n"
                + "".join(f"{d},{v}\n" for d, v in sorted(data[series].items()))).encode()

    h8.fetch(path, data_dir, retrieve=retrieve)
    as_of = datetime(2020, 7, 1, 12, tzinfo=UTC)
    output = tmp_path / "wedges.json"
    args = ["wedges", "--plan", str(path), "--data-dir", str(data_dir),
            "--as-of", as_of.isoformat(), "--output", str(output)]
    event_ids = []
    symbols = [c.oanda_symbol for c in plan.currencies]
    connect = psycopg.connect
    with connect(DSN) as writer:
        events = PostgresEventRepository(writer)
        swaps = PostgresSwapSnapshotRepository(writer)

        def insert(currency, when, points):
            raw = {"name": currency.oanda_symbol, "point": .01, "swap_mode": 1,
                   "swap_long": points, "swap_short": -points, "swap_rollover3days": 3}
            info = SimpleNamespace(**raw, _asdict=lambda: raw)
            snap, event = build_snapshot(currency.oanda_symbol, info, retrieved_at=when)
            events.insert(event)
            event_ids.append(event.event_id)
            swaps.insert(snap)
            return snap

        calls = []

        def readonly_connect(dsn, **kwargs):
            assert dsn == DSN
            assert kwargs["options"] == "-c default_transaction_read_only=on"
            conn = connect(dsn, **kwargs)
            assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            calls.append(True)
            return conn

        monkeypatch.setattr(psycopg, "connect", readonly_connect)
        try:
            chosen = []
            for i, c in enumerate(plan.currencies):
                insert(c, as_of - timedelta(days=1), 90)
                chosen.append(insert(c, as_of - timedelta(minutes=i), 1))
                insert(c, as_of + timedelta(seconds=1), 80)
            assert h8.main(args) == 0
            result = h8.WedgeFile.model_validate_json(output.read_bytes())
            assert len(calls) == 1
            assert [p.snapshot_id for p in result.pairs] == [s.snapshot_id for s in chosen]
            first = result.pairs[0]
            assert first.W == 7
            assert first.point == Decimal(".01")
            assert first.price == 100
            assert first.theoretical_long == 2
            assert first.observed_long == Decimal("3.65")
            assert first.u_long == 0
            assert first.u_short == Decimal("1.65")
            assert h8.wedge_manifest_path(output).exists()

            # 10分ちょうどは同時記録、1秒でも超えれば失敗する。
            writer.execute("UPDATE swap_snapshots SET known_at = %s WHERE id = %s",
                           (as_of - timedelta(minutes=10), chosen[-1].snapshot_id))
            writer.commit()
            assert h8.main(args[:-1] + [str(tmp_path / "edge.json")]) == 0
            writer.execute("UPDATE swap_snapshots SET known_at = %s WHERE id = %s",
                           (as_of - timedelta(minutes=10, seconds=1), chosen[-1].snapshot_id))
            writer.commit()
            with pytest.raises(ValueError, match="同じ時刻の記録ではない"):
                h8.main(args[:-1] + [str(tmp_path / "gap.json")])
            assert not (tmp_path / "gap.json").exists()
            writer.execute("UPDATE swap_snapshots SET known_at = %s WHERE id = %s",
                           (chosen[-1].known_at, chosen[-1].snapshot_id))
            writer.commit()

            writer.execute("DELETE FROM events WHERE payload_hash = %s", (chosen[0].payload_hash,))
            writer.commit()
            with pytest.raises(ValueError, match="raw payload"):
                h8.main(args[:-1] + [str(tmp_path / "no_raw.json")])
            writer.execute("DELETE FROM swap_snapshots WHERE symbol = %s", (symbols[0],))
            writer.commit()
            with pytest.raises(ValueError, match="スワップの記録がありません"):
                h8.main(args[:-1] + [str(tmp_path / "missing.json")])
        finally:
            writer.rollback()
            writer.execute("DELETE FROM swap_snapshots WHERE symbol = ANY(%s)", (symbols,))
            writer.execute("DELETE FROM events WHERE id = ANY(%s)", (event_ids,))
            writer.commit()
