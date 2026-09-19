"""架空の本文・応答で段階3aの事前登録した測定規則を検証する。"""
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from trading.data.policy import opinions_signal_study as study
from trading.data.policy.meetings import PolicyMeeting, load_meetings


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("unit テストからネットワークへ接続しました")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("socket.socket.connect", fail)
    monkeypatch.setattr("socket.getaddrinfo", fail)


def case(day=1, opinions=("政策金利を引き上げる。", "現状を維持する。"), **facts):
    return study.Case(
        meeting=PolicyMeeting(
            bank="BOJ", decision_date=date(2024, 2, day),
            statement_published_at=datetime(2024, 2, day, tzinfo=UTC),
            verified=True, source_uri="https://example.invalid/statement.pdf", **facts,
        ), opinions=opinions,
    )


def extraction(*stances):
    return study.Extraction(opinions=tuple(study.Opinion(stance=s) for s in stances))


def response(target, stances=("HIKE", "UNSPECIFIED"), status="completed"):
    return {
        "custom_id": target.custom_id, "error": None,
        "response": {"status_code": 200, "body": {
            "status": status,
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 50}},
            "output": [{"type": "reasoning"}, {
                "type": "message", "status": "completed", "content": [{
                    "type": "output_text", "text": extraction(*stances).model_dump_json(),
                }],
            }],
        }},
    }


def summarize(cases, records, status="completed"):
    results = study.parse_outputs(["\n".join(json.dumps(r) for r in records)], cases)
    return study.summarize(cases, results, status)


def test_split_excludes_other_sections_and_handles_pdf_spaces_and_pages():
    text = """Ⅰ．金 融 経 済 情 勢 に 関 す る 意 見
⚫ 物 価 は 上 昇 し た。
 1
Ⅱ ． 金 融 政 策 運 営 に 関 す る 意 見
⚫ 政 策 金 利 を
引 き 上 げ る。
 2
⚫ 据 え 置 く。
Ⅲ．政府からの出席者の発言
⚫ 政府委員甲の発言。
以 上
"""
    assert study.split_policy_opinions(text) == ("政策金利を引き上げる。", "据え置く。")
    assert study.split_policy_opinions(study.SECTION_HEADING + "⚫据え置く。\n以 上\n3") == (
        "据え置く。",
    )


def test_the_older_private_use_bullet_is_accepted():
    """2025-09 の会合を境に箇条書き記号が変わっている。

    それ以前は Wingdings 由来で、PDF のテキスト抽出が私用領域 U+F06C へ落とす。
    ⚫ だけを見ると古い 12 件が丸ごと落ち、標本が 8 件に減る。
    """
    old = study.SECTION_HEADING + "政策金利を引き上げる。\n据え置く。\n以 上"
    assert study.split_policy_opinions(old) == ("政策金利を引き上げる。", "据え置く。")

    # 2 つが混在する文書は実データに無い。来たら推測せず失敗させる。
    with pytest.raises(ValueError, match="混在"):
        study.split_policy_opinions(study.SECTION_HEADING + "⚫利上げ。\n据え置く。")


@pytest.mark.parametrize("text", [
    "Ⅱ．金融政策について⚫利上げ", "Ⅱ.金融政策運営に関する意見⚫利上げ",
    study.SECTION_HEADING * 2 + "⚫利上げ", study.SECTION_HEADING,
    study.SECTION_HEADING + "●利上げ", study.SECTION_HEADING + "⚫⚫利上げ",
    study.SECTION_HEADING + "本文⚫利上げ", study.SECTION_HEADING + "⚫",
])
def test_unknown_or_empty_section_fails(text):
    with pytest.raises(ValueError):
        study.split_policy_opinions(text)


@pytest.mark.parametrize("stances, expected", [
    (("HIKE", "HIKE"), 1), (("CUT", "CUT"), -1),
    (("HIKE", "CUT", "HOLD", "HIKE"), Fraction(1, 4)),
    (("UNSPECIFIED", "UNSPECIFIED"), 0), (("HOLD", "HOLD"), 0),
])
def test_stance_balance(stances, expected):
    assert study.stance_balance(extraction(*stances), len(stances)) == expected


