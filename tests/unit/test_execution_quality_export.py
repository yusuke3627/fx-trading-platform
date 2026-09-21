"""保存行から観測へ変換するとき、欠測と不完全な履歴を保持する。"""
from copy import deepcopy
from decimal import Decimal

import pytest

from tests.support import T0, at, usdjpy_spec
from trading.backtest.execution_quality_export import build_input, main
from trading.backtest.execution_quality_study import measure
from trading.domain.order import CommandState


@pytest.fixture
def rows():
    return {
        "extracted_at": at(minutes=1),
        "counts": {"commands": 1, "fills": 1, "states": 0, "quotes": 1,
                   "unlinked_command_fills": 0},
        "commands": [{
            "id": "order-1", "symbol": "USDJPY", "side": "BUY",
            "quantity": Decimal(1000), "state": "FILLED", "created_at": T0,
            "updated_at": at(seconds=9), "state_revision": 0,
            "decision_at": at(seconds=1), "broker_request_started_at": at(seconds=2),
        }],
        "states": [],
        "fills": [{
            "id": "fill-1", "execution_command_id": "order-1", "quantity": Decimal(1000),
            "price": Decimal("150.02"), "side": "BUY", "origin": "COMMAND",
            "broker_time": at(hours=3), "received_at": at(seconds=4),
        }],
        "quotes": [{"symbol": "USDJPY", "received_at": at(seconds=1),
                    "bid": Decimal(150), "ask": Decimal("150.01")}],
    }


def convert(rows, **overrides):
    args = {
        "start": T0, "end": at(seconds=10), "basis": "simulated", "instruments": (usdjpy_spec(),),
        "horizons": (Decimal(1),), "quote_max_age": Decimal(2), "provenance": "架空の保存データ",
        "quote_provenance": "テストfixtureの現行quote",
    }
    return build_input(rows, **(args | overrides))


def history(rows):
    path = ["CREATED", "RISK_APPROVED", "READY", "CLAIMED", "SUBMITTING", "FILLED"]
    rows["states"] = [
        {"command_id": "order-1", "state_revision": index, "state": state,
         "changed_at": at(seconds=index), "quantity": Decimal(1000)}
        for index, state in enumerate(path)
    ]
    rows["commands"][0]["state_revision"] = len(path) - 1


def test_legacy_rows_keep_missing_clocks_and_only_measure_supported_slippage(rows):
    data, evidence = convert(rows)
    order = data.orders[0]
    assert not order.history_complete
    assert not order.fills_complete
    assert order.sent_at is None and order.response_at is None and order.input_received_at is None
    assert order.fills[0].executed_at is None
    assert order.fills[0].broker_time == at(hours=3)
    assert order.fills[0].received_at.at == at(seconds=4)
    report = measure(data)
    assert report["orders"][0]["status"] == "ok"
    assert report["orders"][0]["fills"][0]["decision_slippage"]["adverse_pips"] == Decimal(1)
    assert report["orders"][0]["fills"][0]["markouts"][0]["status"] == "missing_execution_timestamp"
    assert report["symbols"][0]["blocked_entries_count"] is None
    assert evidence["exported_orders"] == evidence["source_counts"]["commands"] == 1


@pytest.mark.parametrize("state", list(CommandState))
def test_all_command_states_remain_in_the_denominator(rows, state):
    rows["commands"][0]["state"] = state
    rows["fills"] = []
    data, evidence = convert(rows)
    assert len(data.orders) == 1
    assert data.orders[0].final_state == state
    assert evidence["final_states"] == {state: 1}


def test_complete_journal_can_restore_state_and_quantity_at_past_cutoff(rows):
    history(rows)
    rows["commands"][0]["updated_at"] = at(days=1)
    rows["states"][4]["quantity"] = Decimal(500)
    rows["fills"] = []
    data, _ = convert(rows, end=at(seconds=4))
    order = data.orders[0]
    assert order.history_complete
    assert order.final_state == CommandState.SUBMITTING
    assert order.quantity == 500
    assert len(order.states) == 5


@pytest.mark.parametrize("defect", ["missing_transition", "no_creation", "missing_clock", "invalid_path"])
def test_incomplete_history_is_never_promoted(rows, defect):
    history(rows)
    if defect == "missing_transition":
        del rows["states"][2]
    elif defect == "no_creation":
        del rows["states"][0]
    elif defect == "missing_clock":
        rows["states"][3]["changed_at"] = None
    else:
        rows["states"][1]["state"] = "UNKNOWN"
    data, _ = convert(rows)
    assert not data.orders[0].history_complete
    rows["commands"][0]["updated_at"] = at(days=1)
    with pytest.raises(ValueError, match="終了時点の状態を復元"):
        convert(rows)


def test_unlinked_command_fill_marks_population_incomplete(rows):
    rows["counts"]["unlinked_command_fills"] = 1
    data, evidence = convert(rows)
    assert not data.orders_complete
    assert evidence["source_counts"]["unlinked_command_fills"] == 1


def test_conflicting_received_quotes_are_rejected_instead_of_reordered(rows):
    duplicate = deepcopy(rows["quotes"][0])
    duplicate["ask"] += Decimal(".01")
    rows["quotes"].append(duplicate)
    with pytest.raises(ValueError, match="quote が重複"):
        convert(rows)


def test_unverified_received_prices_are_omitted(rows):
    data, evidence = convert(rows, quote_provenance=None)
    assert data.quotes == ()
    assert not evidence["quotes_included"]
    assert evidence["quote_provenance"] is None
    assert "申告" in evidence["provenance_limitation"]


def test_observed_window_cannot_extend_beyond_database_snapshot(rows):
    with pytest.raises(ValueError, match="抽出snapshot時刻以前"):
        convert(rows, basis="observed_utc", end=at(days=1))


def test_export_never_uses_inherited_default_dsn(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TRADING_DB_DSN", "must-not-connect")
    monkeypatch.delenv("EXPLICIT_TEST_DSN", raising=False)
    result = main([
        "--dsn-env", "EXPLICIT_TEST_DSN", "--start", T0.isoformat(),
        "--end", at(seconds=10).isoformat(), "--time-basis", "simulated",
        "--provenance", "架空データ", "--instruments", str(tmp_path / "unused.json"),
        "--horizon-seconds", "1", "--quote-max-age-seconds", "2",
        "--output-dir", str(tmp_path / "output"),
    ])
    assert result == 2
    assert "環境変数が未設定" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()
