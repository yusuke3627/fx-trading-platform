"""政策文書抽出の測定規則。APIや原文サイトには接続せず、架空の応答を使う。"""
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from trading.data.policy import extraction_study as study
from trading.data.policy.meetings import PolicyMeeting


def meeting(bank="FED", day=1, **facts):
    return PolicyMeeting(
        bank=bank, decision_date=date(2024, 2, day),
        statement_published_at=datetime(2024, 2, day, tzinfo=UTC), verified=True,
        source_uri=f"https://sources.example/{bank}/statement-{day}.html", **facts,
    )


def case(day=1, missing=None, fetch_error=None, **facts):
    return study.Case(
        meeting=meeting(day=day, **facts),
        missing_inputs={"inflation_forecast_change": "比較資料なし"} if missing is None else missing,
        fetch_error=fetch_error,
    )


def facts(**changes):
    return {"rate_change_bp": 0, "hawkish_dissents": 0, "dovish_dissents": 0,
            "inflation_forecast_change": None, "explicit_future_hike_language": False} | changes


def response(target, values=None, *, usage=True, status="completed"):
    body = {
        "status": status,
        "output": [{"type": "reasoning"}, {"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(
                        facts() if values is None else values
                    )}]}],
    }
    if usage:
        body["usage"] = {"input_tokens": 100, "output_tokens": 20,
                         "input_tokens_details": {"cached_tokens": 50}}
    return {"custom_id": target.custom_id, "error": None,
            "response": {"status_code": 200, "body": body}}


def summarize(cases, records, status="completed"):
    return study.summarize(
        cases, study.parse_outputs(["\n".join(json.dumps(row) for row in records)], cases),
        status, study.DEFAULT_MODEL, study.LUNA_PRICES,
    )


def test_field_matching_uses_custom_id_and_reports_each_mismatch():
    first = case(rate_change_bp=25, hawkish_dissents=1, explicit_future_hike_language=True)
    second = case(day=2)
    report = summarize([first, second], [response(second), response(first)])
    assert report["fields"]["rate_change_bp"]["match_rate"] == 0.5
    assert report["fields"]["dovish_dissents"]["match_rate"] == 1
    assert report["mismatches"] == [
        {"bank": "FED", "decision_date": "2024-02-01", "field": field,
         "expected": expected, "extracted": extracted}
        for field, expected, extracted in [
            ("rate_change_bp", 25, 0), ("hawkish_dissents", 1, 0),
            ("explicit_future_hike_language", True, False),
        ]
    ]


def test_input_insufficiency_is_not_a_mismatch_or_a_zero_percent_accuracy():
    insufficient = case(inflation_forecast_change=1)
    sufficient = case(day=2, missing={}, inflation_forecast_change=-1)
    report = summarize([insufficient, sufficient], [
        response(insufficient), response(sufficient, facts(inflation_forecast_change=-1)),
    ])
    stat = report["fields"]["inflation_forecast_change"]
    assert (stat["total"], stat["eligible"], stat["compared"], stat["matched"]) == (2, 1, 1, 1)
    assert stat["input_insufficient"] == 1
    assert report["input_insufficient"] == [{
        "bank": "FED", "decision_date": "2024-02-01", "field": "inflation_forecast_change",
        "reason": "比較資料なし",
    }]
    none = summarize([insufficient], [response(insufficient)])
    assert none["fields"]["inflation_forecast_change"]["match_rate"] is None
    assert none["mismatches"] == []