@pytest.mark.parametrize("count", [0, 1, 3])
def test_count_mismatch_retains_both_counts_and_fails_meeting(count):
    target = case()
    report = summarize([target], [response(target, ("HIKE",) * count)])
    row = report["rows"][0]
    assert row["mechanical_opinion_count"] == 2
    assert row["llm_opinion_count"] == count
    assert row["opinions_stance_balance"] is None
    assert row["keyword_balance"] == 0.5
    assert "意見数の不一致" in row["error"]
    assert report["completed_meetings"] == 0
    assert report["verdict"] == "incomplete"


def test_keyword_counts_each_direction_once_per_opinion_even_when_negated():
    opinions = (
        "利上げ、政策金利を引き上げ、さらに利上げ。",
        "金利の引下げ。利下げも検討する。",
        "利上げと利下げの両方があり得る。",
        "利上げを見送る。",
        "物価上昇。賃金を引き上げる。正常化。",
    )
    assert study.keyword_balance(opinions) == Fraction(1, 5)
    assert study.keyword_balance(("据え置く。",)) == 0
    with pytest.raises(ValueError):
        study.keyword_balance(())


@pytest.mark.parametrize("span, verdict", [
    (Fraction(249, 1000), "redundant"), (Fraction(1, 4), "nonredundant"),
    (Fraction(251, 1000), "nonredundant"),
])
def test_fixed_threshold_boundary_and_singleton_exclusion(span, verdict):
    rows, actual = study.tied_ranges([0.0, 0.0, -0.5], [Fraction(0), span, Fraction(-1)])
    assert actual == verdict
    assert rows[0]["is_tied_group"] is False
    assert rows[1]["range_exact"] == str(span)
    assert study.THRESHOLD == Fraction(1, 4)


def test_threshold_compares_exact_fractions_without_rounding():
    # float(11/20) - float(3/10) は 0.25 より大きくなり得る。
    groups, verdict = study.tied_ranges([0, 0, 1, 1], [
        Fraction(3, 10), Fraction(11, 20), Fraction(-1), Fraction(-4, 5),
    ])
    assert [g["range_exact"] for g in groups] == ["1/4", "1/5"]
    assert verdict == "nonredundant"


def test_missing_values_do_not_turn_partial_groups_into_final_verdict():
    groups, verdict = study.tied_ranges([0, 0, 0, 2], [Fraction(0), Fraction(1), None, None])
    assert verdict == "incomplete"
    assert groups[0]["range"] == 1
    assert groups[0]["complete"] is False
    assert groups[1]["range"] is None
    assert study.tied_ranges([], [])[1] == "incomplete"
    assert study.tied_ranges([0], [Fraction(0)])[1] == "not_assessable"


def test_spearman_uses_average_ranks_for_ties_without_p_values():
    assert study.spearman([0, 0, 1], [1, 2, 3]) == pytest.approx(0.866025403784)
    assert study.spearman([0, 1, 2], [2, 1, 0]) == pytest.approx(-1)
    assert study.spearman([0, 0], [1, 2]) is None
    assert study.spearman([], []) is None
    with pytest.raises(ValueError):
        study.spearman([0], [])


