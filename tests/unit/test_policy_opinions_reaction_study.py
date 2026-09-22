"""架空の価格・本文で、事前登録した規則と取得境界を固定する。"""
import json
import lzma
import math
import struct
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support import make_tick
from trading.data.policy import opinions_reaction_study as study
from trading.data.policy.extraction_study import Document
from trading.data.policy.meetings import PolicyMeeting, load_meetings

T0 = datetime(2024, 3, 27, 23, 50, tzinfo=UTC)
PUBLICATION_DAYS = (
    "2024-03-27", "2024-05-08", "2024-06-23", "2024-08-07", "2024-09-30",
    "2024-11-10", "2024-12-26", "2025-02-02", "2025-03-27", "2025-05-12",
    "2025-06-24", "2025-08-07", "2025-09-29", "2025-11-09", "2025-12-28",
    "2026-02-01", "2026-03-29", "2026-05-11", "2026-06-23", "2026-08-09",
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("unitテストからネットワークへ接続しました")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("socket.socket.connect", fail)
    monkeypatch.setattr("socket.getaddrinfo", fail)


def meeting(day, bank="BOJ"):
    return PolicyMeeting(bank=bank, decision_date=day,
                         statement_published_at=datetime.combine(day, datetime.min.time(), UTC),
                         verified=True, source_uri="https://example.invalid/statement.pdf")


def observation(value=1.0, tick_count=30, t0=T0):
    window = study.Window(
        return_bp=value, tick_count=tick_count, median_spread_bp=Decimal("0.2"),
        start=t0, end=t0 + study.POST_PRIMARY, start_tick_at=t0,
        end_tick_at=t0 + study.POST_PRIMARY,
        start_price=Decimal(100), end_price=Decimal(101),
    )
    return study.Observation(t0=t0, pre10=window, post10=window, post60=window)


def corpus():
    return [study.Case(day, datetime.fromisoformat(published + "T23:50:00+00:00"),
                       Fraction(i * i, 400))
            for i, (day, published) in enumerate(zip(study.TARGET_DATES, PUBLICATION_DAYS))]


def control_observations(value=1.0, n=30, weekdays=range(5)):
    monday = datetime(2024, 1, 7, 23, 50, tzinfo=UTC)
    return [observation(value, t0=monday + timedelta(days=day, weeks=i))
            for day in weekdays for i in range(n)]


def bi5(records):
    return lzma.compress(b"".join(struct.pack(">IIIff", msec, ask, bid, 1.0, 1.0)
                                  for msec, ask, bid in records))


def test_mid_at_uses_last_tick_at_or_before_including_duplicates():
    ticks = [make_tick("100", "102", T0), make_tick("102", "104", T0),
             make_tick("200", "202", T0 + timedelta(seconds=2))]
    assert study.mid_at(ticks, T0 - timedelta(microseconds=1)) is None
    assert study.mid_at([], T0) is None
    assert study.mid_at(ticks, T0) == Decimal(103)
    assert study.mid_at(ticks, T0 + timedelta(seconds=1)) == Decimal(103)
    assert study.mid_at(ticks, T0 + timedelta(days=1)) == Decimal(201)


@pytest.mark.parametrize("end_price", ["101", "99"])
def test_window_return_scale_endpoints_and_decimal_prices(end_price):
    end = T0 + timedelta(minutes=10)
    ticks = [make_tick("100", "100", T0 - timedelta(seconds=1)),
             make_tick("99", "101", T0),
             make_tick(end_price, end_price, end),
             make_tick("500", "500", end + timedelta(microseconds=1))]
    window = study.window_stats(ticks, T0, end)
    assert window.return_bp == pytest.approx(math.log(float(end_price) / 100) * 10_000)
    assert window.tick_count == 2
    assert window.median_spread_bp == Decimal(100)
    assert window.start_price == Decimal(100)
    assert window.end_price == Decimal(end_price)
    assert window.start_tick_at == T0
    assert window.end_tick_at == end
    assert study.window_stats(ticks, T0 - timedelta(seconds=2), end) is None


def test_window_without_ticks_carries_prior_price_without_future_lookahead():
    before = T0 - timedelta(seconds=1)
    ticks = [make_tick("100", "102", before),
             make_tick("999", "999", T0 + timedelta(hours=1))]
    window = study.window_stats(ticks, T0, T0 + study.POST_PRIMARY)
    assert window.return_bp == 0
    assert window.tick_count == 0
    assert window.median_spread_bp is None
    assert window.start_tick_at == window.end_tick_at == before


@pytest.mark.parametrize("count,reason", [(29, "insufficient_post10_ticks"), (30, None)])
def test_control_liquidity_floor(count, reason):
    assert study.control_exclusion(observation(tick_count=count)) == reason


def test_control_missing_endpoint_and_fetch_error_are_excluded():
    assert study.control_exclusion(observation().model_copy(update={"post10": None})) == (
        "missing_endpoint_price"
    )
    assert study.control_exclusion(study.Observation(t0=T0, error="connection lost")) == (
        "tick_fetch_error"
    )


@pytest.mark.parametrize("value,expected", [(-1, 0), (1, .125), (2, .5), (3, .875), (4, 1)])
def test_percentile_midrank_and_extremes(value, expected):
    assert study.percentile(value, [1, 2, 2, 3]) == expected


def test_control_days_use_jst_weekdays_and_jst_policy_decision_dates():
    publications = [datetime(2024, 1, day, 23, 50, tzinfo=UTC) for day in (10, 12)]
    meetings = [meeting(date(2024, 1, 8)), meeting(date(2024, 1, 17), "FED")]
    controls, excluded = study.control_days(publications, meetings)
    assert [t.day for t in controls] == [31, 1, 2, 3, 4, 9, 11, 14, 18, 21, 22]
    assert len(controls) == len(set(controls))
    assert all(t.astimezone(study.JST).weekday() < 5 and t.hour == 23 and t.minute == 50 for t in controls)
    assert excluded["2024-01-10"] == ["publication_day"]
    assert excluded["2024-01-12"] == ["weekend", "publication_day"]
    assert all("policy_decision_window" in excluded[f"2024-01-{day:02}"]
               for day in (6, 7, 8, 15, 16, 17))
    assert study.control_days([t.astimezone(timezone(timedelta(hours=9)))
                               for t in publications], meetings) == (controls, excluded)


def test_control_radius_is_inclusive_and_does_not_fill_gaps_between_events():
    publications = [datetime(2024, month, 14, 23, 50, tzinfo=UTC) for month in (1, 3)]
    controls, _ = study.control_days(publications, [])
    days = {t.date() for t in controls}
    assert date(2024, 1, 4) in days and date(2024, 1, 24) in days
    assert date(2024, 1, 3) not in days and date(2024, 1, 25) not in days
    assert not any(day.month == 2 for day in days)


def test_preregistered_publication_dates_produce_231_control_days():
    publications = [datetime.fromisoformat(day + "T23:50:00+00:00") for day in PUBLICATION_DAYS]
    controls, _ = study.control_days(publications, load_meetings())
    assert len(controls) == 231
    assert Counter(t.astimezone(study.JST).weekday() for t in controls) == {
        0: 49, 1: 45, 2: 46, 3: 45, 4: 46,
    }
    assert controls[0].date() == date(2024, 3, 21)
    assert controls[-1].date() == date(2026, 8, 19)


def test_control_days_custom_radius_is_inclusive():
    publications = [datetime(2024, 1, 14, 23, 50, tzinfo=UTC)]
    controls, excluded = study.control_days(publications, [], radius_days=15)
    all_days = {t.date() for t in controls} | {date.fromisoformat(day) for day in excluded}
    assert all_days == {date(2023, 12, 30) + timedelta(days=i) for i in range(31)}
    assert date(2023, 12, 31) in {t.date() for t in controls}
    assert date(2024, 1, 29) in {t.date() for t in controls}


def test_extra_publication_exclusion_uses_jst_date():
    publications = [datetime(2024, 1, 10, 23, 50, tzinfo=UTC)]
    controls, excluded = study.control_days(
        publications, [], extra_excluded_jst_days={date(2024, 1, 9)},
    )
    assert excluded["2024-01-08"] == ["other_publication_day"]
    assert date(2024, 1, 8) not in {t.date() for t in controls}
    assert date(2024, 1, 9) in {t.date() for t in controls}


def test_control_days_default_arguments_preserve_existing_results():
    publications = [datetime(2024, 1, 10, 23, 50, tzinfo=UTC)]
    controls, excluded = study.control_days(publications, [])
    assert [t.date().isoformat() for t in controls] == [
        "2023-12-31", "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04",
        "2024-01-07", "2024-01-08", "2024-01-09", "2024-01-11", "2024-01-14",
        "2024-01-15", "2024-01-16", "2024-01-17", "2024-01-18",
    ]
    assert excluded == {
        "2024-01-05": ["weekend"], "2024-01-06": ["weekend"],
        "2024-01-10": ["publication_day"], "2024-01-12": ["weekend"],
        "2024-01-13": ["weekend"], "2024-01-19": ["weekend"], "2024-01-20": ["weekend"],
    }
    assert study.control_days(
        publications, [], radius_days=10, extra_excluded_jst_days=frozenset(),
    ) == (controls, excluded)


def test_inconsistent_publication_clock_time_is_an_error():
    times = [T0 + timedelta(days=i) for i in range(20)]
    times[-1] += timedelta(minutes=1)
    with pytest.raises(ValueError, match="全件一致"):
        study.control_days(times, [])


@pytest.mark.parametrize("values,verdict,median,missing", [
    ([.70] * 20, "reacts", .70, 0),
    ([.699] * 20, "not_established", .699, 0),
    ([None] * 9 + [.70] * 11, "reacts", .70, 9),
    ([None] * 10 + [1.0] * 10, "incomplete", .5, 10),
    ([None] + [.69] * 19, "incomplete", .69, 1),
    ([None] * 20, "incomplete", 0, 20),
])
def test_judgement_order(values, verdict, median, missing):
    assert study.judge(values) == {
        "verdict": verdict, "median_percentile_missing_zero": median, "missing": missing, "n": 20,
    }


def test_selection_rejects_missing_duplicate_and_wrong_bank():
    meetings = [meeting(day) for day in study.TARGET_DATES]
    assert study.select_meetings(list(reversed(meetings))) == meetings
    for invalid in (meetings[:-1], meetings + [meetings[0]],
                    meetings[:-1] + [meeting(study.TARGET_DATES[-1], "FED")]):
        with pytest.raises(ValueError, match="20会合"):
            study.select_meetings(invalid)


def test_preparation_continues_after_failures_and_separates_keyword_failure(tmp_path):
    meetings = [meeting(day) for day in study.TARGET_DATES[:3]]
    statement = "主な意見―3月28日（木）8:50予定"
    opinions = "Ⅱ．金融政策運営に関する意見⚫利上げする。⚫据え置く。以 上"
    fetch = Mock(side_effect=[
        OSError("statement unavailable"), Document("pdf", opinions.encode()),
        Document("pdf", statement.encode()), OSError("opinions unavailable"),
        Document("pdf", statement.encode()), Document("pdf", opinions.encode()),
    ])
    cases = study.prepare_cases(meetings, tmp_path, fetch, bytes.decode)
    assert fetch.call_count == 6
    assert cases[0].t0 is None and "statement unavailable" in cases[0].preparation_error
    assert cases[0].balance == Fraction(1, 2)
    assert cases[1].preparation_error is None and cases[1].t0.hour == 23
    assert cases[1].balance is None and "opinions unavailable" in cases[1].keyword_error
    assert cases[2].t0.tzinfo == UTC
    assert cases[2].balance == Fraction(1, 2)


def test_two_utc_hours_cover_midnight_and_inclusive_post60_boundary():
    assert study.hours_for(T0) == [T0.replace(minute=0),
                                  (T0 + timedelta(hours=1)).replace(minute=0)]
    assert len(study.hours_for(T0.replace(minute=0))) == 3


def test_fetch_retries_then_caches_success_and_404_without_refetch(tmp_path, capsys):
    from http.client import IncompleteRead

    hours = study.hours_for(T0)
    payload = bi5([(0, 100002, 100000)])
    fetch = Mock(side_effect=[OSError("reset"), IncompleteRead(b""), OSError("503"), payload, None])
    sleep = Mock()
    assert study.fetch_hours(hours, tmp_path, fetch, sleep) == {}
    assert [call.args[0] for call in sleep.call_args_list] == [5, 15, 45, .1, .1]
    assert study.cache_path(tmp_path, hours[0]).read_bytes() == payload
    assert study.cache_path(tmp_path, hours[1]).read_bytes() == b""
    assert study.cache_path(tmp_path, hours[0]).relative_to(tmp_path) == (
        Path("USDJPY/2024/03/27/23h_ticks.bi5")
    )
    assert "/2024/02/27/23h_ticks.bi5" in fetch.call_args_list[0].args[0]
    fetch.reset_mock(side_effect=True)
    sleep.reset_mock()
    assert study.fetch_hours(hours, tmp_path, fetch, sleep) == {}
    fetch.assert_not_called()
    sleep.assert_not_called()
    assert "2/2" in capsys.readouterr().err


def test_outage_pauses_and_retries_the_third_hour_without_caching_failures(tmp_path):
    hours = [T0.replace(minute=0) + timedelta(hours=i) for i in range(3)]
    fetch = Mock(side_effect=[OSError("outage")] * 12 + [b""])
    sleep = Mock()
    errors = study.fetch_hours(hours, tmp_path, fetch, sleep)
    assert set(errors) == set(hours[:2])
    assert fetch.call_args_list[-1] == fetch.call_args_list[-2]
    assert [c.args[0] for c in sleep.call_args_list].count(300) == 1
    assert not study.cache_path(tmp_path, hours[0]).exists()
    assert study.cache_path(tmp_path, hours[2]).exists()
    fetch = Mock(return_value=b"")
    assert study.fetch_hours(hours, tmp_path, fetch, Mock()) == {}
    assert fetch.call_count == 2


def test_persistent_outage_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "OUTAGE_MAX_PAUSES", 1)
    hours = [T0.replace(minute=0) + timedelta(hours=i) for i in range(3)]
    fetch, sleep = Mock(side_effect=OSError("outage")), Mock()
    with pytest.raises(RuntimeError, match="障害が継続"):
        study.fetch_hours(hours, tmp_path, fetch, sleep)
    assert fetch.call_count == 16
    assert [c.args[0] for c in sleep.call_args_list].count(300) == 1


