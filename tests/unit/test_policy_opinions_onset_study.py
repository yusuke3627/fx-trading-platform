"""合成tickで事前登録したオンセットの規則を固定する。ネットワーク・DBは使わない。"""
import hashlib
import json
import lzma
import math
import struct
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from unittest.mock import Mock

import pytest

from tests.support import make_tick
from trading.data.policy import opinions_onset_study as study
from trading.data.policy.opinions_reaction_study import cache_path

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
    cases = [study.Case(date(2030, 1, i + 1), T0 + timedelta(weeks=i), None)
             for i in range(20)]
    events = {c.decision_date: study.Observation(t0=c.t0, k_star=1) for c in cases}
    controls = [study.Observation(t0=T0 + timedelta(weeks=i), k_star=0 if i < 2 else 1)
                for i in range(30)]
    return cases, events, controls


def test_registered_constants_and_bins():
    assert study.STUDY_VERSION == "opinions_onset_timing_v1"
    assert study.ONSET_BEFORE == study.ONSET_AFTER == timedelta(minutes=10)
    assert study.BIN == timedelta(minutes=1)
    assert study.SUBBIN == timedelta(seconds=10)
    assert (study.ONSET_MINIMUM_COUNT, study.ONSET_RATIO) == (4, 3)
    bins = study.onset_bins(T0)
    assert len(bins) == 20
    assert bins[0] == (-10, T0 - timedelta(minutes=10), T0 - timedelta(minutes=9))
    assert bins[10] == (0, T0, T0 + timedelta(minutes=1))
    assert bins[-1] == (9, T0 + timedelta(minutes=9), T0 + timedelta(minutes=10))
    assert not any(start <= T0 + timedelta(minutes=10) < end for _, start, end in bins)
    assert study.required_hours(T0) == [T0.replace(minute=0), T0 + timedelta(minutes=10)]


@pytest.mark.parametrize("k", [-10, -3, 0, 9])
@pytest.mark.parametrize("price", ["102", "98"])
def test_unique_largest_absolute_return_and_empty_intervals(k, price):
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
             make_tick(price, price, T0 + k * study.BIN + timedelta(seconds=5))]
    observation = study.measure(ticks, T0)
    assert observation.k_star == k
    assert observation.missing_reason is None
    assert len(observation.returns_bp) == 20
    assert observation.returns_bp[k + 10] == pytest.approx(math.log(float(price) / 100) * 10_000)
    assert sum(r == 0 for r in observation.returns_bp) == 19
    assert observation.subbin_max == (0 if k == 0 else None)


def test_tie_chooses_earlier_interval():
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
             make_tick("200", "200", T0 - timedelta(minutes=2, seconds=30)),
             make_tick("400", "400", T0 + timedelta(seconds=30))]
    observation = study.measure(ticks, T0)
    assert observation.returns_bp[7] == observation.returns_bp[10]
    assert observation.k_star == -3


def test_endpoint_uses_last_tick_strictly_before_without_future_price():
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
             make_tick("200", "200", T0), make_tick("400", "400", T0),
             make_tick("800", "800", T0 + timedelta(minutes=1, microseconds=1))]
    observation = study.measure(ticks, T0)
    assert observation.returns_bp[9] == 0
    assert observation.returns_bp[10] == pytest.approx(math.log(4) * 10_000)
    assert observation.returns_bp[11] == pytest.approx(math.log(2) * 10_000)
    assert observation.k_star == 0
    assert observation.subbin_max == 0


@pytest.mark.parametrize("k", range(-10, 10))
def test_tick_on_minute_boundary_belongs_to_interval_starting_there(k):
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
             make_tick("101", "101", T0 + k * study.BIN)]
    observation = study.measure(ticks, T0)
    assert observation.k_star == k
    assert observation.returns_bp[k + 10] == pytest.approx(math.log(1.01) * 10_000)
    assert sum(r != 0 for r in observation.returns_bp) == 1
    if k == 0:
        assert observation.subbin_max == 0