def test_request_has_only_normalized_opinions_and_strict_schema():
    request = study.build_request(case())
    assert request["body"]["model"] == "gpt-5.6-luna"
    assert request["url"] == "/v1/responses"
    assert request["body"]["store"] is False
    fmt = request["body"]["text"]["format"]
    assert fmt["strict"] is True
    schema = fmt["schema"]
    assert schema["required"] == ["opinions"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["opinions"]["items"]
    assert item["required"] == ["stance"]
    assert item["additionalProperties"] is False
    assert item["properties"]["stance"]["enum"] == list(study.STANCE_VALUES)
    assert "score" not in json.dumps(request["body"]["input"])


def test_out_of_order_results_and_keyword_comparison():
    first, second = case(), case(day=2, hawkish_dissents=1)
    report = summarize([first, second], [response(second, ("CUT", "HOLD")), response(first)])
    assert [r["opinions_stance_balance"] for r in report["rows"]] == [0.5, -0.5]
    assert report["keyword_comparison"] == {
        "n": 2, "exact_matches": 1, "mean_absolute_difference": 0.5,
        "max_absolute_difference": 1.0, "spearman_rho": None,
        "verdict": "llm_differs",
    }
    assert report["spearman"]["rho"] == pytest.approx(-1)


@pytest.mark.parametrize("status", ["failed", "expired", "cancelled", "in_progress"])
def test_noncompleted_batch_never_counts_partial_results_as_success(status):
    target = case()
    report = summarize([target], [response(target)], status=status)
    assert report["completed_meetings"] == 0
    assert report["rows"][0]["opinions_stance_balance"] is None
    assert report["verdict"] == "incomplete"
    assert report["usage"]["input_tokens"] == 100
    assert Decimal(report["estimated_cost_usd"]) == Decimal("0.0000175")


@pytest.mark.parametrize("change", [
    lambda body: body.update(status="incomplete"),
    lambda body: body["output"][1].update(status="incomplete"),
    lambda body: body["output"][1].update(content=[{"type": "refusal"}]),
    lambda body: body["output"][1]["content"][0].update(text="not json"),
    lambda body: body["output"][1]["content"][0].update(
        text='{"opinions":[{"stance":"UNKNOWN"}]}'
    ),
    lambda body: body["output"][1]["content"][0].update(
        text='{"opinions":[{"stance":"HIKE","confidence":1}]}'
    ),
    lambda body: body.update(output=[]),
    lambda body: body.update(output=[None]),
])
def test_bad_responses_fail_but_keep_usage(change):
    target = case()
    record = response(target)
    change(record["response"]["body"])
    report = summarize([target], [record])
    assert report["completed_meetings"] == 0
    assert report["failures"]
    assert report["usage"]["output_tokens"] == 20


@pytest.mark.parametrize("payload", [
    {"opinions": [{"stance": "HOLD"}, {"stance": "CUT"}], "score": 0},
    {"opinions": [{"stance": None}, {"stance": "CUT"}]},
    {"opinions": [{}, {"stance": "CUT"}]},
    {"opinions": None}, {},
])
def test_invalid_schema_is_never_accepted(payload):
    target = case()
    record = response(target)
    record["response"]["body"]["output"][1]["content"][0]["text"] = json.dumps(payload)
    result = study.parse_result(record, target)
    assert result.extraction is None
    assert result.error


def test_missing_and_error_records_remain_in_table_and_cost_is_partial():
    first, second, third = case(), case(day=2), case(day=3)
    error = {"custom_id": second.custom_id, "error": {"code": "batch_expired"}, "response": None}
    report = summarize([first, second, third], [response(first), error])
    assert len(report["rows"]) == 3
    assert len(report["failures"]) == 2
    assert report["usage_unavailable_requests"] == 2
    assert report["cost_is_partial"] is True
    assert report["keyword_comparison"]["n"] == 1


@pytest.mark.parametrize("custom_id", ["unknown", None, []])
def test_unknown_ids_rejected(custom_id):
    record = response(case())
    record["custom_id"] = custom_id
    with pytest.raises(ValueError, match="custom_id"):
        study.parse_outputs([json.dumps(record)], [case()])


def test_duplicate_id_across_output_and_error_files_rejected():
    text = json.dumps(response(case()))
    with pytest.raises(ValueError, match="custom_id"):
        study.parse_outputs([text, text], [case()])


def test_preparation_keeps_failures_and_reuses_opinions_url_and_extractor(tmp_path):
    meetings = [case(day=i).meeting for i in (1, 2, 3)]
    fetch = Mock(side_effect=[
        study.Document("pdf", b"first"), URLError("offline"), study.Document("pdf", b"third"),
    ])
    extract = Mock(side_effect=[study.SECTION_HEADING + "⚫利上げ。⚫据え置く。", "見出し不明"])
    cases = study.prepare_cases(meetings, tmp_path, fetch, extract)
    assert len(cases) == 3
    assert cases[0].opinions == ("利上げ。", "据え置く。")
    assert cases[0].source_sha256 is not None
    assert "offline" in cases[1].preparation_error
    assert "Ⅱ節" in cases[2].preparation_error
    assert cases[2].source_sha256 is not None
    assert extract.call_count == 2
    assert fetch.call_args_list[0].args == (study.opinions_url(meetings[0].decision_date), tmp_path)
    report = summarize(cases, [response(cases[0])])
    assert len(report["rows"]) == 3
    assert len(report["failures"]) == 2
    assert report["usage_unavailable_requests"] == 0
    assert report["rows"][1]["mechanical_opinion_count"] is None


def test_corpus_is_fixed_even_after_unpublished_meeting_is_released():
    meetings = load_meetings()
    selected = study.select_meetings(meetings)
    assert len(selected) == 20
    assert tuple(m.decision_date for m in selected) == study.TARGET_DATES
    assert date(2026, 9, 18) not in {m.decision_date for m in selected}
    for invalid in (selected[:-1], selected + selected[:1], [
        selected[0].model_copy(update={"verified": False}), *selected[1:],
    ]):
        with pytest.raises(ValueError, match="20会合"):
            study.select_meetings(invalid)


def test_submit_saves_requests_and_uses_batch_with_injected_client(tmp_path):
    client = Mock()
    client.files.create.return_value.id = "file-fixture"
    batch = {"id": "batch-fixture", "status": "validating"}
    client.batches.create.return_value = SimpleNamespace(id=batch["id"], model_dump=lambda **_: batch)
    failed = case(day=2).model_copy(update={"opinions": (), "preparation_error": "書式不明"})
    assert study.submit_study(client, [case(), failed], tmp_path) == "batch-fixture"
    payload = (tmp_path / "input.jsonl").read_text()
    assert len(payload.splitlines()) == 1
    assert json.loads(payload)["custom_id"] == case().custom_id
    client.batches.create.assert_called_once_with(
        input_file_id="file-fixture", endpoint="/v1/responses", completion_window="24h",
    )
    assert study.submit_study(client, [failed], tmp_path) is None
    assert client.files.create.call_count == 1


def test_import_does_not_require_optional_extras_or_api_key():
    script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'pypdf', 'psycopg'}:
            raise ImportError('optional extra blocked')