def test_malformed_bi5_is_not_cached_as_no_ticks(tmp_path):
    fetch = Mock(return_value=b"corrupt")
    hour = study.hours_for(T0)[0]
    errors = study.fetch_hours([hour], tmp_path, fetch, Mock())
    assert hour in errors
    assert fetch.call_count == 4
    assert not study.cache_path(tmp_path, hour).exists()


def test_cached_ticks_are_sorted_without_reordering_equal_timestamps(tmp_path):
    first = T0.replace(minute=0)
    second = first + timedelta(hours=1)
    for hour, records in (
        (first, [(2000, 100006, 100004), (1000, 100002, 100000), (1000, 100004, 100002)]),
        (second, [(0, 100010, 100008)]),
    ):
        path = study.cache_path(tmp_path, hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bi5(records))

    ticks, errors = study.read_cached_ticks([second, first], tmp_path, {})

    assert errors == {}
    assert [tick.bid for tick in ticks] == [
        Decimal("100.000"), Decimal("100.002"), Decimal("100.004"), Decimal("100.008"),
    ]
    assert [tick.time for tick in ticks] == [
        first + timedelta(seconds=1), first + timedelta(seconds=1),
        first + timedelta(seconds=2), second,
    ]
    assert [tick.received_at for tick in ticks] == [first, first, first, second]