def test_window_end_tick_is_excluded_from_onset_but_counts_for_post10_floor():
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1))]
    ticks += [make_tick("100", "100", T0 + timedelta(seconds=i)) for i in range(29)]
    ticks.append(make_tick("101", "101", T0 + timedelta(minutes=9, seconds=59)))
    ticks.append(make_tick("999", "999", T0 + study.ONSET_AFTER))
    observation = study.measure(ticks, T0)
    assert observation.k_star == 9
    assert observation.returns_bp[-1] == pytest.approx(math.log(1.01) * 10_000)
    assert observation.post10_tick_count == 31
    assert study.control_exclusion(observation) is None


def test_no_movement_including_price_change_only_at_window_end():
    observation = study.measure([
        make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
        make_tick("999", "999", T0 + study.ONSET_AFTER),
    ], T0)
    assert observation.k_star is None
    assert observation.missing_reason == "no_movement"
    assert observation.returns_bp == (0.0,) * 20
    assert study.control_exclusion(observation) == "no_movement"


@pytest.mark.parametrize("ticks", [
    [], [make_tick("100", "100", T0)],
    [make_tick("100", "100", T0 - study.ONSET_BEFORE)],
])
def test_missing_start_price(ticks):
    observation = study.measure(ticks, T0)
    assert observation.k_star is None
    assert observation.missing_reason == "missing_start_price"
    assert study.control_exclusion(observation) == "missing_start_price"


@pytest.mark.parametrize("bucket", range(6))
@pytest.mark.parametrize("offset", [timedelta(), timedelta(seconds=1)])
def test_ten_second_bucket_position(bucket, offset):
    observation = study.measure([
        make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
        make_tick("101", "101", T0 + bucket * study.SUBBIN + offset),
    ], T0)
    assert observation.k_star == 0
    assert observation.subbin_max == bucket


def test_ten_second_bucket_tie_uses_earliest():
    observation = study.measure([
        make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
        make_tick("200", "200", T0 + timedelta(seconds=11)),
        make_tick("400", "400", T0 + timedelta(seconds=41)),
    ], T0)
    assert observation.k_star == 0
    assert observation.subbin_max == 1


@pytest.mark.parametrize("n0,missing,expected,verdict", [
    (4, 0, Fraction(1), "onset_at_publication"),
    (4, 0, Fraction(4, 3), "onset_at_publication"),
    (3, 0, Fraction(0), "not_established"),
    (6, 0, Fraction(21, 10), "not_established"),
    (4, 16, Fraction(1), "onset_at_publication"),
    (3, 1, Fraction(1), "incomplete"),
    (0, 20, Fraction(1), "incomplete"),
    (0, 0, Fraction(1), "not_established"),
])
def test_primary_judgement_order(n0, missing, expected, verdict):
    result = study.judge([0] * n0 + [None] * missing + [1] * (20 - n0 - missing), expected)
    assert result == {"verdict": verdict, "n0": n0, "n": 20,
                      "expected_count": float(expected), "missing": missing}


def test_expectation_weights_jst_weekdays_and_keeps_missing_events(corpus):
    cases, events, controls = corpus
    for i, case in enumerate(cases):
        if i >= 15:
            cases[i] = replace(case, t0=case.t0 + timedelta(days=1))
        events[case.decision_date] = study.Observation(
            t0=cases[i].t0, missing_reason="no_movement",
        )
    controls = [o.model_copy(update={"k_star": 0 if i < 6 else 1})
                for i, o in enumerate(controls)]
    controls += [study.Observation(t0=T0 + timedelta(days=1, weeks=i), k_star=1)
                 for i in range(30)]
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["expected_count"] == 3.0  # 月曜15件 * 6/30 + 火曜5件 * 0/30。
    assert report["primary"]["verdict"] == "incomplete"
    assert report["controls"]["by_jst_weekday"]["0"]["rate"] == .2
    assert report["controls"]["by_jst_weekday"]["1"]["rate"] == 0
    assert report["histograms"]["events"]["missing"] == 20
    assert all(b["rate"] is None for b in report["histograms"]["events"]["bins"])
    assert "incomplete" in study.render_report(report)


