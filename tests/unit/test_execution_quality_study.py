"""合成観測で計測の境界・母数・時計と単位の分離を確認する。"""
from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.support import at
from trading.backtest.execution_quality_study import StudyInput, main, markdown, measure

FIXTURE = Path(__file__).parents[1] / "fixtures/execution_quality/synthetic.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def stamp(seconds, basis="simulated"):
    return {"at": at(seconds=seconds).isoformat(), "basis": basis}


def report(payload):
    return measure(StudyInput.model_validate(payload))


def row(result, order_id="buy-split"):
    return next(o for o in result["orders"] if o["order_id"] == order_id)


def mark(result, symbol="USDJPY", horizon="1", basis="simulated"):
    summary = next(s for s in result["symbols"] if s["symbol"] == symbol)
    return next(m for m in summary["markouts"]
                if m["horizon_seconds"] == Decimal(horizon) and m["basis"] == basis)


def test_all_orders_remain_in_population_and_symbols_keep_units(payload):
    result = report(payload)
    assert result["orders_total"] == 6
    usd, eur = result["symbols"]
    assert usd["orders_total"] == 5
    assert usd["final_states"] == {
        "FILLED": 1, "EXPIRED": 1, "REJECTED": 1, "UNKNOWN": 1, "PARTIAL_FILL": 1,
    }
    assert usd["orders_without_known_fills"] == 3
    assert usd["latencies_seconds"]["send_to_response"]["orders_compared"] == 3
    assert usd["latencies_seconds"]["send_to_response"]["coverage_over_orders"] == Decimal(".6")
    assert mark(result)["orders_compared"] == 2
    assert mark(result)["coverage_over_orders"] == Decimal(".4")
    assert (usd["pip_size"], usd["base_currency"], usd["quote_currency"]) == (
        Decimal(".01"), "USD", "JPY",
    )
    assert (eur["pip_size"], eur["base_currency"], eur["quote_currency"]) == (
        Decimal(".0001"), "EUR", "USD",
    )


def test_latency_uses_local_execution_and_receipt_as_distinct_observations(payload):
    buy = row(report(payload))
    assert buy["latencies"]["send_to_first_execution"]["seconds"] == Decimal(1)
    assert buy["latencies"]["send_to_first_fill_received"]["seconds"] == Decimal(2)
    assert buy["latencies"]["send_to_response"]["seconds"] == Decimal(".2")
    assert buy["fills"][1]["send_to_execution"]["seconds"] == Decimal(3)
    assert buy["fills"][1]["send_to_fill_received"]["seconds"] == Decimal(4)
    # 原 broker 時計は 3 時間先でも、上記の時間差には使わない。
    assert "03:00:02" in buy["fills"][0]["broker_time"]


def test_buy_sell_markout_uses_fill_horizon_and_executable_side(payload):
    result = report(payload)
    buy = row(result)["fills"][0]
    sell = row(result, "sell")["fills"][0]
    assert buy["markouts"][0]["executable_pips"] == Decimal(1)
    assert buy["markouts"][0]["mid_pips"] == Decimal(2)
    assert buy["markouts"][0]["executable_quote_amount"] == Decimal(4)
    assert buy["markouts"][1]["executable_pips"] == Decimal(-2)
    assert sell["markouts"][0]["executable_pips"] == Decimal(2)
    assert sell["markouts"][1]["executable_pips"] == Decimal(-5)
    assert buy["decision_slippage"]["adverse_pips"] == Decimal(1)
    assert sell["decision_slippage"]["adverse_pips"] == Decimal(1)


def test_partial_fills_are_quantity_weighted(payload):
    payload["orders"] = [payload["orders"][0]]
    payload["blocked_entries"] = []
    metric = mark(report(payload))
    assert metric["compared"] == 2
    assert metric["compared_quantity"] == Decimal(1000)
    assert metric["weighted_executable_pips"] == Decimal("2.8")