def test_observe_uses_real_utc_and_keeps_decimal_prices(tmp_path):
    hours = study.hours_for(T0)
    payloads = [bi5([(40 * 60_000, 100002, 100000), (50 * 60_000, 101002, 101000)]),
                bi5([(0, 102002, 102000), (50 * 60_000, 103002, 103000)])]
    study.fetch_hours(hours, tmp_path, Mock(side_effect=payloads), Mock())
    measured = study.observe(T0, tmp_path, {})
    assert measured.pre10.start_tick_at == T0 - study.PRE_WINDOW
    assert measured.post10.start_price == Decimal("101.001")
    assert measured.post10.end_price == Decimal("102.001")
    assert measured.post60.end_tick_at == T0 + study.POST_SECONDARY
    assert study.observe(T0, tmp_path, {hours[1]: "network failure"}).error
    study.cache_path(tmp_path, hours[1]).write_bytes(b"corrupt")
    assert study.observe(T0, tmp_path, {}).error


def test_report_always_computes_separate_direction_series_and_does_not_bridge_gaps():
    cases = corpus()
    events = {c.decision_date: observation(i + 1, t0=c.t0) for i, c in enumerate(cases)}
    controls = control_observations(100)
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["verdict"] == "not_established"
    assert report["direction"]["keyword_change"] == {"n": 19, "rho": pytest.approx(1)}
    assert report["direction"]["keyword_level"] == {"n": 20, "rho": pytest.approx(1)}
    cases[4] = replace(cases[4], balance=None, keyword_error="missing PDF")
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["missing"] == 0
    assert report["direction"]["keyword_change"]["n"] == 17
    assert report["direction"]["keyword_level"]["n"] == 19
    markdown = study.render_report(report)
    assert markdown.index("主判定:") < markdown.index("## 事象ごと")
    assert "前会合差 と post10 bp）: n=17" in markdown
    assert "水準 と post10 bp）: n=19" in markdown
    assert "missing PDF" in markdown
    json.dumps(report, allow_nan=False)


