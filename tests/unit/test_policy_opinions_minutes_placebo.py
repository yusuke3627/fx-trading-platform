"""架空の公表予定・オンセットでプラセボの規則を固定する。ネットワーク・DBは使わない。"""
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from unittest.mock import Mock

import pytest

from tests.support import make_tick
from trading.data.policy import opinions_minutes_placebo as study
from trading.data.policy.extraction_study import Document
from trading.data.policy.meetings import PolicyMeeting

T0 = datetime(2024, 1, 7, 23, 50, tzinfo=UTC)  # JST月曜、UTC日曜。


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("unitテストからネットワークへ接続しました")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("socket.socket.connect", fail)
    monkeypatch.setattr("socket.getaddrinfo", fail)


@pytest.fixture
def corpus():
    cases = [study.Case(date(2023, 12, i + 1), T0 + timedelta(weeks=i)) for i in range(14)]
    events = {c.decision_date: study.Observation(t0=c.t0, k_star=-1, post10_tick_count=30)
              for c in cases}
    controls = [study.Observation(t0=T0 + timedelta(weeks=100 + i),
                                  k_star=0 if i < 15 else -1, post10_tick_count=30)
                for i in range(30)]
    return cases, events, controls


def test_registered_constants_and_shared_observation_rules():
    from trading.data.policy import opinions_onset_study as onset

    assert study.STUDY_VERSION == "opinions_minutes_placebo_v1"
    assert study.AS_OF == date(2026, 9, 22)
    assert (study.EXPECTED_EVENTS, study.EXPECTED_CONTROL_CANDIDATES) == (14, 236)
    assert study.CONTROL_RADIUS_DAYS == 15
    assert study.ALPHA == Fraction(1, 20)
    assert study.observe is onset.observe
    assert study.control_exclusion is onset.control_exclusion
    assert study.required_hours is onset.required_hours
    assert study.histogram is onset.histogram
    observation = onset.measure([
        make_tick("100", "100", T0 - timedelta(minutes=10, microseconds=1)),
        *[make_tick("101", "101", T0 + timedelta(seconds=i)) for i in range(30)],
        make_tick("999", "999", T0 + timedelta(minutes=10)),
    ], T0)
    assert observation.k_star == 0
    assert observation.post10_tick_count == 31
    assert study.control_exclusion(observation) is None


@pytest.mark.parametrize("m,missing,verdict", [
    (11, 0, "not_specific_to_opinions"),
    (11, 3, "not_specific_to_opinions"),
    (10, 1, "incomplete"),
    (10, 0, "not_established"),
    (0, 14, "incomplete"),
])
def test_judgement_order_at_threshold(m, missing, verdict):
    onsets = [0, *([9] * (m - 1))] if m else []
    result = study.judge(onsets + [None] * missing + [-1] * (14 - m - missing), Fraction(1, 2))
    assert result == {"verdict": verdict, "n": 14, "p_hat": .5,
                      "threshold": 11, "m": m, "missing": missing}


def test_binomial_threshold_uses_exact_tail_at_alpha():
    assert study.judge([0], Fraction(1, 20))["threshold"] == 1
    # floatではALPHAと同じ値に丸められる差でも、厳密な裾はALPHAを超える。
    above = Fraction(1, 20) + Fraction(1, 10 ** 30)
    assert study.judge([0], above)["threshold"] == 2
    assert study.judge([0], above)["verdict"] == "not_established"


@pytest.mark.parametrize("rate,threshold,verdict", [
    (Fraction(0), 1, "not_specific_to_opinions"),
    (Fraction(1), 15, "not_established"),
    (Fraction(489, 1000), 11, "not_specific_to_opinions"),
])
def test_threshold_at_extreme_and_registered_example_rates(rate, threshold, verdict):
    result = study.judge([0] * 14, rate)
    assert result["threshold"] == threshold
    assert result["verdict"] == verdict