def test_fetch_api_and_missing_output_failures_remain_visible_in_denominators():
    good, fetch_failed, api_failed, absent = (
        case(), case(day=2, fetch_error="HTTP 404"), case(day=3), case(day=4),
    )
    api_error = {"custom_id": api_failed.custom_id, "response": None,
                 "error": {"code": "request_failed", "message": "架空のエラー"}}
    report = summarize([good, fetch_failed, api_failed, absent], [response(good), api_error])
    stat = report["fields"]["rate_change_bp"]
    assert stat["eligible"] == 4
    assert stat["compared"] == 1
    assert stat["fetch_failed"] == 1
    assert stat["extraction_failed"] == 2
    assert stat["coverage"] == stat["matched_over_eligible"] == 0.25
    assert stat["match_rate"] == 1
    assert len(report["failures"]) == 3
    assert report["meeting_statuses"] == {
        "completed": 1, "fetch_failed": 1, "extraction_failed": 2,
    }
    assert report["usage_unavailable_requests"] == 2
    assert report["cost_is_partial"]


@pytest.mark.parametrize("status", [
    "validating", "failed", "in_progress", "finalizing", "expired", "cancelling", "cancelled",
])
def test_only_completed_batches_count_as_success_but_partial_usage_is_reported(status):
    target = case()
    report = summarize([target], [response(target)], status)
    assert report["fields"]["rate_change_bp"]["compared"] == 0
    assert report["meeting_statuses"] == {"extraction_failed": 1}
    assert report["usage"]["input_tokens"] == 100
    assert Decimal(report["estimated_cost_usd"]) > 0


@pytest.mark.parametrize("changes", [
    {"rate_change_bp": True}, {"rate_change_bp": "0"}, {"rate_change_bp": 0.5},
    {"hawkish_dissents": -1}, {"dovish_dissents": False},
    {"inflation_forecast_change": 1}, {"explicit_future_hike_language": 1},
    {"rate_change_bp": None}, {"confidence": 0.9},
])
def test_invalid_extraction_and_unavailable_field_guesses_are_not_coerced(changes):
    target = case()
    result = study.parse_result(response(target, facts(**changes)), target)
    assert result.extraction is None
    assert result.error
    assert result.usage.input_tokens == 100


@pytest.mark.parametrize("forecast", [True, 2, -2, "1"])
def test_forecast_is_strict_integer_enum(forecast):
    target = case(missing={})
    result = study.parse_result(response(target, facts(inflation_forecast_change=forecast)), target)
    assert result.error


def test_refusal_incomplete_invalid_json_and_missing_usage():
    target = case()
    for content in [
        [{"type": "refusal", "refusal": "架空の拒否"}],
        [{"type": "output_text", "text": "not json"}], [],
    ]:
        raw = response(target)
        raw["response"]["body"]["output"][1]["content"] = content
        assert study.parse_result(raw, target).error
    assert study.parse_result(response(target, status="incomplete"), target).error
    report = summarize([target], [response(target, usage=False)])
    assert report["fields"]["rate_change_bp"]["matched"] == 1
    assert report["usage_unavailable_requests"] == 1
    assert report["cost_is_partial"]


def test_reject_duplicate_unknown_and_malformed_batch_lines():
    target = case()
    line = json.dumps(response(target))
    for texts in ([line, line], [json.dumps(response(case(day=2)))], ["[]"], ["not json"]):
        with pytest.raises(ValueError):
            study.parse_outputs(texts, [target])


def test_batch_cost_uses_cached_tokens_once_and_all_output_tokens():
    usage = study.Usage(input_tokens=1_000_000, cached_tokens=250_000, output_tokens=100_000)
    assert study.estimate_cost(usage, study.LUNA_PRICES) == Decimal("0.1375")
    assert study.estimate_cost(usage, study.LUNA_PRICES, long_context=True) == Decimal("0.245")
    target = case()
    parsed = study.parse_outputs([json.dumps(response(target))], [target])
    report = study.summarize([target], parsed, "completed", "fictional-model", None)
    assert report["estimated_cost_usd"] is None
    with pytest.raises(ValueError):
        study.Usage(input_tokens=1, output_tokens=0, cached_tokens=2)