def test_report_missing_events_and_control_floor():
    cases = corpus()
    with pytest.raises(ValueError, match="JST 月曜の対照が30日未満"):
        study.summarize(cases, {}, control_observations(n=29), {})
    report = study.summarize(cases, {}, control_observations(), {})
    assert report["primary"]["verdict"] == "incomplete"
    assert report["primary"]["missing"] == 20
    assert report["direction"]["keyword_change"] == {"n": 0, "rho": None}
    assert report["comparison"]["median_ratio"] is None
    assert "incomplete" in study.render_report(report)


def test_control_only_liquidity_exclusion_and_strict_p90_comparison():
    cases = corpus()
    events = {c.decision_date: observation(1, tick_count=1, t0=c.t0) for c in cases}
    report = study.summarize(cases, events, control_observations(), {})
    assert report["primary"]["missing"] == 0
    assert report["primary"]["median_percentile_missing_zero"] == .5
    assert report["comparison"]["above_control_p90"] == 0
    assert report["comparison"]["median_ratio"] == 1


def test_definition_preserves_preregistered_constants():
    definition = study.study_definition()
    assert {key: definition[key] for key in (
        "SYMBOL", "STUDY_VERSION", "PRE_WINDOW_seconds", "POST_PRIMARY_seconds",
        "POST_SECONDARY_seconds", "MINIMUM_TICKS", "REACTS_THRESHOLD",
        "CONTROL_RADIUS_DAYS", "MEETING_EXCLUSION_DAYS", "MINIMUM_CONTROLS_PER_WEEKDAY",
    )} == {
        "SYMBOL": "USDJPY", "STUDY_VERSION": "opinions_market_reaction_v1",
        "PRE_WINDOW_seconds": 600, "POST_PRIMARY_seconds": 600, "POST_SECONDARY_seconds": 3600,
        "MINIMUM_TICKS": 30, "REACTS_THRESHOLD": .70,
        "CONTROL_RADIUS_DAYS": 10, "MEETING_EXCLUSION_DAYS": 1, "MINIMUM_CONTROLS_PER_WEEKDAY": 30,
    }
    assert len(study.select_meetings(load_meetings())) == 20


