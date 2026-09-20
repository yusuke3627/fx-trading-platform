"""外部APIを呼ばず、架空の本文・合成応答で比較CLIの契約を検証する。"""
import copy
import json
from datetime import UTC, datetime

import pytest

from trading.data.policy import opinions_comparison as study
from trading.data.policy import opinions_signal_study as legacy
from trading.data.policy.meetings import PolicyMeeting


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("オフラインCLIから通信しました")
    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.socket.connect", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("socket.getaddrinfo", fail)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture
def source(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cases, uris = [], {}
    for index, day in enumerate(legacy.TARGET_DATES):
        raw = f"架空原文{index}".encode()
        case = legacy.Case(meeting=PolicyMeeting(
            bank="BOJ", decision_date=day, statement_published_at=datetime(
                day.year, day.month, day.day, tzinfo=UTC),
            verified=True, source_uri="https://example.invalid/statement.pdf"),
            source_sha256=study.digest(raw),
            opinions=tuple(f"架空の意見{index}-{i}。政策金利を引き上げる。"
                           for i in range(15 if index < 10 else 14)))
        uri = f"https://example.invalid/opinions-{index}.pdf"
        uris[case.custom_id] = uri
        (cache / (study.digest(uri.encode()) + ".source")).write_bytes(raw)
        cases.append(case.model_dump(mode="json"))
    path = tmp_path / "source.json"
    study.write_json(path, {"cases": cases, "source_uris": uris,
                            "model": legacy.DEFAULT_MODEL, "prompt": legacy.PROMPT})
    return path, cache


@pytest.fixture
def prepared(source, tmp_path):
    run = tmp_path / "run"
    manifest = study.prepare(*source, run, repeats=2)
    return run, manifest


def response(case, arm, choice="HIKE"):
    ids = [row["id"] for row in case["opinions"]]
    body = {"model": study.MODELS[arm], "status": "completed",
            "created_at": 10, "completed_at": 12,
            "usage": {"input_tokens": 100, "output_tokens": 20}}
    if arm == "jev-ids":
        body["answers"] = {key: {"type": "choice", "choice": choice, "confidence": 0.5,
                                 "probabilities": {label: 0.7 if label == choice else 0.1
                                                   for label in study.CRITERIA}} for key in ids}
    else:
        data = ({"opinions": [{"stance": choice} for _ in ids]}
                if arm == "luna-legacy" else dict.fromkeys(ids, choice))
        body["output"] = [{"type": "message", "status": "completed", "content": [
            {"type": "output_text", "text": json.dumps(data)}]}]
    return {"id": "synthetic", "custom_id": case["id"], "error": None,
            "response": {"status_code": 200, "body": body}}


def import_rows(run, manifest, arm, rows=None, repeat=1, attempt=1, **kwargs):
    path = run / f"synthetic-{arm}-{repeat}-{attempt}.jsonl"
    study.write_jsonl(path, rows if rows is not None else [
        response(case, arm) for case in manifest["cases"]])
    return study.import_responses(run, arm, [path], run / f"{arm}.r{repeat}.requests.jsonl",
                                  repeat, attempt, **kwargs)


def reviewed_labels(run):
    rows = [study.decode(line) for line in (run / "labels.draft.jsonl").read_text().splitlines()]
    for row in rows:
        row.update(stance="HIKE", status="reviewed", reviewer="架空の確認者A",
                   reviewed_at="2026-09-21T00:00:00+00:00")
    path = run / "labels.reviewed.jsonl"
    study.write_jsonl(path, rows)
    return path


def test_prepare_freezes_290_blind_labels_and_three_request_contracts(prepared):
    run, manifest = prepared
    labels = [study.decode(line) for line in (run / "labels.draft.jsonl").read_text().splitlines()]
    assert len(labels) == 290
    assert all(row["stance"] is None and row["status"] == "draft" for row in labels)
    case = manifest["cases"][0]
    old, fixed, jev = [study.requests_for(case, arm) for arm in study.ARMS]
    assert old == legacy.build_request(legacy.Case.model_validate(case["legacy_case"]))
    assert old["body"]["reasoning"] == fixed["body"]["reasoning"] == {"effort": "medium"}
    assert fixed["body"]["input"][0]["content"][0]["text"] == jev["body"]["state"]
    schema = fixed["body"]["text"]["format"]["schema"]
    expected = {row["id"] for row in case["opinions"]}
    assert set(schema["required"]) == set(schema["properties"]) == expected
    assert schema["additionalProperties"] is False
    assert set(jev["body"]["questions"]) == expected
    assert all(key in question["instructions"] and question["criteria"] == study.CRITERIA
               for key, question in jev["body"]["questions"].items())
    assert "rate_change_bp" not in fixed["body"]["input"][0]["content"][0]["text"]
    assert len(manifest["requests"]) == 6


def test_raw_hash_mismatch_fails_without_network_or_output(source, tmp_path):
    path, cache = source
    next(cache.glob("*.source")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="原文hash"):
        study.prepare(path, cache, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_confirmation_rejects_exploratory_meetings(source, prepared, tmp_path):
    run, _ = prepared
    with pytest.raises(ValueError, match="重複"):
        study.prepare(*source, tmp_path / "confirmation", split="confirmation",
                      exploratory=run / "manifest.json")


def test_complete_cli_cycle_is_offline_and_pending_until_human_review(source, tmp_path):
    run = tmp_path / "cli"
    assert study.main(["prepare", "--source-manifest", str(source[0]), "--cache-dir",
                       str(source[1]), "--run-dir", str(run), "--repeats", "1"]) == 0
    manifest = study.load_manifest(run)
    for arm in study.ARMS:
        output = run / f"{arm}.responses.jsonl"
        study.write_jsonl(output, [response(case, arm) for case in manifest["cases"]])
        assert study.main(["import-responses", "--run-dir", str(run), "--arm", arm,
                           "--responses", str(output), "--requests",
                           str(run / f"{arm}.r1.requests.jsonl")]) == 0
    assert study.main(["evaluate", "--run-dir", str(run), "--labels",
                       str(run / "labels.draft.jsonl"), "--output-dir", str(run / "pending")]) == 0
    report = study.read_json(run / "pending/report.json")
    assert report["labels"]["status"] == "pending_human_review"
    assert report["arms"]["jev-ids"]["repeats"][0]["semantics"] is None
    labels = reviewed_labels(run)
    report = study.evaluate(run, labels, run / "reviewed")
    assert report["labels"]["status"] == "ready"
    for arm in study.ARMS:
        metrics = report["arms"][arm]["repeats"][0]["semantics"]
        assert metrics["accuracy"] == 1 and metrics["compared"] == 290
        assert metrics["brier_mean"] == (pytest.approx(0.12) if arm == "jev-ids" else None)
        assert report["arms"][arm]["cost_usd"]["total"] is None
    assert report["common_semantics"]["1"]["opinions"] == 290


def test_legacy_count_mismatch_is_not_positionally_scored(prepared):
    run, manifest = prepared
    records = [response(case, "luna-legacy") for case in manifest["cases"]]
    output = records[0]["response"]["body"]["output"][0]["content"][0]
    data = json.loads(output["text"])
    data["opinions"].pop()
    output["text"] = json.dumps(data)
    imported = import_rows(run, manifest, "luna-legacy", records)
    assert imported["rows"][0]["predictions"] == {}
    assert imported["rows"][0]["returned_opinions"] == 14
    report = study.evaluate(run, reviewed_labels(run), run / "evaluation")
    assert report["arms"]["luna-legacy"]["repeats"][0]["semantics"]["compared"] == 275


@pytest.mark.parametrize("fault", ["missing", "extra", "model", "refusal", "probability", "choice"])
def test_invalid_jev_answers_and_model_preserve_failure_usage(prepared, fault):
    _, manifest = prepared
    case = manifest["cases"][0]
    record = response(case, "jev-ids")
    body = record["response"]["body"]
    key = case["opinions"][0]["id"]
    if fault == "missing":
        del body["answers"][key]
    elif fault == "extra":
        body["answers"]["extra"] = body["answers"][key]
    elif fault == "model":
        body["model"] = "jev-other"
    elif fault == "refusal":
        record["error"] = {"message": "synthetic refusal"}
    elif fault == "probability":
        body["answers"][key]["probabilities"]["HIKE"] = 0.4
    else:
        body["answers"][key]["choice"] = "CUT"
    result = study.parse_response(record, case, "jev-ids")
    assert result["errors"] and not result["predictions"]
    assert result["usage"]["input_tokens"] == 100


def test_duplicate_extra_and_missing_records_are_kept(prepared):
    run, manifest = prepared
    record = response(manifest["cases"][0], "luna-ids")
    extra = copy.deepcopy(record)
    extra["custom_id"] = "unknown"
    prices = run / "prices.json"
    study.write_json(prices, {"input": "0.1", "cached_input": "0.01", "output": "0.6",
                              "source": "架空の単価表"})
    result = import_rows(run, manifest, "luna-ids", [record, record, extra], prices=prices)
    assert len(result["rows"]) == 3
    assert all(row["errors"] and not row["predictions"] for row in result["rows"])
    assert len(result["missing_responses"]) == 19
    assert result["rows"][0]["server_seconds"] is None
    report = study.evaluate(run, None, run / "evaluation")
    assert report["arms"]["luna-ids"]["unobserved_response_rows"] == 19
    assert report["arms"]["luna-ids"]["repeats"][0]["valid_documents"] == 0
    assert report["arms"]["luna-ids"]["unattributed_response_rows"] == 1
    assert report["arms"]["luna-ids"]["server_seconds"]["known_subtotal"] is None
    assert report["arms"]["luna-ids"]["estimated_cost_usd"]["known_subtotal"] is None


def test_duplicate_json_keys_are_detected_before_dictionary_conversion(prepared):
    _, manifest = prepared
    case = manifest["cases"][0]
    record = response(case, "luna-ids")
    record["response"]["body"]["output"][0]["content"][0]["text"] = '{"x":"HIKE","x":"CUT"}'
    result = study.parse_response(record, case, "luna-ids")
    assert "重複" in result["errors"][0]


def test_retries_keep_failed_cost_and_separate_all_time_measurements(prepared):
    run, manifest = prepared
    record = response(manifest["cases"][0], "luna-ids")
    record["response"]["status_code"] = 500
    telemetry = run / "telemetry.json"
    study.write_json(telemetry, {record["custom_id"]: {"client_seconds": 3, "cost_usd": "0.2"}})
    batch, old_run = run / "batch.json", run / "old-report.json"
    study.write_json(batch, {"created_at": 100, "completed_at": 188, "status": "completed"})
    study.write_json(old_run, {"elapsed_seconds": 1121.67})
    import_rows(run, manifest, "luna-ids", [record], telemetry=telemetry,
                batch=batch, run_metadata=old_run)
    import_rows(run, manifest, "luna-ids", attempt=2)
    import_rows(run, manifest, "luna-ids", repeat=2)
    report = study.evaluate(run, None, run / "evaluation")
    arm = report["arms"]["luna-ids"]
    assert arm["failed_attempt_rows"] == 1
    assert arm["repeats"][0]["valid_documents"] == 20
    assert arm["cost_usd"]["known_subtotal"] == "0.2" and arm["cost_usd"]["total"] is None
    assert arm["batch_elapsed_seconds"]["known_subtotal"] == 88
    assert arm["run_elapsed_seconds"]["known_subtotal"] == 1121.67
    assert arm["client_request_seconds"]["known_subtotal"] == 3
    assert arm["server_seconds"]["known_subtotal"] == 82
    assert arm["reproducibility"][0]["agreement"] == 1


def test_unspecified_is_scored_as_a_class_without_luna_probability(prepared):
    _, manifest = prepared
    case = manifest["cases"][0]
    result = study.parse_response(response(case, "luna-ids", "UNSPECIFIED"), case, "luna-ids")
    assert not result["errors"] and set(result["predictions"].values()) == {"UNSPECIFIED"}
    assert result["probabilities"] == {}


def test_changed_request_is_rejected_before_import(prepared):
    run, manifest = prepared
    request = study.requests_for(manifest["cases"][0], "luna-ids")
    request["body"]["instructions"] = "changed"
    path = run / "changed.jsonl"
    study.write_jsonl(path, [request])
    with pytest.raises(ValueError, match="不一致"):
        study.import_responses(run, "luna-ids", [], path)
    assert not (run / "imports").exists()


def test_partial_review_does_not_unlock_semantic_metrics(prepared):
    run, manifest = prepared
    path = reviewed_labels(run)
    rows = [study.decode(line) for line in path.read_text().splitlines()]
    rows[0]["status"] = "draft"
    study.write_jsonl(path, rows)
    labels, status = study.human_labels(manifest, path)
    assert not labels and status["reviewed"] == 289
    rows[0]["text_sha256"] = "changed"
    study.write_jsonl(path, rows)
    with pytest.raises(ValueError, match="hash不一致"):
        study.human_labels(manifest, path)


def test_prices_estimate_known_usage_but_never_call_it_actual_cost(prepared):
    run, manifest = prepared
    prices = run / "prices.json"
    study.write_json(prices, {"input": "0.1", "cached_input": "0.01", "output": "0.6",
                              "source": "架空の単価表"})
    result = import_rows(run, manifest, "luna-legacy", prices=prices)
    assert result["rows"][0]["estimated_cost_usd"] == "0.000022"
    assert result["rows"][0]["cost_usd"] is None


@pytest.mark.parametrize("duplicate", [False, True])
def test_request_telemetry_survives_missing_or_duplicate_responses(prepared, duplicate):
    run, manifest = prepared
    case = manifest["cases"][0]
    telemetry = run / "telemetry.json"
    study.write_json(telemetry, {case["id"]: {"client_seconds": 4, "cost_usd": "0.003"}})
    rows = [response(case, "luna-ids")] * 2 if duplicate else []
    import_rows(run, manifest, "luna-ids", rows, telemetry=telemetry)
    report = study.evaluate(run, None, run / "evaluation")
    assert report["arms"]["luna-ids"]["cost_usd"]["known_subtotal"] == "0.003"
    assert report["arms"]["luna-ids"]["client_request_seconds"]["known_subtotal"] == 4
    assert report["arms"]["luna-ids"]["cost_usd"]["missing_count"] == 19


def test_no_overwrite_of_run_or_imports(prepared):
    run, manifest = prepared
    import_rows(run, manifest, "luna-legacy")
    with pytest.raises(FileExistsError):
        import_rows(run, manifest, "luna-legacy")


@pytest.mark.parametrize("raw", ['{"a":NaN}', '{"a":Infinity}', '{"a":1,"a":2}'])
def test_nonstandard_or_duplicate_json_is_rejected(raw):
    with pytest.raises(ValueError):
        study.decode(raw)