def test_weekday_minimum_29_rejected_30_accepted(corpus):
    cases, events, controls = corpus
    with pytest.raises(ValueError, match="月曜の対照が30日未満.*29日"):
        study.summarize(cases, events, controls[:-1], {})
    assert study.summarize(cases, events, controls, {})["primary"]["n"] == 20
    tuesday_only = [o.model_copy(update={"t0": o.t0 + timedelta(days=1)}) for o in controls]
    with pytest.raises(ValueError, match="月曜の対照が30日未満.*0日"):
        study.summarize(cases, events, tuesday_only, {})


@pytest.mark.parametrize("n0,robust_count", [(4, 16), (5, 20)])
def test_leave_one_out_recomputes_count_and_expectation(corpus, n0, robust_count):
    cases, events, controls = corpus
    for case in cases[:n0]:
        events[case.decision_date] = study.Observation(t0=case.t0, k_star=0, subbin_max=2)
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["verdict"] == "onset_at_publication"
    assert report["primary"]["expected_count"] == pytest.approx(4 / 3)
    loo = report["leave_one_out"]
    assert loo["onset_at_publication_count"] == loo["unchanged_verdict_count"] == robust_count
    assert loo["n"] == 20
    assert all(row["n"] == 19 and row["expected_count"] == pytest.approx(19 / 15)
               for row in loo["results"])
    assert len(report["subbins"]["events"]) == n0


def test_histogram_counts_and_rates_exclude_missing():
    values = [study.Observation(t0=T0, k_star=k) for k in (-10, 0, 0, 9, None)]
    hist = study.histogram(values)
    assert (hist["n"], hist["missing"]) == (4, 1)
    assert len(hist["bins"]) == 20
    assert hist["bins"][10] == {"k": 0, "count": 2, "rate": .5}
    assert sum(row["count"] for row in hist["bins"]) == 4
    assert sum(row["rate"] for row in hist["bins"]) == 1


def test_post_hoc_tail_and_markdown_verdict_first(corpus):
    cases, events, controls = corpus
    for i, day in enumerate(study.STAGE3B_TAIL_DATES):
        old = cases[i]
        cases[i] = replace(old, decision_date=day)
        events[day] = events.pop(old.decision_date)
    report = study.summarize(cases, events, controls, {})
    assert report["stage3b_tail"]["post_hoc_subset"] is True
    assert report["stage3b_tail"]["used_for_primary"] is False
    assert len(report["stage3b_tail"]["events"]) == 5
    markdown = study.render_report(report)
    assert markdown.index("主判定: **not_established**") < markdown.index("n0 =")
    assert "事後の部分集合" in markdown
    assert "主判定には使わない" in markdown
    assert "| -10 |" in markdown and "| 9 |" in markdown


@pytest.mark.parametrize("count,reason", [(29, "insufficient_post10_ticks"), (30, None)])
def test_liquidity_uses_closed_post10_only(count, reason):
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1))]
    ticks += [make_tick("101", "101", T0 + timedelta(seconds=i + 1)) for i in range(count - 1)]
    ticks += [make_tick("999", "999", T0 + study.POST_PRIMARY)]
    observation = study.measure(ticks, T0)
    assert observation.k_star == 0
    assert observation.post10_tick_count == count
    assert study.control_exclusion(observation) == reason


def test_observe_reuses_cached_hours_and_does_not_treat_fetch_failure_as_flat(tmp_path):
    first, boundary = study.required_hours(T0)
    path = cache_path(tmp_path, first)
    path.parent.mkdir(parents=True)
    path.write_bytes(lzma.compress(b"".join(
        struct.pack(">IIIff", msec, price, price, 1.0, 1.0)
        for msec, price in [(40 * 60_000 - 1, 100_000), (50 * 60_000 + 1000, 101_000)]
    )))
    boundary_path = cache_path(tmp_path, boundary)
    boundary_path.parent.mkdir(parents=True)
    boundary_path.write_bytes(b"")
    assert study.fetch_hours(study.required_hours(T0), tmp_path) == {}
    observation = study.observe(T0, tmp_path, {})
    assert observation.k_star == 0 and observation.subbin_max == 0
    assert observation.post10_error is None
    observation = study.observe(T0, tmp_path, {first: "download failed"})
    assert observation.k_star is None
    assert observation.missing_reason == "tick_fetch_error"
    assert "download failed" in observation.error
    path.write_bytes(b"invalid compressed data")
    assert study.observe(T0, tmp_path, {}).missing_reason == "tick_fetch_error"