def test_slippage_aggregates_comparable_quantity_with_coverage_for_order_and_symbol(payload):
    result = report(payload)
    usd, eur = result["symbols"]
    metric = next(m for m in usd["decision_slippage"] if m["basis"] == "simulated")
    assert metric["compared_quantity"] == Decimal(1400)
    assert metric["weighted_adverse_pips"] == Decimal(13) / 7
    assert metric["orders_compared"] == 2 and metric["orders_total"] == 5
    assert metric["coverage_over_orders"] == Decimal(".4")
    assert metric["statuses"] == {"ok": 3}
    buy = next(m for m in row(result)["decision_slippage"] if m["basis"] == "simulated")
    assert buy["weighted_adverse_pips"] == Decimal("2.2")
    sell = next(m for m in eur["decision_slippage"] if m["basis"] == "simulated")
    assert sell["weighted_adverse_pips"] == Decimal(1)
    observed = next(m for m in usd["decision_slippage"] if m["basis"] == "observed_utc")
    assert observed["weighted_adverse_pips"] is None
    assert observed["statuses"] == {"other_basis": 3}
    assert "判断時の滑りの由来" in markdown(result)


def test_missing_slippage_is_not_weighted_as_zero(payload):
    payload["orders"][0]["fills"][0]["executed_at"]["basis"] = "reconstructed"
    result = report(payload)
    metric = next(m for m in row(result)["decision_slippage"] if m["basis"] == "simulated")
    assert metric["compared_quantity"] == Decimal(600)
    assert metric["weighted_adverse_pips"] == Decimal(3)
    assert metric["statuses"] == {"missing_or_mixed_fill_basis": 1, "ok": 1}
    payload["quotes"] = []
    metric = next(m for m in row(report(payload))["decision_slippage"] if m["basis"] == "simulated")
    assert metric["weighted_adverse_pips"] is None
    assert metric["compared"] == 0
    assert metric["coverage_over_orders"] == Decimal(0)


def test_quote_selection_never_moves_future_quote_backwards_and_checks_freshness(payload):
    # t=3 の quote を取り除く。t=3.001 の好条件 quote は t=3 の評価には使えない。
    payload["quotes"] = [q for q in payload["quotes"] if q["observed_at"] != stamp(3)]
    payload["quotes"].append({"symbol": "USDJPY", "observed_at": stamp(3.001),
                              "bid": "110", "ask": "110.02"})
    payload["quote_max_age_seconds"] = "2.5"
    first = row(report(payload))["fills"][0]["markouts"][0]
    assert first["status"] == "ok"
    assert first["executable_pips"] == Decimal(-3)
    payload["quote_max_age_seconds"] = "2.499999"
    assert row(report(payload))["fills"][0]["markouts"][0]["status"] == "stale_quote"


def test_missing_execution_clock_does_not_fall_back_to_broker_or_receipt(payload):
    payload["orders"][0]["fills"][0]["executed_at"] = None
    buy = row(report(payload))
    assert buy["latencies"]["send_to_first_execution"]["status"] == "incomplete_fill_timestamps"
    assert buy["latencies"]["send_to_first_fill_received"]["seconds"] == Decimal(2)
    assert buy["fills"][0]["markouts"][0]["status"] == "missing_execution_timestamp"
    assert buy["fills"][0]["markouts"][0]["executable_pips"] is None


def test_provenance_is_not_pooled_or_subtracted_across_clocks(payload):
    payload["orders"][0]["response_at"]["basis"] = "observed_utc"
    # 注文単位で復元時刻の標本を作り、シミュレーション値と分ける。
    reconstructed = deepcopy(payload["orders"][3])
    reconstructed["order_id"] = "reconstructed-reject"
    for name in ("created_at", "input_received_at", "decision_at", "sent_at", "response_at"):
        reconstructed[name]["basis"] = "reconstructed"
    for state in reconstructed["states"]:
        state["at"]["basis"] = "reconstructed"
    payload["orders"].append(reconstructed)
    result = report(payload)
    assert row(result)["latencies"]["send_to_response"]["status"] == "mixed_basis"
    metric = result["symbols"][0]["latencies_seconds"]["send_to_response"]
    assert metric["by_basis"]["observed_utc"]["count"] == 0
    assert metric["by_basis"]["reconstructed"]["count"] == 1
    assert metric["by_basis"]["simulated"]["count"] == 2


