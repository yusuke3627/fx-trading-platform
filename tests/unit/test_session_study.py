"""時間帯診断の時計・費用・欠測・探索範囲を検証する。"""
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.support import usdjpy_spec
from trading.backtest.session_study import (
    Exclusion,
    Plan,
    interval,
    main,
    paired_observation,
    run,
    sample_quotes,
    windows,
)


def plan(**overrides):
    values = {
        "schema_version": "session_study_v1",
        "population_description": "架空価格による単体検証",
        "instrument": usdjpy_spec(),
        "from_date": "2026-03-09", "to_date": "2026-03-10",
        "basis": "observed_utc", "quotes_complete": True,
        "calendar_source": "架空の空カレンダー",
        "calendar_start": "2026-03-08T00:00:00Z",
        "calendar_end": "2026-04-02T00:00:00Z",
        "quote_max_age_seconds": "2", "latency_ms": "0",
        "slippage_pips_per_side": "0.3", "commission_pips_round_trip": "0",
        "server_ahead_of_ny_hours": 7,
        "minimum_effect_pips": "0.1", "planned_days": 2,
        "sample_size_rationale": "単体テスト用の最小件数。実測の検出力を表さない",
        "block_days": 1, "bootstrap_replicates": 2000, "seed": 7,
    }
    values.update(overrides)
    return Plan.model_validate(values)


def quote(at, bid="150", *, basis="observed_utc", symbol="USDJPY"):
    return {"symbol": symbol, "observed_at": {"at": at.isoformat(), "basis": basis},
            "bid": str(bid), "ask": str(Decimal(bid) + Decimal("0.02"))}


def quotes_file(tmp_path, p):
    rows = []
    for row in windows(p):
        for at, bid in zip(row["times"], ("150", "150.01", "150.04"), strict=True):
            rows.append(quote(at, bid, basis=p.basis))
    rows.sort(key=lambda q: q["observed_at"]["at"])
    path = tmp_path / "quotes.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_fixed_windows_follow_each_local_dst_and_exclude_weekends():
    before = windows(plan(from_date="2026-03-06", to_date="2026-03-06"))
    us_dst = windows(plan(from_date="2026-03-09", to_date="2026-03-09"))
    uk_dst = windows(plan(from_date="2026-03-30", to_date="2026-03-30"))
    assert [r["times"][1].hour for r in before] == [0, 8, 13]
    assert [r["times"][1].hour for r in us_dst] == [0, 8, 12]
    assert [r["times"][1].hour for r in uk_dst] == [0, 7, 12]
    assert all(r["week_open"] for r in us_dst)
    assert windows(plan(from_date="2026-03-07", to_date="2026-03-08")) == []
    assert "TOKYO" in us_dst[1]["active_sessions"]


def test_before_after_bid_ask_slippage_and_signs(tmp_path):
    p = plan()
    result = run(p, quotes_file(tmp_path, p))
    row = result["observations"][0]["directions"]
    assert row["LONG"]["before"]["net_pips"] == Decimal("-1.6")
    assert row["LONG"]["after"]["net_pips"] == Decimal("0.4")
    assert row["SHORT"]["after"]["net_pips"] == Decimal("-5.6")
    assert row["LONG"]["difference_net_pips"] == Decimal(2)
    assert result["summary"][0]["status"] == "confirmation_candidate"
    assert result["summary"][1]["status"] == "stop"
    assert result["split"] == "exploratory"
    assert result["profitability_established"] is False
    assert result["family_size"] == 18


def test_future_quotes_are_not_pulled_back_and_latency_moves_all_endpoints(tmp_path):
    p = plan(latency_ms="150")
    target = windows(p)[0]["times"][1]
    assert target.microsecond == 150000
    source = tmp_path / "quotes.jsonl"
    source.write_text("\n".join(json.dumps(q) for q in (
        quote(target - timedelta(seconds=1), "150"),
        quote(target + timedelta(microseconds=1), "190"),
    )))
    selected, _ = sample_quotes(source, p, [target])
    assert selected[target].bid == Decimal(150)


@pytest.mark.parametrize("problem", ["missing", "stale", "incomplete", "calendar", "outage"])
def test_missing_evidence_holds_even_with_profitable_remaining_days(tmp_path, problem):
    p = plan()
    path = quotes_file(tmp_path, p)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if problem == "missing":
        rows.pop(0)
    elif problem == "stale":
        rows[0]["observed_at"]["at"] = "2026-03-08T22:59:57Z"
    elif problem == "incomplete":
        p = plan(quotes_complete=False)
    elif problem == "calendar":
        p = plan(calendar_start="2026-03-09T00:00:00Z")
    else:
        p = plan(exclusions=[{"start": "2026-03-08T23:00:00Z", "end": "2026-03-09T01:00:00Z",
                              "reason": "data_outage"}])
    path.write_text("\n".join(json.dumps(row) for row in rows))
    result = run(p, path)
    assert result["summary"][0]["status"] == "hold"
    assert result["summary"][0]["eligible_days"] == 2


def test_declared_holiday_is_excluded_without_calling_absent_quotes_missing(tmp_path):
    p = plan(exclusions=[{"start": "2026-03-08T23:00:00Z", "end": "2026-03-09T01:01:00Z",
                          "reason": "holiday"}])
    selected = {t: None for t in windows(p)[0]["times"]}
    result = paired_observation(windows(p)[0], p, selected)
    assert result["excluded_reasons"] == ["holiday"]