def test_cli_writes_artifacts_and_reuses_cache_across_runs(tmp_path, monkeypatch):
    import hashlib

    cases = corpus()
    monkeypatch.setattr(study, "prepare_cases", lambda *args: cases)
    payload = bi5([(second * 1000, 100002 + second, 100000 + second)
                   for second in range(0, 3600, 20)])
    fetch = Mock(return_value=payload)
    fetch_hours = study.fetch_hours
    monkeypatch.setattr(
        study, "fetch_hours", lambda hours, cache: fetch_hours(hours, cache, fetch, Mock()),
    )
    run_dir, cache_dir = tmp_path / "run-1", tmp_path / "cache"
    assert study.main(["--run-dir", str(run_dir), "--cache-dir", str(cache_dir)]) == 0
    manifest = json.loads((run_dir / "manifest.json").read_text())
    report = json.loads((run_dir / "report.json").read_text())
    assert manifest["status"] == "completed"
    expected_sha = hashlib.sha256(study.DEFAULT_MEETINGS_PATH.read_bytes()).hexdigest()
    assert manifest["meetings_sha256"] == expected_sha
    assert manifest["definition"]["SCORING_VERSION"] == study.SCORING_VERSION
    assert len(manifest["events"]) == 20
    assert manifest["events"][0]["t0"] == T0.isoformat()
    assert len(manifest["controls"]) == 231
    assert set(manifest["controls"]).isdisjoint(c.t0.isoformat() for c in cases)
    assert manifest["excluded_control_days"]
    assert report["primary"]["missing"] == 0
    assert report["direction"]["keyword_change"]["n"] == 19
    assert report["direction"]["keyword_level"]["n"] == 20
    assert report["events"][0]["observation"]["post10"]["start_price"] == "103.001"
    assert "主判定:" in (run_dir / "report.md").read_text()
    assert fetch.call_count > 200
    fetch.reset_mock()
    assert study.main(["--run-dir", str(tmp_path / "run-2"), "--cache-dir", str(cache_dir)]) == 0
    fetch.assert_not_called()
    assert json.loads((tmp_path / "run-2" / "report.json").read_text()) == report
    assert study.main(["--run-dir", str(run_dir), "--cache-dir", str(cache_dir)]) == 1
    assert json.loads((run_dir / "manifest.json").read_text()) == manifest