def test_p_hat_weights_event_jst_weekdays_including_missing(corpus):
    cases, events, controls = corpus
    for i, case in enumerate(cases):
        if i >= 10:
            cases[i] = replace(case, t0=case.t0 + timedelta(days=1))
        events[case.decision_date] = study.Observation(t0=cases[i].t0, missing_reason="no_movement")
    controls = [o.model_copy(update={"k_star": 1}) for o in controls]
    controls += [study.Observation(t0=T0 + timedelta(days=1, weeks=100 + i),
                                   k_star=-1, post10_tick_count=30) for i in range(30)]
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["p_hat"] == float(Fraction(10, 14))
    assert report["primary"]["p_hat"] != report["controls"]["rate"] == .5
    assert report["primary"]["missing"] == 14
    assert report["primary"]["verdict"] == "incomplete"
    assert report["controls"]["by_jst_weekday"]["0"]["rate"] == 1
    assert report["controls"]["by_jst_weekday"]["1"]["rate"] == 0


def test_weekday_minimum_29_rejected_30_accepted(corpus):
    cases, events, controls = corpus
    with pytest.raises(ValueError, match="月曜の対照が30日未満.*29日"):
        study.summarize(cases, events, controls[:-1], {})
    assert study.summarize(cases, events, controls, {})["primary"]["n"] == 14
    tuesday_only = [o.model_copy(update={"t0": o.t0 + timedelta(days=1)}) for o in controls]
    with pytest.raises(ValueError, match="月曜の対照が30日未満.*0日"):
        study.summarize(cases, events, tuesday_only, {})


def test_event_selection_uses_publication_jst_day_and_fixed_as_of(corpus):
    cases, _, _ = corpus
    cutoff = datetime(2026, 9, 22, 8, 50, tzinfo=study.JST)
    cases[-1] = replace(cases[-1], t0=cutoff.astimezone(UTC))
    meeting_day = date(2025, 1, 15)
    meetings = [Mock(decision_date=meeting_day)]
    excluded_cases = [
        study.Case(date(2023, 11, 1), cutoff + timedelta(days=1)),
        study.Case(date(2023, 11, 2), datetime(2025, 1, 17, 23, 50, tzinfo=UTC)),
        *[study.Case(date(2023, 11, i + 4),
                     datetime(2025, 1, 15, 8, 50, tzinfo=study.JST) + timedelta(days=i))
          for i in (-1, 0, 1)],
    ]
    selected, excluded = study.select_events(cases + excluded_cases, meetings)
    assert selected == cases
    assert excluded == {
        "2023-11-01": ["not_yet_published"], "2023-11-02": ["weekend"],
        **{f"2023-11-{day:02}": ["policy_decision_window"] for day in (3, 4, 5)},
    }


def test_event_count_mismatch_stops(corpus):
    cases, _, _ = corpus
    with pytest.raises(ValueError, match="14件と一致しません: 13件"):
        study.select_events(cases[:-1], [])


def test_preparation_reads_all_minutes_but_only_selected_opinions(tmp_path, monkeypatch):
    meetings = [PolicyMeeting(
        bank="BOJ", decision_date=date(2030, 1, i + 1),
        statement_published_at=datetime(2030, 1, i + 1, 3, tzinfo=UTC),
        verified=True, source_uri="https://example.invalid/statement.pdf",
    ) for i in range(21)]
    selector = Mock(return_value=meetings[:20])
    monkeypatch.setattr(study, "select_meetings", selector)
    minutes = "議事要旨――2030年3月1日(金)8:50予定"
    opinions = "主な意見――2月1日(金)8:50予定"
    fetch = Mock(side_effect=[Document("pdf", (minutes + opinions).encode())] * 20
                 + [Document("pdf", minutes.encode())])
    cases = study.prepare_cases(meetings, tmp_path, fetch, bytes.decode)
    selector.assert_called_once_with(meetings)
    assert len(cases) == fetch.call_count == 21
    assert sum(c.opinions_t0 is not None for c in cases) == 20
    assert all(c.t0 == datetime(2030, 2, 28, 23, 50, tzinfo=UTC) for c in cases)
    assert all(c.preparation_error is None for c in cases)
    assert all(call.args[0].endswith("a.pdf") for call in fetch.call_args_list)