def test_unknown_and_nonterminal_partial_integrate_received_unfilled_quantity(payload):
    result = report(payload)
    intervals = row(result)["pending"]["intervals"]
    assert [(i["state"], i["seconds"], i["remaining_quantity_seconds"]) for i in intervals] == [
        ("UNKNOWN", Decimal("1.5"), Decimal(1500)),
        ("PARTIAL_FILL", Decimal(2), Decimal(1200)),
    ]
    unknown = row(result, "unknown")["pending"]["intervals"][0]
    assert unknown["right_censored"] is True
    assert unknown["remaining_quantity_seconds"] == Decimal(8000)
    partial = row(result, "partial-open")["pending"]["intervals"][0]
    assert partial["right_censored"] is True
    assert partial["remaining_quantity_seconds"] == Decimal(4200)
    assert row(result)["fills"][0]["markouts"][2]["status"] == "right_censored"


def test_partial_terminal_evidence_ends_interval_without_assuming_every_partial_is_terminal(payload):
    partial = payload["orders"][-1]
    partial["states"].append({"state": "PARTIAL_FILL", "at": stamp(6),
                              "terminal_evidence": "合成 broker 終端記録: 残数量失効"})
    interval = row(report(payload), "partial-open")["pending"]["intervals"][0]
    assert interval["right_censored"] is False
    assert interval["seconds"] == Decimal(3)
    assert interval["remaining_quantity_seconds"] == Decimal(1800)


def test_fills_received_while_unknown_reduce_quantity_integral(payload):
    buy = payload["orders"][0]
    buy["states"] = buy["states"][:6] + [{"state": "FILLED", "at": stamp(5)}]
    interval = row(report(payload))["pending"]["intervals"][0]
    assert interval["seconds"] == Decimal("3.5")
    assert interval["remaining_quantity_seconds"] == Decimal(2700)


@pytest.mark.parametrize(("mutation", "expected"), [
    ("history", "incomplete_state_history"),
    ("fills", "incomplete_fill_history"),
    ("receipt", "missing_fill_receipt_timestamp"),
])
def test_missing_history_cannot_be_reconstructed_from_final_state(payload, mutation, expected):
    partial = payload["orders"][-1]
    if mutation == "history":
        partial["history_complete"] = False
    elif mutation == "fills":
        partial["fills_complete"] = False
    else:
        partial["fills"][0]["received_at"] = None
    pending = row(report(payload), "partial-open")["pending"]
    assert pending == {"status": expected, "intervals": []}


def test_truncated_state_history_preserves_independent_latency_and_markout(payload):
    buy = payload["orders"][0]
    buy["history_complete"] = False
    buy["states"].pop()
    result = report(payload)
    measured = row(result)
    assert measured["status"] == "ok"
    assert measured["pending"]["status"] == "incomplete_state_history"
    assert measured["latencies"]["send_to_first_execution"]["seconds"] == Decimal(1)
    assert measured["fills"][0]["markouts"][0]["executable_pips"] == Decimal(1)
    assert mark(result)["compared"] == 3
    buy["history_complete"] = True
    assert "final_state_mismatch" in row(report(payload))["errors"]


@pytest.mark.parametrize("partial_time", [-1, 1])
def test_mixed_state_clocks_only_exclude_pending_intervals(payload, partial_time):
    payload["window_start"] = stamp(-10)
    buy = payload["orders"][0]
    buy["states"][6]["at"] = stamp(partial_time, "reconstructed")
    measured = row(report(payload))
    assert measured["status"] == "ok"
    assert measured["pending"]["status"] == "incomplete"
    assert all(i["status"] == "mixed_basis" for i in measured["pending"]["intervals"])
    assert all(i["seconds"] is None for i in measured["pending"]["intervals"])
    assert measured["latencies"]["send_to_first_execution"]["seconds"] == Decimal(1)
    assert measured["fills"][0]["markouts"][0]["executable_pips"] == Decimal(1)
    buy["states"][6]["at"]["basis"] = "simulated"
    assert "state_history_not_ordered" in row(report(payload))["errors"]