def test_cli_stops_before_ticks_when_publication_time_is_unknown(tmp_path, monkeypatch):
    cases = corpus()
    cases[3] = replace(cases[3], t0=None, preparation_error="statement unavailable")
    monkeypatch.setattr(study, "prepare_cases", lambda *args: cases)
    fetch = Mock(side_effect=AssertionError("日時不明のままtickを取得"))
    monkeypatch.setattr(study, "fetch_hours", fetch)
    run_dir = tmp_path / "run"
    assert study.main(["--run-dir", str(run_dir)]) == 1
    fetch.assert_not_called()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "error"
    assert len(manifest["events"]) == 20
    assert manifest["events"][3]["preparation_error"] == "statement unavailable"
    assert "全20件の公表日時" in manifest["error"]
    assert not (run_dir / "report.json").exists()


def test_cli_records_rejected_controls_and_errors_without_a_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "prepare_cases", lambda *args: corpus())
    monkeypatch.setattr(study, "fetch_hours", lambda *args: {})
    monkeypatch.setattr(study, "observe", lambda t0, *args: observation(tick_count=29).model_copy(
        update={"t0": t0},
    ))
    run_dir = tmp_path / "run"
    assert study.main(["--run-dir", str(run_dir)]) == 1
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "error"
    assert "30日未満" in manifest["error"]
    assert manifest["controls"] == []
    assert len(manifest["rejected_controls"]) > 100
    assert all(o["reason"] == "insufficient_post10_ticks" for o in manifest["rejected_controls"])
    assert not (run_dir / "report.json").exists()


def test_cli_help_needs_no_openai_extra_or_api_key():
    import os
    import subprocess
    import sys

    script = """
import runpy
import sys
sys.modules['openai'] = None
sys.argv = ['opinions_reaction_study', '--help']
runpy.run_module('trading.data.policy.opinions_reaction_study', run_name='__main__')
"""
    env = {k: v for k, v in os.environ.items() if k not in ("TRADING_DB_DSN", "OPENAI_API_KEY")}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--cache-dir" in result.stdout


@pytest.mark.parametrize("missing_window", ["pre10", "post60"])
def test_secondary_window_missing_does_not_remove_primary_or_control(missing_window):
    measured = observation().model_copy(update={missing_window: None})
    assert study.missing_reason(measured) is None
    assert study.control_exclusion(measured) is None


def test_observe_keeps_primary_when_only_other_windows_have_fetch_errors(tmp_path):
    t0 = T0.replace(minute=0)
    pre_hour, primary_hour, secondary_hour = study.hours_for(t0)
    payload = bi5([(second * 1000, 100002 + second, 100000 + second)
                   for second in range(0, 3600, 20)])
    study.fetch_hours([primary_hour], tmp_path, Mock(return_value=payload), Mock())
    measured = study.observe(t0, tmp_path, {
        pre_hour: "pre unavailable", secondary_hour: "post60 unavailable",
    })
    assert measured.post10 is not None and measured.post10.tick_count == 31
    assert measured.pre10 is measured.post60 is None
    assert measured.error is None
    assert set(measured.window_errors) == {"pre10", "post60"}
    assert study.control_exclusion(measured) is None