@pytest.mark.parametrize("failure", [
    OSError("statement unavailable"), Document("html", b"not a pdf"),
    Document("pdf", "主な意見――2月1日(金)8:50予定".encode()),
    Document("pdf", "議事要旨――3月1日(金)8:50予定".encode()),
])
def test_preparation_records_failure_and_cannot_build_controls(tmp_path, monkeypatch, failure):
    meetings = [Mock(bank="BOJ", decision_date=date(2030, 1, i + 1)) for i in range(2)]
    monkeypatch.setattr(study, "select_meetings", Mock(return_value=meetings))
    statement = "主な意見――2月1日(金)8:50予定 議事要旨――3月1日(金)8:50予定"
    fetch = Mock(side_effect=[failure, Document("pdf", statement.encode())])
    cases = study.prepare_cases(meetings, tmp_path, fetch, bytes.decode)
    assert fetch.call_count == 2
    assert cases[0].preparation_error is not None
    assert cases[1].preparation_error is None
    with pytest.raises(ValueError, match="全会合の公表日時"):
        study.select_events(cases, meetings)


@pytest.fixture
def cli_inputs(tmp_path, monkeypatch, corpus):
    cases, _, _ = corpus
    prepared = [replace(c, opinions_t0=c.t0 - timedelta(days=40)) for c in cases]
    excluded_times = [datetime(2025, 1, 6, 8, 50, tzinfo=study.JST) + timedelta(weeks=i)
                      for i in range(5)]
    future = [datetime(2026, 10, 1, 8, 50, tzinfo=study.JST) + timedelta(days=30 * i)
              for i in range(2)]
    prepared += [study.Case(date(2023, 11, i + 1), t0,
                           opinions_t0=t0 - timedelta(days=40) if i < 6 else None)
                 for i, t0 in enumerate([*excluded_times, *future])]
    meetings = [Mock(decision_date=t.date()) for t in excluded_times]
    meetings_path = tmp_path / "meetings.yaml"
    meetings_path.write_text("synthetic meetings", encoding="utf-8")
    monkeypatch.setattr(study, "load_meetings", Mock(return_value=meetings))
    monkeypatch.setattr(study, "prepare_cases", Mock(return_value=prepared))
    candidates = [T0 + timedelta(weeks=100 + i // 5, days=i % 5) for i in range(236)]
    monkeypatch.setattr(study, "control_days", Mock(return_value=(candidates, {})))
    monkeypatch.setattr(study, "fetch_hours", Mock(return_value={}))
    monkeypatch.setattr(study, "observe", lambda t0, *args: study.Observation(
        t0=t0, k_star=-1, post10_tick_count=30,
    ))
    args = ["--run-dir", str(tmp_path / "run"), "--meetings", str(meetings_path)]
    return args, prepared, candidates


def test_cli_writes_reports_and_excludes_all_publications(cli_inputs, tmp_path):
    args, prepared, candidates = cli_inputs
    assert study.main(args) == 0
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["meetings_sha256"] == hashlib.sha256(b"synthetic meetings").hexdigest()
    assert manifest["cache_dir"].endswith("tmp/opinions-reaction/cache")
    assert len(manifest["publications"]) == 21
    assert len(manifest["events"]) == 14 and len(manifest["excluded_events"]) == 7
    assert len(manifest["controls"]) == len(manifest["control_candidates"]) == 236
    assert manifest["definition"]["ALPHA"] == "1/20"
    assert report["as_of"] == "2026-09-22"
    assert report["preregistration"] == study.PREREGISTRATION
    assert report["primary"]["verdict"] == "not_established"
    assert report["primary"]["p_hat"] == 0 and report["primary"]["threshold"] == 1
    assert report["events"][0]["post10_tick_count"] == 30
    assert report["controls"]["n"] == report["histograms"]["controls"]["n"] == 236
    assert study.control_days.call_args.kwargs == {
        "radius_days": 15,
        "extra_excluded_jst_days": {t.astimezone(study.JST).date() for c in prepared
                                    for t in (c.t0, c.opinions_t0) if t is not None},
    }
    assert study.control_days.call_args.args[0] == [c.t0 for c in prepared[:14]]
    hours = sorted({h for t0 in [*[c.t0 for c in prepared[:14]], *candidates]
                    for h in study.required_hours(t0)})
    assert study.fetch_hours.call_args.args[0] == hours
    assert study.prepare_cases.call_args.args[1] == study.fetch_hours.call_args.args[1]
    markdown = (tmp_path / "run/report.md").read_text()
    assert markdown == study.render_report(report)
    assert markdown.index("主判定: **not_established**") < markdown.index("m =")
    assert "JST曜日" in markdown and "| -10 |" in markdown and "| 9 |" in markdown
    assert "p_value" not in json.dumps(report) and "p値" not in markdown


def test_cli_preserves_fetch_failure_as_missing_and_rejects_illiquid_controls(
    cli_inputs, tmp_path, monkeypatch,
):
    args, prepared, candidates = cli_inputs
    failed_hour = study.required_hours(prepared[0].t0)[0]
    errors = {failed_hour: "download failed"}
    monkeypatch.setattr(study, "fetch_hours", Mock(return_value=errors))

    def observe(t0, cache_dir, received_errors):
        assert received_errors is errors
        if t0 == prepared[0].t0 or t0 == candidates[0]:
            return study.Observation(t0=t0, missing_reason="tick_fetch_error",
                                     error="download failed", post10_error="download failed")
        return study.Observation(t0=t0, k_star=-1,
                                 post10_tick_count=29 if t0 == candidates[1] else 30)

    monkeypatch.setattr(study, "observe", observe)
    assert study.main(args) == 0
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert manifest["tick_fetch_errors"] == {failed_hour.isoformat(): "download failed"}
    assert len(manifest["rejected_controls"]) == 2
    assert report["controls"]["n"] == 234
    assert report["controls"]["exclusion_counts"] == {
        "tick_fetch_error": 1, "insufficient_post10_ticks": 1,
    }
    assert report["primary"]["verdict"] == "incomplete"
    assert report["primary"]["missing"] == 1
    assert report["events"][0]["missing_reason"] == "tick_fetch_error"
    assert "download failed" in (tmp_path / "run/report.md").read_text()


@pytest.mark.parametrize("problem,message", [
    ("preparation", "全会合の公表日時"),
    ("events", "14件と一致しません: 13件"),
    ("controls", "236日と一致しません: 235日"),
])
def test_cli_stops_before_ticks_when_inputs_differ(cli_inputs, tmp_path, monkeypatch, problem, message):
    args, prepared, candidates = cli_inputs
    if problem == "preparation":
        prepared[0] = replace(prepared[0], t0=None, preparation_error="statement unavailable")
    elif problem == "events":
        prepared.pop(0)
    else:
        monkeypatch.setattr(study, "control_days", Mock(return_value=(candidates[:-1], {})))
    assert study.main(args) == 1
    study.fetch_hours.assert_not_called()
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    assert manifest["status"] == "error" and message in manifest["error"]
    assert not (tmp_path / "run/report.json").exists()
    assert not (tmp_path / "run/report.md").exists()


def test_cli_stops_without_verdict_when_a_weekday_has_too_few_controls(
    cli_inputs, tmp_path, monkeypatch,
):
    args, _, candidates = cli_inputs
    monday = [t for t in candidates if t.astimezone(study.JST).weekday() == 0]
    rejected = set(monday[29:])
    monkeypatch.setattr(study, "observe", lambda t0, *args: study.Observation(
        t0=t0, k_star=-1, post10_tick_count=29 if t0 in rejected else 30,
    ))
    assert study.main(args) == 1
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    assert "月曜の対照が30日未満です: 29日" in manifest["error"]
    assert not (tmp_path / "run/report.json").exists()
    assert not (tmp_path / "run/report.md").exists()


def test_cli_rejects_existing_directory_without_overwriting(cli_inputs, tmp_path):
    args, _, _ = cli_inputs
    (tmp_path / "run").mkdir()
    marker = tmp_path / "run/manifest.json"
    marker.write_text("keep existing result")
    assert study.main(args) == 1
    assert marker.read_text() == "keep existing result"
    study.prepare_cases.assert_not_called()


def test_cli_default_output_and_explicit_cache(cli_inputs, tmp_path, monkeypatch):
    args, _, _ = cli_inputs
    monkeypatch.chdir(tmp_path)
    assert study.main(args[2:] + ["--cache-dir", str(tmp_path / "shared-cache")]) == 0
    outputs = list((tmp_path / "tmp/opinions-minutes").iterdir())
    assert len(outputs) == 1 and outputs[0].name.endswith("Z")
    manifest = json.loads((outputs[0] / "manifest.json").read_text())
    assert manifest["cache_dir"] == str(tmp_path / "shared-cache")
