"""DB なしでも検証できる、同期の境界・固定手順・秘密情報の出力制限。"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

pytest.importorskip("psycopg")
from psycopg import sql

from trading.storage import research_mirror as mirror


@pytest.mark.parametrize("kwargs", [
    {"symbols": ()}, {"symbols": ("",)}, {"symbols": ("USDJPY", "USDJPY")},
    {"chunk_size": 0}, {"chunk_size": -1}, {"max_rows": 0}, {"max_rows": -1},
    {"sleep_seconds": -1}, {"sleep_seconds": float("nan")}, {"sleep_seconds": float("inf")},
])
def test_invalid_options_fail_before_connecting(kwargs):
    with pytest.raises(mirror.MirrorError):
        mirror.MirrorOptions(**kwargs)


def test_pin_reads_ceiling_then_settles_each_symbol_in_a_separate_transaction():
    calls = []
    source = MagicMock()

    def execute(query, params=()):
        calls.append((query, params))
        cursor = MagicMock()
        cursor.fetchone.return_value = (42,)
        return cursor

    @contextmanager
    def transaction():
        calls.append("begin")
        yield
        calls.append("commit")

    source.execute.side_effect = execute
    source.transaction.side_effect = transaction
    assert mirror._pin_ticks(source, ("USDJPY", "EURUSD")) == 42
    assert calls[0][0] == "SELECT max(id) FROM public.market_ticks"
    assert calls[1] == "begin"
    assert calls[3] == ("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                        (mirror._TICK_ADVISORY_LOCK_CLASS_ID, "USDJPY"))
    assert calls[4:6] == ["commit", "begin"]
    assert calls[7][1] == (mirror._TICK_ADVISORY_LOCK_CLASS_ID, "EURUSD")
    assert calls[8] == "commit"


def test_copy_streams_raw_text_blocks_and_checks_server_counts():
    source, target = MagicMock(), MagicMock()
    reader = source.cursor.return_value.__enter__.return_value
    writer = target.cursor.return_value.__enter__.return_value
    blocks = [b"1\tline\\nvalue\n", b"2\t\\N\n"]
    reader.copy.return_value.__enter__.return_value.__iter__.return_value = iter(blocks)
    reader.rowcount, writer.rowcount = 2, 1
    with pytest.raises(mirror.MirrorError, match="行数"):
        mirror._copy_table(source, target, "events", ["id", "source"],
                           sql.Composed([sql.SQL("SELECT id, source FROM public.events")]))
    incoming = writer.copy.return_value.__enter__.return_value
    assert [call.args[0] for call in incoming.write.call_args_list] == blocks


def test_unmarked_database_is_rejected():
    target = MagicMock()
    target.execute.return_value.fetchone.return_value = (None,)
    with pytest.raises(mirror.MirrorError, match="印"):
        mirror._require_marker(target)


def test_same_database_is_rejected_when_target_holds_lock():
    source = MagicMock()
    source.execute.return_value.fetchone.return_value = (False,)
    with pytest.raises(mirror.MirrorError, match="同じ DB"):
        mirror._require_distinct_database(source)
    assert source.execute.call_count == 1


@pytest.mark.parametrize("error", [
    mirror.psycopg.OperationalError("postgresql://fake:private-password@invalid/db"),
    ValueError("private-password"),
])
def test_cli_never_prints_connection_exception_or_dsn(monkeypatch, capsys, error):
    monkeypatch.setenv("MIRROR_TEST_TARGET", "postgresql://fake:private-password@invalid/db")
    monkeypatch.setattr(mirror, "initialize", MagicMock(side_effect=error))
    assert mirror.main(["init", "--target-dsn-env", "MIRROR_TEST_TARGET"]) == 1
    output = capsys.readouterr()
    assert "private-password" not in output.out + output.err
    assert "postgresql://" not in output.out + output.err
    assert json.loads(output.out)["status"] == "failed"


def test_cli_does_not_fall_back_to_trading_dsn(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_DB_DSN", "must-not-connect")
    monkeypatch.delenv("MISSING_MIRROR_DSN", raising=False)
    connect = MagicMock()
    monkeypatch.setattr(mirror, "_connect", connect)
    assert mirror.main(["init", "--target-dsn-env", "MISSING_MIRROR_DSN"]) == 1
    connect.assert_not_called()
    assert "must-not-connect" not in str(capsys.readouterr())


def test_cli_summary_contains_only_committed_counts(monkeypatch, capsys):
    monkeypatch.setenv("MIRROR_TEST_TARGET", "fake-target")
    monkeypatch.setenv("MIRROR_TEST_SOURCE", "fake-source")

    def interrupted(source, target, options, *, report):
        report.ceiling = 900
        report.rows["market_ticks"] = 5
        raise KeyboardInterrupt

    monkeypatch.setattr(mirror, "synchronize", interrupted)
    assert mirror.main(["sync", "--target-dsn-env", "MIRROR_TEST_TARGET",
                        "--source-dsn-env", "MIRROR_TEST_SOURCE"]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "interrupted"
    assert result["rows"]["market_ticks"] == 5
    assert result["ceiling"] == 900
    assert result["elapsed_seconds"] >= 0
    assert result["rows_per_second"] > 0


def test_cli_argument_errors_do_not_echo_values(capsys):
    with pytest.raises(SystemExit) as error:
        mirror.main(["sync", "--source-dsn", "postgresql://fake:private-password@invalid/db"])
    assert error.value.code == 2
    assert "private-password" not in str(capsys.readouterr())