def test_primary_fetch_failure_still_preserves_observed_pre_window(tmp_path):
    first_hour, second_hour = study.hours_for(T0)
    payload = bi5([(40 * 60_000, 100002, 100000), (50 * 60_000, 101002, 101000)])
    study.fetch_hours([first_hour], tmp_path, Mock(return_value=payload), Mock())
    measured = study.observe(T0, tmp_path, {second_hour: "primary unavailable"})
    assert measured.pre10 is not None
    assert measured.post10 is measured.post60 is None
    assert study.missing_reason(measured) == "tick_fetch_error"
    assert set(measured.window_errors) == {"post10", "post60"}


@pytest.mark.parametrize("monday_count", [0, 29])
def test_weekday_control_floor_cannot_be_replaced_by_large_total(monday_count):
    controls = control_observations(n=monday_count, weekdays=[0]) + control_observations(
        n=50, weekdays=range(1, 5),
    )
    with pytest.raises(ValueError, match=f"JST 月曜の対照が30日未満です: {monday_count}日"):
        study.summarize(corpus(), {}, controls, {})


def test_control_floor_applies_only_to_used_weekdays_and_accepts_exactly_30():
    monday = datetime(2024, 1, 7, 23, 50, tzinfo=UTC)
    cases = [replace(c, t0=monday + timedelta(weeks=i)) for i, c in enumerate(corpus())]
    controls = control_observations(n=30, weekdays=[0])
    report = study.summarize(cases, {}, controls, {})
    assert report["controls"]["abs_post10"]["n"] == 30
    assert report["primary"]["verdict"] == "incomplete"
    assert report["comparison"]["null_expected_count"] == 0
    assert report["comparison"]["event_abs_post10"]["n"] == 0


def test_weekday_matching_prevents_a_weekday_effect_from_becoming_reacts():
    monday = datetime(2024, 1, 7, 23, 50, tzinfo=UTC)
    cases = [replace(c, t0=monday + timedelta(weeks=i, days=0 if i < 12 else 1))
             for i, c in enumerate(corpus())]
    events = {c.decision_date: observation(100 if i < 12 else 1, t0=c.t0)
              for i, c in enumerate(cases)}
    controls = control_observations(100, weekdays=[0]) + control_observations(1, weekdays=[1])
    pooled = [abs(o.post10.return_bp) for o in controls]
    pooled_ranks = [study.percentile(abs(o.post10.return_bp), pooled) for o in events.values()]
    assert study.judge(pooled_ranks)["verdict"] == "reacts"
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["verdict"] == "not_established"
    assert [row["percentile_abs_post10"] for row in report["events"]] == [.5] * 20
    for day, count in (("0", 12), ("1", 8)):
        comparison = report["comparison"]["by_jst_weekday"][day]
        assert comparison["event_abs_post10"]["n"] == count
        assert comparison["median_ratio"] == 1
        assert comparison["above_control_p90"] == 0
    assert report["controls"]["by_jst_weekday"]["0"]["abs_post10"]["median_bp"] == 100
    assert report["controls"]["by_jst_weekday"]["1"]["abs_post10"]["median_bp"] == 1
    assert "| 月 | 30 | 100.0000 | 100.0000 | 100.0000 |" in study.render_report(report)


def test_overall_p90_count_sums_weekday_comparisons():
    cases = corpus()
    events = {c.decision_date: observation(
        50 if c.t0.astimezone(study.JST).weekday() == 0 else 2, t0=c.t0,
    ) for c in cases}
    controls = control_observations(100, weekdays=[0]) + control_observations(1, weekdays=range(1, 5))
    report = study.summarize(cases, events, controls, {})
    assert report["comparison"]["above_control_p90"] == 12
    assert report["comparison"]["by_jst_weekday"]["0"]["above_control_p90"] == 0
    assert report["comparison"]["above_control_p90"] == sum(
        values["above_control_p90"] for values in report["comparison"]["by_jst_weekday"].values()
    )
    assert report["comparison"]["null_expected_count"] == 2
    assert "| 全体 | 20 | 2.0000 | 2.0000 | 12/20 | 2/20 |" in study.render_report(report)