def test_rollover_is_excluded_before_claiming_cost_complete(tmp_path):
    # NY 19時（UTC23時）にrolloverを持つ仮想server。
    p = plan(server_ahead_of_ny_hours=5)
    result = run(p, quotes_file(tmp_path, p))
    assert "rollover_excluded" in result["observations"][0]["excluded_reasons"]


@pytest.mark.parametrize("kind", ["duplicate", "reversed", "wrong_basis", "wrong_symbol", "crossed"])
def test_rejects_invalid_quote_source(tmp_path, kind):
    p = plan()
    path = quotes_file(tmp_path, p)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if kind == "duplicate":
        rows.insert(0, rows[0])
    elif kind == "reversed":
        rows.reverse()
    elif kind == "wrong_basis":
        rows[0]["observed_at"]["basis"] = "reconstructed"
    elif kind == "wrong_symbol":
        rows[0]["symbol"] = "EURUSD"
    else:
        rows[0]["ask"] = "100"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError):
        run(p, path)


def test_quote_file_end_is_not_extended_as_if_recording_continued(tmp_path):
    p = plan()
    at = datetime(2026, 3, 9, tzinfo=UTC)
    path = tmp_path / "short.jsonl"
    path.write_text(json.dumps(quote(at)))
    selected, _ = sample_quotes(path, p, [at, at + timedelta(milliseconds=1)])
    assert selected[at] is not None
    assert selected[at + timedelta(milliseconds=1)] is None


def test_sample_size_gate_and_minimum_effect(tmp_path):
    p = plan(planned_days=3)
    path = quotes_file(tmp_path, p)
    assert run(p, path)["summary"][0]["status"] == "hold"
    p = plan(minimum_effect_pips="1")
    assert run(p, path)["summary"][0]["status"] == "inconclusive"


def test_opposing_daily_results_remain_inconclusive(tmp_path):
    p = plan()
    path = quotes_file(tmp_path, p)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[11]["bid"], rows[11]["ask"] = "149.99", "150.01"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    result = run(p, path)["summary"][0]
    assert result["status"] == "inconclusive"
    assert result["metrics"]["after"]["interval"][0] < 0
    assert result["metrics"]["after"]["interval"][1] > 0


def test_longer_blocks_preserve_uncertainty_in_clustered_days():
    values = [Decimal(-1)] * 30 + [Decimal(1)] * 30
    independent = interval(values, plan(planned_days=60))
    clustered = interval(values, plan(planned_days=60, block_days=5))
    assert clustered[0] < independent[0] < 0 < independent[1] < clustered[1]
    assert clustered == interval(values, plan(planned_days=60, block_days=5))


def test_synthetic_prices_cannot_establish_a_research_candidate(tmp_path):
    p = plan(basis="simulated")
    result = run(p, quotes_file(tmp_path, p))
    assert all(row["status"] == "synthetic_only" for row in result["summary"])


def test_commission_changes_absolute_edge_not_the_same_cost_paired_difference(tmp_path):
    p = plan(commission_pips_round_trip="0.5")
    result = run(p, quotes_file(tmp_path, p))
    assert result["summary"][0]["metrics"]["after"]["mean_net_pips"] == Decimal("-0.1")
    assert result["summary"][0]["metrics"]["difference"]["mean_net_pips"] == Decimal(2)
    assert result["summary"][0]["status"] == "stop"


def test_rejects_naive_calendar_and_uninformative_block_size():
    with pytest.raises(ValidationError):
        plan(calendar_start="2026-03-01T00:00:00")
    with pytest.raises(ValidationError):
        plan(block_days=2, planned_days=2)
    with pytest.raises(ValidationError):
        Exclusion(start="2026-03-01T00:00:00Z", end="2026-03-01T00:00:00Z", reason="macro")


@pytest.mark.parametrize("pip_size", ["NaN", "Infinity", "-Infinity", "0", "-0.01"])
def test_cli_rejects_nonfinite_or_nonpositive_pip_size_before_analysis(tmp_path, pip_size):
    values = plan().model_dump(mode="json")
    values["instrument"]["pip_size"] = pip_size
    with pytest.raises(ValidationError):
        Plan.model_validate(values)
    source = tmp_path / "invalid-plan.json"
    source.write_text(json.dumps(values))
    output = tmp_path / "result"
    assert main(["--plan", str(source), "--quotes", str(tmp_path / "unused.jsonl"),
                 "--output-dir", str(output)]) == 2
    assert not output.exists()


def test_cli_keeps_input_plan_reports_provenance_and_refuses_overwrite(tmp_path):
    p = plan()
    source = tmp_path / "plan.json"
    source.write_text(p.model_dump_json())
    output = tmp_path / "result"
    args = ["--plan", str(source), "--quotes", str(quotes_file(tmp_path, p)),
            "--output-dir", str(output)]
    assert main(args) == 0
    report = json.loads((output / "report.json").read_text())
    assert len(report["quote_source"]["sha256"]) == 64
    assert report["plan"]["sample_size_rationale"] == p.sample_size_rationale
    assert (output / "plan.json").read_bytes() == source.read_bytes()
    original = (output / "report.json").read_bytes()
    assert main(args) == 2
    assert (output / "report.json").read_bytes() == original