def test_request_has_strict_five_field_schema_without_ground_truth_or_tools():
    target = case(rate_change_bp=1234567, hawkish_dissents=7654321)
    request = study.build_request(target, {
        target.custom_id: {"type": "input_text", "text": "架空の声明"},
    }, "fictional-model")
    assert request["url"] == "/v1/responses"
    assert request["custom_id"] == target.custom_id
    body = request["body"]
    assert body["model"] == "fictional-model"
    assert "tools" not in body
    schema = body["text"]["format"]["schema"]
    assert body["text"]["format"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == set(study.FIELDS)
    assert schema["properties"]["inflation_forecast_change"] == {"type": "null"}
    assert "1234567" not in json.dumps(request)
    assert "7654321" not in json.dumps(request)


def test_source_coverage_comes_from_document_kind_not_answer(monkeypatch):
    # 本文の人物・組織・会合は架空。URLのホスト名も架空に置き換える。
    real_split = study.urlsplit

    def split(value):
        parsed = real_split(value)
        host = "www.boj.or.jp" if parsed.hostname == "bank-a.example" else "www.federalreserve.gov"
        return parsed._replace(netloc=host)

    monkeypatch.setattr(study, "urlsplit", split)
    fed = meeting().model_copy(update={
        "source_uri": "https://bank-b.example/newsevents/pressreleases/monetary20240201a.htm",
    })
    boj = meeting(bank="BOJ").model_copy(update={
        "source_uri": "https://bank-a.example/mopo/mpmdeci/mpr_2024/k240201a.pdf",
    })
    for current in (fed, boj):
        assert set(study.statement_gaps(current, None)) == {
            "rate_change_bp", "inflation_forecast_change",
        }
        previous = current.model_copy(update={"decision_date": date(2024, 1, 1)})
        assert set(study.statement_gaps(current, previous)) == {"inflation_forecast_change"}
        assert study.statement_gaps(current, previous) == study.statement_gaps(
            current.model_copy(update={"inflation_forecast_change": 1, "rate_change_bp": 99}),
            previous,
        )
    with pytest.raises(ValueError, match="事前確認"):
        study.statement_gaps(meeting(), None)


def test_rate_change_is_dropped_when_the_previous_meeting_is_too_far_back(fictional_sources):
    """部分コーパスで中間の会合が抜けていると、古い会合を直前と誤認する。

    そのまま送ると複数会合分の差を単会合の正解と比べることになるので、間隔が
    定例の周期から外れている側は入力不足へ倒す。
    """
    decided = date(2024, 6, 1)
    current = meeting().model_copy(update={"decision_date": decided})

    def earlier(days: int) -> PolicyMeeting:
        return meeting().model_copy(update={"decision_date": decided - timedelta(days=days)})

    assert "rate_change_bp" not in study.statement_gaps(current, earlier(55))
    assert "rate_change_bp" not in study.statement_gaps(
        current, earlier(study.MAX_ADJACENT_MEETING_DAYS)
    )
    assert (
        study.statement_gaps(current, earlier(study.MAX_ADJACENT_MEETING_DAYS + 1))[
            "rate_change_bp"
        ]
        == study.RATE_GAP_DISCONTINUOUS
    )


@pytest.fixture
def fictional_sources(monkeypatch):
    monkeypatch.setattr(study, "validate_statement_uri", lambda _: None)


@pytest.mark.parametrize("error", [URLError("架空の通信エラー"), IncompleteRead(b"partial")])
def test_source_fetch_failure_is_retained_and_does_not_drop_other_meetings(
    fictional_sources, tmp_path, error,
):
    fetch = Mock(side_effect=[
        error, study.Document("html", b"second", "2回目の声明"),
        study.Document("html", b"third", "3回目の声明"),
    ])
    cases, documents = study.prepare_cases([meeting(day=day) for day in (1, 2, 3)], tmp_path, fetch)
    assert len(cases) == 3
    assert f"対象会合 {cases[0].custom_id}: {type(error).__name__}" in cases[0].fetch_error
    assert f"前回会合 {cases[0].custom_id}: {type(error).__name__}" in cases[1].fetch_error
    assert "rate_change_bp" not in cases[1].missing_inputs
    assert cases[2].fetch_error is None
    assert cases[2].previous_statement.custom_id == cases[1].custom_id
    assert list(documents) == [cases[1].custom_id, cases[2].custom_id]
    assert cases[1].source_sha256
    report = summarize(cases, [response(cases[2])])
    assert report["fields"]["rate_change_bp"]["input_insufficient"] == 1
    assert report["fields"]["rate_change_bp"]["eligible"] == 2
    assert report["fields"]["rate_change_bp"]["fetch_failed"] == 1
    assert report["fields"]["rate_change_bp"]["compared"] == 1
    client = Mock()
    client.files.create.return_value = SimpleNamespace(id="file-jsonl")
    client.batches.create.return_value = batch("validating")
    study.submit_study(client, cases, documents, study.DEFAULT_MODEL, tmp_path)
    sent = [json.loads(line) for line in (tmp_path / "input.jsonl").read_text().splitlines()]
    assert [row["custom_id"] for row in sent] == [cases[2].custom_id]
    assert sent[0]["body"]["input"][0]["content"][3]["text"] == "2回目の声明"
    with pytest.raises(ValueError, match="verified"):
        study.prepare_cases([meeting().model_copy(update={"verified": False})], tmp_path, fetch)


def test_pairing_uses_nearest_earlier_meeting_of_same_bank_not_yaml_order(
    fictional_sources, tmp_path,
):
    meetings = [meeting("FED", 4), meeting("BOJ", 3), meeting("FED", 1),
                meeting("BOJ", 1), meeting("BOJ", 2)]
    fetch = Mock(return_value=study.Document("html", b"fiction", "架空の声明"))
    cases, _ = study.prepare_cases(meetings, tmp_path, fetch)
    by_id = {case.custom_id: case for case in cases}
    assert by_id["FED-2024-02-04"].previous_statement.custom_id == "FED-2024-02-01"
    assert by_id["BOJ-2024-02-03"].previous_statement.custom_id == "BOJ-2024-02-02"
    assert by_id["BOJ-2024-02-02"].previous_statement.custom_id == "BOJ-2024-02-01"
    for bank in ("BOJ", "FED"):
        first = by_id[f"{bank}-2024-02-01"]
        assert first.previous_statement is None
        assert "rate_change_bp" in first.missing_inputs
    assert fetch.call_count == len(meetings)
    assert {call.args[0] for call in fetch.call_args_list} == {m.source_uri for m in meetings}
    target = by_id["BOJ-2024-02-03"]
    restored = study.Case.model_validate_json(target.model_dump_json())
    assert restored.previous_statement.source_sha256 == by_id["BOJ-2024-02-02"].source_sha256
    assert restored.previous_statement.source_uri == by_id["BOJ-2024-02-02"].meeting.source_uri


def test_a_non_adjacent_meeting_is_not_sent_and_cannot_fail_the_target(
    fictional_sources, tmp_path,
):
    """間が抜けている会合は前回声明にしない。

    送れば「前回会合」と偽ることになる。依存に残すと、その取得失敗が対象声明
    だけで測れる反対票数や利上げ文言まで巻き込んで落とす。
    """
    def at(day: date) -> PolicyMeeting:
        return meeting(bank="BOJ").model_copy(update={
            "decision_date": day,
            "statement_published_at": datetime(day.year, day.month, day.day, tzinfo=UTC),
            "source_uri": f"https://sources.example/BOJ/statement-{day}.html",
        })

    distant = at(date(2024, 3, 1))
    current = at(date(2024, 6, 1))
    assert (current.decision_date - distant.decision_date).days > study.MAX_ADJACENT_MEETING_DAYS

    def fetch(url, _cache):
        if url == distant.source_uri:
            raise OSError("架空の取得失敗")
        return study.Document("html", b"fiction", "架空の声明")

    cases, _ = study.prepare_cases([distant, current], tmp_path, fetch)
    by_id = {case.custom_id: case for case in cases}

    target = by_id["BOJ-2024-06-01"]
    assert target.previous_statement is None
    assert target.fetch_error is None
    assert target.missing_inputs["rate_change_bp"] == study.RATE_GAP_DISCONTINUOUS
    assert by_id["BOJ-2024-03-01"].fetch_error is not None


def test_rate_coverage_is_40_of_42_and_forecast_remains_unavailable(fictional_sources, tmp_path):
    meetings = [meeting(bank, day) for bank in ("BOJ", "FED") for day in range(1, 22)]
    cases, _ = study.prepare_cases(meetings[::-1], tmp_path, Mock(
        return_value=study.Document("html", b"fiction", "架空の声明"),
    ))
    records = [response(target, facts(
        rate_change_bp=None if target.previous_statement is None else 0,
    )) for target in cases]
    report = summarize(cases, records)
    rate = report["fields"]["rate_change_bp"]
    assert (rate["eligible"], rate["compared"], rate["matched"], rate["input_insufficient"]) == (
        40, 40, 40, 2,
    )
    assert report["fields"]["inflation_forecast_change"]["input_insufficient"] == 42
    for bank in ("BOJ", "FED"):
        assert sum(c.meeting.bank == bank and "rate_change_bp" not in c.missing_inputs
                   for c in cases) == 20


@pytest.mark.parametrize("invalid", ["duplicate", "publication_in_future", "same_publication"])
def test_invalid_corpus_is_rejected_before_fetching(fictional_sources, tmp_path, invalid):
    first, second = meeting(), meeting(day=2)
    if invalid == "duplicate":
        second = first
    else:
        first = first.model_copy(update={"statement_published_at": datetime(
            2024, 2, 3 if invalid == "publication_in_future" else 2, tzinfo=UTC,
        )})
    fetch = Mock()
    with pytest.raises(ValueError):
        study.prepare_cases([second, first], tmp_path, fetch)
    fetch.assert_not_called()


def test_two_pdf_size_limit_applies_per_request(fictional_sources, tmp_path, monkeypatch):
    monkeypatch.setattr(study, "MAX_SOURCE_BYTES", 20)
    cases, _ = study.prepare_cases([meeting(day=day) for day in (1, 2, 3)], tmp_path, Mock(
        side_effect=[study.Document("pdf", b"x" * 11), study.Document("pdf", b"y" * 11),
                     study.Document("html", b"third", "3回目の声明")],
    ))
    assert cases[0].fetch_error is None
    assert "入力PDFの合計" in cases[1].fetch_error
    assert cases[2].fetch_error is None


def test_html_preserves_footnotes_ignores_scripts_and_cache_avoids_refetch(monkeypatch, tmp_path):
    raw = b"<html><head><title>omit</title></head><body>" \
          b"<h1>Statement on Monetary Policy</h1><p>Policy 0.25</p>" \
          b"<p>Vote <sup>1</sup>: 8-1 &amp; dissent</p><script>omit</script></body></html>"
    response_mock = Mock(status=200)
    response_mock.read.return_value = raw
    opened = Mock()
    opened.__enter__ = Mock(return_value=response_mock)
    opened.__exit__ = Mock(return_value=False)
    urlopen = Mock(return_value=opened)
    monkeypatch.setattr(study.urllib.request, "urlopen", urlopen)
    first = study.fetch_document("https://sources.example/a.html", tmp_path)
    assert first.text == "Statement on Monetary Policy\nPolicy 0.25\nVote 1: 8-1 & dissent"
    assert study.fetch_document("https://sources.example/a.html", tmp_path) == first
    urlopen.assert_called_once()


@pytest.mark.parametrize("raw,url", [
    (b"", "https://sources.example/a.html"),
    (b"error", "https://sources.example/a.html"),
    (b"<html>error</html>", "https://sources.example/a.html"),
    (b"<html>error</html>", "https://sources.example/a.pdf"),
    (b"%PDF-truncated", "https://sources.example/a.pdf"),
])
def test_invalid_sources_are_not_model_inputs(raw, url):
    with pytest.raises(ValueError):
        study.decode_document(raw, url)


def batch(status, **changes):
    data = {"id": "batch-fiction", "status": status, "output_file_id": None,
            "error_file_id": None} | changes
    return SimpleNamespace(**data, model_dump=lambda **_: data)


def test_pdf_and_jsonl_uploads_use_confirmed_responses_batch_path(tmp_path):
    target = case()
    client = Mock()
    client.files.create.side_effect = [SimpleNamespace(id="file-pdf"), SimpleNamespace(id="file-jsonl")]
    client.batches.create.return_value = batch("validating")
    result = study.submit_study(client, [target], {
        target.custom_id: study.Document("pdf", b"%PDF-fake\n%%EOF"),
    }, study.DEFAULT_MODEL, tmp_path)
    assert result == "batch-fiction"
    assert client.files.create.call_args_list[0].kwargs["purpose"] == "user_data"
    request = json.loads((tmp_path / "input.jsonl").read_text())
    assert request["body"]["input"][0]["content"][1] == {
        "type": "input_file", "file_id": "file-pdf",
    }
    client.batches.create.assert_called_once_with(
        input_file_id="file-jsonl", endpoint="/v1/responses", completion_window="24h",
    )
    assert json.loads((tmp_path / "batch.json").read_text())["id"] == result


@pytest.mark.parametrize("bank", ["BOJ", "FED"])
@pytest.mark.parametrize("kinds", [("html", "html"), ("pdf", "pdf"),
                                  ("html", "pdf"), ("pdf", "html")])
def test_two_documents_are_labeled_and_uploaded_once_without_sending_labels(
    fictional_sources, tmp_path, bank, kinds,
):
    previous = meeting(bank, 1, rate_change_bp=1234567, hawkish_dissents=7654321)
    current = meeting(bank, 2, rate_change_bp=2345678, dovish_dissents=8765432)
    documents = [study.Document(kind, f"document-{i}".encode(), f"架空の声明{i}")
                 for i, kind in enumerate(kinds, 1)]
    cases, originals = study.prepare_cases([current, previous], tmp_path, Mock(side_effect=documents))
    client = Mock()
    client.files.create.side_effect = lambda **kwargs: SimpleNamespace(id=f"file-{kwargs['file'][0]}")
    client.batches.create.return_value = batch("validating")
    study.submit_study(client, cases, originals, study.DEFAULT_MODEL, tmp_path)
    requests = [json.loads(line) for line in (tmp_path / "input.jsonl").read_text().splitlines()]
    first, target = requests
    parts = target["body"]["input"][0]["content"]
    assert len(first["body"]["input"][0]["content"]) == 2
    assert first["body"]["text"]["format"]["schema"]["properties"]["rate_change_bp"] == {
        "type": "null",
    }
    assert len(parts) == 4
    assert f"対象会合の声明: {bank}-2024-02-02" in parts[0]["text"]
    assert f"前回会合の声明: {bank}-2024-02-01" in parts[2]["text"]
    assert "金利水準の比較にのみ" in parts[2]["text"]
    assert "5フィールドはすべて「対象会合」についての値" in target["body"]["instructions"]
    assert "前回会合から転記しない" in target["body"]["instructions"]
    for item, kind, original_day in ((parts[1], kinds[1], 2), (parts[3], kinds[0], 1)):
        if kind == "pdf":
            assert item == {"type": "input_file", "file_id": f"file-{bank}-2024-02-0{original_day}.pdf"}
        else:
            assert item == {"type": "input_text", "text": f"架空の声明{original_day}"}
    assert client.files.create.call_count == kinds.count("pdf") + 1
    assert target["body"]["text"]["format"]["schema"]["properties"]["rate_change_bp"] == {
        "type": "integer",
    }
    serialized = json.dumps(requests)
    assert all(str(value) not in serialized for value in (1234567, 7654321, 2345678, 8765432))
    assert study.parse_result(response(cases[0], facts(rate_change_bp=0)), cases[0]).error
    report = summarize(cases, [
        response(cases[0], facts(rate_change_bp=None, hawkish_dissents=7654321)),
        response(cases[1], facts(rate_change_bp=2345678, dovish_dissents=8765432)),
    ])
    assert report["fields"]["rate_change_bp"]["matched"] == 1
    assert report["mismatches"] == []


def test_no_batch_is_submitted_when_all_sources_fail(tmp_path):
    client = Mock()
    target = case(fetch_error="HTTP 404")
    assert study.submit_study(client, [target], {}, study.DEFAULT_MODEL, tmp_path) is None
    assert not client.mock_calls


def test_polling_downloads_both_result_files_and_stops_for_cancelling_timeout(tmp_path, monkeypatch):
    client = Mock()
    client.batches.retrieve.side_effect = [batch("in_progress"), batch(
        "completed", output_file_id="output-fiction", error_file_id="error-fiction",
    )]
    client.files.content.side_effect = [SimpleNamespace(text="output"), SimpleNamespace(text="error")]
    sleep = Mock()
    monkeypatch.setattr(study.time, "sleep", sleep)
    status, texts = study.collect_batch(client, "batch-fiction", tmp_path,
                                       poll_seconds=1, wait_seconds=10)
    assert (status, texts) == ("completed", ["output", "error"])
    assert sleep.call_count == 1
    assert (tmp_path / "error_file_id.jsonl").read_text() == "error"
    client.batches.retrieve.side_effect = None
    client.batches.retrieve.return_value = batch("cancelling")
    assert study.collect_batch(client, "batch-fiction", tmp_path,
                               poll_seconds=1, wait_seconds=0) == ("cancelling", [])


def test_import_needs_neither_sdk_nor_key_and_cli_fails_before_io(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    imported = subprocess.run([sys.executable, "-c", (
        "import sys; sys.modules['openai'] = None; "
        "import trading.data.policy.extraction_study"
    )], env=env, capture_output=True, text=True, check=False)
    assert imported.returncode == 0, imported.stderr
    run_dir = tmp_path / "never-created"
    called = subprocess.run([
        sys.executable, "-m", "trading.data.policy.extraction_study", "--run-dir", str(run_dir),
    ], env=env, capture_output=True, text=True, check=False)
    assert called.returncode == 2
    assert "OPENAI_API_KEY が未設定" in called.stderr
    assert not run_dir.exists()


def test_resume_uses_saved_labels_model_and_prices_without_resubmitting(tmp_path, monkeypatch):
    target = case(rate_change_bp=25)
    study.write_json(tmp_path / "manifest.json", {
        "model": "fictional-model", "started_at": datetime.now(UTC).isoformat(),
        "prices": ["1", "0.5", "2"], "cases": [target.model_dump(mode="json")],
    })
    study.write_json(tmp_path / "batch.json", {"id": "batch-fiction"})
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.batches.retrieve.return_value = batch("completed", output_file_id="output-fiction")
    client.files.content.return_value = SimpleNamespace(text=json.dumps(
        response(target, facts(rate_change_bp=25))
    ))
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=Mock(return_value=client)))
    assert study.main(["--resume", str(tmp_path)]) == 0
    client.files.create.assert_not_called()
    client.batches.create.assert_not_called()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["model"] == "fictional-model"
    assert report["fields"]["rate_change_bp"]["matched"] == 1
    assert report["batch_prices_usd_per_million"]["input"] == "1"


def test_dependency_is_optional_for_runtime():
    import tomllib

    config = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    extras = config["project"]["optional-dependencies"]
    assert any(dep.startswith("openai") for dep in extras["llm"])
    assert not any(dep.startswith("openai") for dep in extras["dev"])
    assert not any(dep.startswith("openai") for dep in config["project"]["dependencies"])