def test_descriptive_series_use_independent_samples_and_expected_count_uses_observed_events():
    cases = corpus()
    first = observation(2, t0=cases[0].t0).model_copy(update={"pre10": None})
    second = observation(4, t0=cases[1].t0)
    second = second.model_copy(update={
        "pre10": second.pre10.model_copy(update={"return_bp": -4}), "post60": None,
    })
    third = observation(-8, t0=cases[2].t0).model_copy(update={"post10": None})
    events = dict(zip([c.decision_date for c in cases], [first, second, third]))
    controls = control_observations()
    controls[0] = controls[0].model_copy(update={"pre10": None})
    controls[1] = controls[1].model_copy(update={"post60": None})
    report = study.summarize(cases, events, controls, {})
    assert report["primary"]["missing"] == 18
    descriptive = report["descriptive"]["events"]
    assert descriptive["abs_pre10"]["n"] == 2
    assert descriptive["abs_pre10"]["median_bp"] == 6
    assert descriptive["abs_post60"]["n"] == 2
    assert descriptive["abs_post60"]["median_bp"] == 5
    assert descriptive["post10_spread_bp"]["n"] == 2
    assert report["descriptive"]["controls"]["abs_pre10"]["n"] == 149
    assert report["descriptive"]["controls"]["abs_post60"]["n"] == 149
    assert report["controls"]["by_jst_weekday"]["0"]["descriptive"]["abs_pre10"]["n"] == 29
    assert report["comparison"]["event_abs_post10"]["n"] == 2
    assert report["comparison"]["above_control_p90"] == 2
    assert report["comparison"]["null_expected_count"] == .2
    markdown = study.render_report(report)
    assert "| 全体 | 2 | 3.0000 | 3.0000 | 2/2 | 0.2/2 |" in markdown
    assert "| 事象 | 全体 | 2 | 6.0000 | 7.6000 | 7.8000 | 2 | 5.0000 | 2 | 0.2000 |" in markdown
    assert "abs(pre10) n" in markdown
    assert "| pre10 bp |" not in markdown


def test_secondary_post60_ranks_against_the_same_weekday_and_never_judges():
    cases = corpus()
    events = {}
    for case in cases:
        base = observation(1.0, t0=case.t0)
        loud = study.Window(
            return_bp=50.0, tick_count=30, median_spread_bp=Decimal("0.2"),
            start=case.t0, end=case.t0 + study.POST_SECONDARY,
            start_tick_at=case.t0, end_tick_at=case.t0 + study.POST_SECONDARY,
            start_price=Decimal(100), end_price=Decimal(101),
        )
        events[case.decision_date] = base.model_copy(update={"post60": loud})
    report = study.summarize(cases, events, control_observations(1.0), {})
    secondary = report["secondary_post60"]
    assert secondary["verdict"] is None
    assert secondary["no_threshold_was_preregistered"] is True
    assert secondary["n"] == 20
    assert secondary["median_percentile"] == 1.0
    assert secondary["above_control_p90"] == 20
    # post60 が閾値を超えても主判定は post10 のままで動かない。
    assert report["primary"]["verdict"] == "not_established"
    assert [row["percentile_abs_post60"] for row in report["events"]] == [1.0] * 20


def test_secondary_post60_strict_p90_comparison_at_midrank_boundary():
    case = corpus()[0]
    events = {case.decision_date: observation(40, t0=case.t0)}
    controls = [observation(value, t0=case.t0 + timedelta(weeks=value + 1))
                for value in range(45)]
    report = study.summarize([case], events, controls, {})
    assert report["events"][0]["percentile_abs_post60"] == .90
    assert report["secondary_post60"]["median_percentile"] == .90
    assert report["secondary_post60"]["above_control_p90"] == 1


def test_secondary_post60_skips_events_without_the_window():
    cases = corpus()
    events = {c.decision_date: observation(1.0, t0=c.t0) for c in cases}
    first = cases[0].decision_date
    events[first] = events[first].model_copy(update={"post60": None})
    report = study.summarize(cases, events, control_observations(1.0), {})
    assert report["secondary_post60"]["n"] == 19
    assert report["events"][0]["percentile_abs_post60"] is None
    # 副次窓が欠けても主判定の観測は残る。
    assert report["primary"]["missing"] == 0