def test_failure_in_hour_starting_at_end_affects_liquidity_only(tmp_path, monkeypatch):
    ticks = [make_tick("100", "100", T0 - study.ONSET_BEFORE - timedelta(microseconds=1)),
             make_tick("101", "101", T0 + timedelta(seconds=1))]
    first, boundary = study.required_hours(T0)
    path = cache_path(tmp_path, first)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    monkeypatch.setattr(
        "trading.data.policy.opinions_reaction_study.decode_bi5", lambda *args: ticks,
    )
    observation = study.observe(T0, tmp_path, {boundary: "download failed"})
    assert observation.k_star == 0 and observation.missing_reason is None
    assert observation.post10_tick_count is None
    assert study.control_exclusion(observation) == "tick_fetch_error"


@pytest.fixture
def cli_inputs(tmp_path, monkeypatch, corpus):
    cases, _, _ = corpus
    meetings_path = tmp_path / "meetings.yaml"
    meetings_path.write_text("synthetic meetings", encoding="utf-8")
    meetings = [object()]
    monkeypatch.setattr(study, "load_meetings", Mock(return_value=meetings))
    monkeypatch.setattr(study, "select_meetings", Mock(return_value=meetings))
    monkeypatch.setattr(study, "prepare_cases", Mock(return_value=cases))
    candidates = [T0 + timedelta(weeks=100 + i // 5, days=i % 5) for i in range(231)]
    monkeypatch.setattr(study, "control_days", Mock(return_value=(candidates, {})))
    monkeypatch.setattr(study, "fetch_hours", Mock(return_value={}))

    def observe(t0, cache_dir, errors):
        return study.Observation(t0=t0, k_star=1, post10_tick_count=30)

    monkeypatch.setattr(study, "observe", observe)
    args = ["--run-dir", str(tmp_path / "run"), "--meetings", str(meetings_path)]
    return args, cases, candidates


def test_cli_writes_manifest_report_and_uses_shared_cache(cli_inputs, tmp_path):
    args, cases, candidates = cli_inputs
    assert study.main(args) == 0
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["meetings_sha256"] == hashlib.sha256(b"synthetic meetings").hexdigest()
    assert manifest["cache_dir"].endswith("tmp/opinions-reaction/cache")
    assert len(manifest["events"]) == 20
    assert len(manifest["controls"]) == len(manifest["control_candidates"]) == 231
    assert manifest["definition"]["ONSET_MINIMUM_COUNT"] == 4
    assert manifest["definition"]["primary_order"][0] == "onset_at_publication"
    assert report["primary"]["verdict"] == "not_established"
    assert (tmp_path / "run/report.md").read_text() == study.render_report(report)
    hours = sorted({h for t in [*[c.t0 for c in cases], *candidates]
                    for h in study.required_hours(t)})
    assert study.fetch_hours.call_args.args[0] == hours
    assert study.prepare_cases.call_args.args[1] == study.fetch_hours.call_args.args[1]


def test_cli_records_rejected_controls(cli_inputs, tmp_path, monkeypatch):
    args, _, candidates = cli_inputs

    def observe(t0, *args):
        return study.Observation(t0=t0, k_star=1,
                                 post10_tick_count=29 if t0 in candidates[:4] else 30)

    monkeypatch.setattr(study, "observe", observe)
    assert study.main(args) == 0
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    assert len(manifest["controls"]) == 227
    assert len(manifest["rejected_controls"]) == 4
    assert all(value == ["insufficient_post10_ticks"]
               for value in manifest["excluded_control_days"].values())


def test_cli_rejects_existing_directory_without_writing(cli_inputs, tmp_path):
    args, _, _ = cli_inputs
    (tmp_path / "run").mkdir()
    marker = tmp_path / "run/manifest.json"
    marker.write_text("keep existing result")
    assert study.main(args) == 1
    assert marker.read_text() == "keep existing result"
    study.prepare_cases.assert_not_called()


def test_cli_stops_before_fetch_when_control_candidates_differ(cli_inputs, tmp_path, monkeypatch):
    args, _, candidates = cli_inputs
    monkeypatch.setattr(study, "control_days", Mock(return_value=(candidates[:-1], {})))
    assert study.main(args) == 1
    study.fetch_hours.assert_not_called()
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    assert manifest["status"] == "error" and "231日と一致しません: 230日" in manifest["error"]
    assert not (tmp_path / "run/report.json").exists()


def test_cli_cannot_construct_controls_without_all_publication_times(cli_inputs, tmp_path):
    args, cases, _ = cli_inputs
    cases[0] = replace(cases[0], t0=None, preparation_error="statement unavailable")
    assert study.main(args) == 1
    study.control_days.assert_not_called()
    study.fetch_hours.assert_not_called()
    manifest = json.loads((tmp_path / "run/manifest.json").read_text())
    assert manifest["status"] == "error" and "全20件" in manifest["error"]


def test_cli_default_output_and_explicit_cache(cli_inputs, tmp_path, monkeypatch):
    args, _, _ = cli_inputs
    monkeypatch.chdir(tmp_path)
    assert study.main(args[2:] + ["--cache-dir", str(tmp_path / "shared-cache")]) == 0
    outputs = list((tmp_path / "tmp/opinions-onset").iterdir())
    assert len(outputs) == 1
    assert outputs[0].name.endswith("Z")
    manifest = json.loads((outputs[0] / "manifest.json").read_text())
    assert manifest["cache_dir"] == str(tmp_path / "shared-cache")


def post_half_corpus():
    """事象は月8・火12件。対照は月曜だけ k* >= 0 が全件、火曜は全件が負。"""
    monday, tuesday = T0, T0 + timedelta(days=1)
    cases = [
        study.Case(date(2030, 1, i + 1),
                   (monday if i < 8 else tuesday) + timedelta(weeks=i), None)
        for i in range(20)
    ]
    events = {c.decision_date: study.Observation(t0=c.t0, k_star=2) for c in cases}
    controls = [
        study.Observation(t0=monday + timedelta(weeks=i), k_star=3) for i in range(30)
    ] + [
        study.Observation(t0=tuesday + timedelta(weeks=i), k_star=-3) for i in range(30)
    ]
    return cases, events, controls


def test_post_half_is_descriptive_and_weekday_weighted():
    cases, events, controls = post_half_corpus()
    report = study.summarize(cases, events, controls, {})
    post_half = report["post_hoc_post_half"]
    assert post_half["verdict"] is None
    assert post_half["no_threshold_was_preregistered"] is True
    assert (post_half["events"], post_half["n"]) == (20, 20)
    assert post_half["controls"]["by_jst_weekday"] == {"0": 1.0, "1": 0.0}
    # 月8件 x 1.0 + 火12件 x 0.0 = 8.0。曜日構成で重みづけている。
    assert post_half["expected_count"] == 8.0
    assert post_half["controls"]["rate"] == 0.5
    # 主判定は k* == 0 だけを見るので、この形に引きずられない。
    assert report["primary"]["verdict"] == "not_established"
    assert "判定は出さない" in study.render_report(report)


def test_post_half_ignores_missing_events_but_keeps_the_registered_denominator():
    cases, events, controls = post_half_corpus()
    first = cases[0].decision_date
    events[first] = study.Observation(t0=cases[0].t0, missing_reason="no_movement")
    report = study.summarize(cases, events, controls, {})
    post_half = report["post_hoc_post_half"]
    assert (post_half["events"], post_half["n"]) == (19, 20)
    assert report["primary"]["missing"] == 1