@pytest.mark.parametrize("unknown_time", [4, 7])
def test_state_order_is_checked_across_intervening_other_basis(payload, unknown_time):
    partial = payload["orders"][-1]
    partial["final_state"] = "UNKNOWN"
    partial["states"] = partial["states"][:-1] + [
        {"state": "ACKNOWLEDGED", "at": stamp(5)},
        {"state": "PARTIAL_FILL", "at": stamp(6, "reconstructed")},
        {"state": "UNKNOWN", "at": stamp(unknown_time)},
    ]
    measured = row(report(payload), "partial-open")
    if unknown_time == 4:
        assert "state_history_not_ordered" in measured["errors"]
        assert measured["pending"] == {"status": "invalid_order", "intervals": []}
    else:
        assert measured["status"] == "ok"
        assert measured["pending"]["intervals"][-1]["remaining_quantity_seconds"] == Decimal(1800)


@pytest.mark.parametrize("execution_missing", [False, True])
def test_fill_before_decision_cannot_be_used_for_slippage(payload, execution_missing):
    buy = payload["orders"][0]
    buy["decision_at"] = stamp(4)
    buy["sent_at"]["basis"] = "reconstructed"
    if execution_missing:
        buy["fills"][0]["executed_at"] = None
    measured = row(report(payload))
    assert measured["status"] == "ok"
    assert measured["fills"][0]["decision_slippage"]["status"] == "invalid_chronology"
    assert measured["fills"][0]["decision_slippage"]["adverse_pips"] is None
    assert measured["fills"][1]["decision_slippage"]["status"] == "ok"
    if not execution_missing:
        assert measured["fills"][0]["markouts"][0]["executable_pips"] == Decimal(1)


@pytest.mark.parametrize("quote_status", ["missing", "stale"])
def test_fill_clock_inconsistency_is_reported_even_without_a_usable_quote(payload, quote_status):
    buy = payload["orders"][0]
    buy["decision_at"] = stamp(4)
    buy["sent_at"]["basis"] = "reconstructed"
    if quote_status == "missing":
        payload["quotes"] = []
    else:
        payload["quote_max_age_seconds"] = "0"
    measured = row(report(payload))
    assert measured["fills"][0]["decision_slippage"]["status"] == "invalid_chronology"
    assert measured["fills"][0]["decision_slippage"]["adverse_pips"] is None
    assert measured["fills"][1]["decision_slippage"]["status"] == f"{quote_status}_quote"


def test_unsorted_quotes_are_indexed_without_mixing_symbol_or_basis(payload):
    expected = report(payload)
    payload["quotes"].reverse()
    different_basis = deepcopy(payload["quotes"])
    for quote in different_basis:
        quote["observed_at"]["basis"] = "observed_utc"
        quote["bid"], quote["ask"] = "999", "1000"
    payload["quotes"].extend(different_basis)
    assert report(payload) == expected


def test_window_filters_utc_values_but_censor_duration_requires_matching_basis(payload):
    unknown = payload["orders"][-2]
    for name in ("created_at", "input_received_at", "decision_at", "sent_at"):
        unknown[name]["basis"] = "observed_utc"
    for state in unknown["states"]:
        state["at"]["basis"] = "observed_utc"
    measured = row(report(payload), "unknown")
    assert measured["status"] == "ok"
    assert measured["pending"]["intervals"][0]["status"] == "mixed_basis"
    assert measured["pending"]["intervals"][0]["seconds"] is None
    unknown["states"][-1]["at"] = stamp(11, "observed_utc")
    assert "timestamp_outside_window" in row(report(payload), "unknown")["errors"]


def test_pending_mixed_clock_has_no_fabricated_zero_quantity(payload):
    payload["orders"][-1]["fills"][0]["received_at"]["basis"] = "reconstructed"
    interval = row(report(payload), "partial-open")["pending"]["intervals"][0]
    assert interval["status"] == "mixed_basis"
    assert interval["remaining_quantity_seconds"] is None