sys.meta_path.insert(0, Block())
from trading.data.policy import opinions_signal_study
assert opinions_signal_study.keyword_balance(('利上げ',)) == 1
"""
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_api_key_fails_before_fetch_and_run_creation(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fetch = Mock(side_effect=AssertionError("fetch called"))
    monkeypatch.setattr(study, "prepare_cases", fetch)
    with pytest.raises(SystemExit) as error:
        study.main(["--run-dir", str(tmp_path / "run")])
    assert error.value.code == 2
    fetch.assert_not_called()
    assert not (tmp_path / "run").exists()


def test_cli_and_resume_use_saved_cases_and_never_resubmit(monkeypatch, tmp_path):
    selected = study.select_meetings(load_meetings())
    cases = [study.Case(meeting=m, opinions=("利上げ。", "据え置く。")) for m in selected]
    prepare = Mock(return_value=cases)
    monkeypatch.setattr(study, "prepare_cases", prepare)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))

    def submit(client, cases, run_dir):
        study.write_json(run_dir / "batch.json", {"id": "batch-fixture"})
        return "batch-fixture"

    submit_mock = Mock(side_effect=submit)
    monkeypatch.setattr(study, "submit_study", submit_mock)
    collect = Mock(side_effect=[
        ("in_progress", []),
        ("completed", ["\n".join(json.dumps(response(c)) for c in reversed(cases))]),
    ])
    monkeypatch.setattr(study, "collect_batch", collect)
    run_dir = tmp_path / "run"
    assert study.main(["--run-dir", str(run_dir), "--wait-seconds", "0"]) == 1
    assert study.main(["--resume", str(run_dir), "--meetings", "nonexistent.yaml"]) == 0
    assert submit_mock.call_count == prepare.call_count == 1
    report = json.loads((run_dir / "report.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert report["completed_meetings"] == len(report["rows"]) == 20
    assert report["verdict"] == "redundant"
    assert manifest["threshold"] == "1/4"
    assert manifest["hike_keywords"] == list(study.HIKE_KEYWORDS)
    table = (run_dir / "report.md").read_text()
    for meeting in selected:
        assert table.count(str(meeting.decision_date)) == 1
    factory.assert_called_with(timeout=60, max_retries=0)


@pytest.mark.parametrize("key, value", [
    ("threshold", "1/2"), ("hike_keywords", ["変更された語"]), ("cut_keywords", []),
    ("balance_version", "v2"), ("existing_score_version", "v2"), ("prompt", "変更"),
    ("model", "other-model"), ("target_dates", []),
])
def test_resume_rejects_changed_preregistration_before_api(monkeypatch, tmp_path, key, value):
    manifest = study.study_definition() | {key: value}
    study.write_json(tmp_path / "manifest.json", manifest)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    factory = Mock(side_effect=AssertionError("API client must not be created"))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    with pytest.raises(SystemExit) as error:
        study.main(["--resume", str(tmp_path)])
    assert error.value.code == 2
    factory.assert_not_called()


def test_markdown_keeps_failed_meetings_and_escapes_error_cells():
    failed = case(day=2).model_copy(update={"opinions": (), "preparation_error": "a|b\nc"})
    table = study.render_report(summarize([case(), failed], [response(case())]))
    assert "2024-02-01" in table
    assert "2024-02-02" in table
    assert "a\\|b c" in table
    assert "incomplete" in table


def test_the_keyword_verdict_uses_the_same_threshold_as_the_main_decision():
    """副次判定も 0.25 を単位に使う。二つ目の恣意的な数値を持ち込まないため。

    keyword_sufficient なら、この feature に LLM は要らず Gate のコスト側が
    変わる。どちらが正しいかはこの測定では決まらない（両者に正解ラベルが無い）。
    """
    target = case()
    # LLM と一致する応答では差が 0 になり、閾値未満なので keyword_sufficient。
    same = summarize([target], [response(target)])
    assert same["keyword_comparison"]["max_absolute_difference"] == 0.0
    assert same["keyword_comparison"]["verdict"] == "keyword_sufficient"

    # 差が閾値ちょうどに達したら llm_differs 側へ倒す。
    assert study.THRESHOLD == Fraction(1, 4)


def test_the_keyword_verdict_is_incomplete_when_a_meeting_is_missing():
    """欠測があるときに keyword_sufficient を確定させない。

    差は成功したペアだけで作られるので、欠測した会合で 0.25 以上になる可能性を
    排除できない。事前登録した「全20件で最大差が0.25未満」を満たさないまま
    「この feature に LLM は要らない」と読まれるのを防ぐ。
    """
    ok, missing = case(), case(day=2)
    report = summarize([ok, missing], [response(ok)])

    assert report["keyword_comparison"]["n"] == 1
    assert report["keyword_comparison"]["max_absolute_difference"] == 0.0
    assert report["keyword_comparison"]["verdict"] == "incomplete"


def test_llm_differs_is_decided_even_with_a_missing_meeting():
    """llm_differs は 1 件でも閾値に達すれば成立するので欠測があっても確定する。"""
    big, missing = case(), case(day=2)
    report = summarize([big, missing], [response(big, ("CUT", "CUT"))])

    assert report["keyword_comparison"]["max_absolute_difference"] >= float(study.THRESHOLD)
    assert report["keyword_comparison"]["verdict"] == "llm_differs"


def test_note_lists_all_preregistered_keywords():
    note = Path("docs/research/2026-09-20-opinions-signal-redundancy-screen.md").read_text()
    for word in (*study.HIKE_KEYWORDS, *study.CUT_KEYWORDS):
        assert f"`{word}`" in note