def test_blocked_attempts_count_once_on_attempt_symbol_not_related_orders(payload):
    result = report(payload)
    usd, eur = result["symbols"]
    assert usd["blocked_entries_count"] == 0
    assert eur["blocked_entries_count"] == 1
    payload["blocked_entries_complete"] = False
    eur = report(payload)["symbols"][1]
    assert eur["blocked_entries_count"] is None
    assert eur["blocked_entries_observed"] == 1


@pytest.mark.parametrize("mutation", ["overfill", "execution_after_receipt", "bad_transition"])
def test_inconsistent_order_is_retained_but_not_in_metric_samples(payload, mutation):
    buy = payload["orders"][0]
    if mutation == "overfill":
        buy["fills"][0]["quantity"] = "9999"
    elif mutation == "execution_after_receipt":
        buy["fills"][0]["executed_at"] = stamp(4)
    else:
        buy["states"][5]["state"] = "READY"
    result = report(payload)
    assert result["orders_total"] == 6
    assert result["symbols"][0]["invalid_orders"] == 1
    assert row(result)["fills"] == []
    metric = mark(result)
    assert metric["fills_total"] == 3
    assert metric["statuses"]["invalid_order"] == 2
    assert metric["coverage_over_orders"] == Decimal(".2")


@pytest.mark.parametrize("mutation", ["naive", "nan", "duplicate_quote", "duplicate_fill",
                                     "unknown_symbol", "exit_block", "submicrosecond"])
def test_input_boundary_rejects_ambiguous_or_invalid_observations(payload, mutation):
    if mutation == "naive":
        payload["orders"][0]["created_at"]["at"] = "2026-08-13T00:00:00"
    elif mutation == "nan":
        payload["orders"][0]["fills"][0]["price"] = "NaN"
    elif mutation == "duplicate_quote":
        payload["quotes"].append(payload["quotes"][0])
    elif mutation == "duplicate_fill":
        payload["orders"][0]["fills"][1]["fill_id"] = "buy-1"
    elif mutation == "unknown_symbol":
        payload["orders"][0]["symbol"] = "TESTPAIR"
    elif mutation == "exit_block":
        payload["blocked_entries"][0]["action"] = "CLOSE"
    else:
        payload["horizons_seconds"] = ["0.0000001"]
    with pytest.raises(ValidationError):
        StudyInput.model_validate(payload)


def test_cli_fixture_writes_reviewable_json_markdown_without_external_services(tmp_path):
    output = tmp_path / "report"
    run = subprocess.run([sys.executable, "-m", "trading.backtest.execution_quality_study",
                          "--input", str(FIXTURE), "--output-dir", str(output)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    result = json.loads((output / "report.json").read_text())
    text = (output / "report.md").read_text()
    assert len(result["input_sha256"]) == 64
    assert result["symbols"][0]["latencies_seconds"]["send_to_response"]["coverage_over_orders"] == "0.6"
    assert "入力注文: 6 件" in text
    assert "USD 数量×秒" in text and "EUR 数量×秒" in text
    assert "right_censored" in text
    assert "実口座・実測データではない" in text
    assert "拘束資金ではありません" in text
    assert main(["--input", str(FIXTURE), "--output-dir", str(output)]) == 2


def test_cli_invalid_order_returns_nonzero_with_report_and_invalid_contract_without_report(payload, tmp_path):
    source = tmp_path / "input.json"
    payload["orders"][0]["fills"][0]["quantity"] = "2000"
    source.write_text(json.dumps(payload))
    output = tmp_path / "invalid-order"
    assert main(["--input", str(source), "--output-dir", str(output)]) == 1
    assert "overfilled" in (output / "report.md").read_text()
    source.write_text("{}")
    output = tmp_path / "invalid-contract"
    assert main(["--input", str(source), "--output-dir", str(output)]) == 2
    assert not output.exists()


def test_missing_quotes_and_empty_population_report_absence_not_perfect_execution(payload):
    payload["quotes"] = []
    result = report(payload)
    assert mark(result)["statuses"]["missing_quote"] == 3
    assert mark(result)["weighted_executable_pips"] is None
    assert "missing_quote" in markdown(result)
    payload["orders"] = []
    payload["blocked_entries"] = []
    result = report(payload)
    assert mark(result)["coverage_over_orders"] is None
    assert result["orders_total"] == 0
