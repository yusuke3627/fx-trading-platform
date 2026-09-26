"""H7 の時刻・母集団・順位統計・約定とファイル入出力の定義を確かめる。"""
from __future__ import annotations

import csv
import gzip
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from random import Random

import pytest
from pydantic import ValidationError

from trading.backtest import event_currency_strength_study as h7


@pytest.fixture
def plan():
    return h7.Plan(
        study_version="test_h7", symbols={"usdjpy": "TEST_UJ", "eurusd": "TEST_EU"},
        usdjpy_pip_size="0.01", series=("test_a", "test_b", "test_c", "test_d"),
        stages={"explore": {"start": "2025-01-01", "end": "2025-12-31"},
                "confirm": {"start": "2024-01-01", "end": "2024-12-31"}},
        excluded_dates=(), bootstrap_seed=17, bootstrap_samples=100,
        confirm_extra_cost_pips="2",
    )


def observation(plan, day, series="test_a", period=None):
    day = date.fromisoformat(day)
    return series, period or day.isoformat(), h7.release_at(day, plan)


def event_file(plan, *days, plan_hash="test"):
    return h7.build_events([observation(plan, day) for day in days], plan, plan_hash)


def write_ticks(path, plan, stage, overrides=None):
    rows = []
    id_ = 0
    for window in stage.windows:
        t0 = h7.broker_at(h7.release_at(window.day, plan), plan)
        for symbol in (plan.symbols.usdjpy, plan.symbols.eurusd):
            # 各時間区間、pre/post/end、約定用の直後 quote を独立に用意する。
            entries = [(-3600, "100", "100.02"), (-60, "100", "100.02"),
                       (900, "101", "101.02"), (3600, "102", "102.02"),
                       (7200, "103", "103.02")]
            if overrides:
                entries = overrides(window.day, symbol, entries)
            for offset, bid, ask in entries:
                id_ += 1
                rows.append([symbol, (t0 + timedelta(seconds=offset)).isoformat(), id_, bid, ask])
    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    with gzip.open(path, "wt", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(h7.CSV_FIELDS)
        writer.writerows(rows)
    return rows


def test_plan_rejects_unknown_fields_and_is_frozen(plan):
    with pytest.raises(ValidationError):
        h7.Plan.model_validate({**plan.model_dump(), "surprise": True})
    with pytest.raises(ValidationError):
        plan.bootstrap_seed = 99
    for update in ({"confirm_extra_cost_pips": "-1"}, {"usdjpy_pip_size": "NaN"},
                   {"window_end_minutes": 120}, {"release_timezone": "UTC"}):
        with pytest.raises(ValidationError):
            h7.Plan.model_validate({**plan.model_dump(), **update})


@pytest.mark.parametrize(("day", "hour"), [("2025-03-07", 13), ("2025-03-10", 12),
                                           ("2025-11-03", 13)])
def test_release_and_broker_time_across_dst(plan, day, hour):
    t0 = h7.release_at(date.fromisoformat(day), plan)
    assert (t0.hour, t0.minute) == (hour, 30)
    assert h7.broker_at(t0, plan) == datetime.fromisoformat(day + "T15:30:00+00:00")


def test_events_first_release_coincident_series_and_exclusions(plan):
    plan = plan.model_copy(update={"excluded_dates": (
        h7.ExcludedDate(day="2025-03-14", reason="既に見た架空の窓"),)})
    first = observation(plan, "2025-03-07", period="period_1")
    rows = [observation(plan, "2025-03-21", period="period_1"), first,
            observation(plan, "2025-03-07", series="test_b"),
            observation(plan, "2025-03-14"), observation(plan, "2023-01-06")]
    s, p, t = observation(plan, "2025-04-04")
    rows.extend([(s, p, t - timedelta(minutes=1)), (s, p, t)])
    events = h7.build_events(rows, plan, "hash")
    assert events == h7.build_events(reversed(rows), plan, "hash")
    stage = events.stages.explore
    assert len(stage.events) == 1
    assert stage.events[0].series == ("test_a", "test_b")
    assert {e.day: e.reasons for e in stage.excluded} == {
        date(2023, 1, 6): ("out_of_range",), date(2025, 3, 14): ("excluded_date",)}
    assert len(events.excluded_release_times) == 1
    assert stage.events[0].candidates[1].reasons == ("excluded_date", "event_day")
    assert stage.windows[0].since == datetime(2025, 2, 28, 12, 30, tzinfo=UTC)
    assert stage.windows[0].until == datetime(2025, 2, 28, 15, 45, tzinfo=UTC)


def test_placebo_excludes_event_days_across_stages(plan):
    events = event_file(plan, "2024-12-27", "2025-01-03")
    candidates = events.stages.explore.events[0].candidates
    assert candidates[0].reasons == ("out_of_range", "event_day")
    assert candidates[1].reasons == ()


def test_event_file_round_trip_and_hash_window_validation(plan, tmp_path):
    path = tmp_path / "events.json"
    events = event_file(plan, "2025-03-07")
    h7.write_json(path, events.model_dump(mode="json"))
    assert h7.load_events(path, plan, "test") == events
    with pytest.raises(ValueError, match="sha256"):
        h7.load_events(path, plan, "other")
    data = json.loads(path.read_text())
    data["stages"]["explore"]["windows"][0]["until"] = "2025-02-28T23:00:00Z"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="整合"):
        h7.load_events(path, plan, "test")


@pytest.mark.parametrize(("age", "valid"), [(60, True), (61, False)])
def test_last_quote_age_boundary_and_id_tie(plan, tmp_path, age, valid):
    stage = event_file(plan, "2025-03-07").stages.explore

    def change(day, symbol, entries):
        return [(s, b, a) for s, b, a in entries if s != -60] + [
            (-60 - age, "98", "100"), (-60 - age, "100", "102"),
            (-59, "999", "1001")]

    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, change)
    quotes = h7.read_quotes(path, plan, stage)
    day = date(2025, 3, 7)
    assert quotes[day][plan.symbols.usdjpy].before[0].mid == Decimal(101)
    result, reason = h7.observe(day, quotes[day], plan)
    assert (result is not None) == valid
    assert reason == ("" if valid else "stale_quote")


@pytest.mark.parametrize("missing_bucket", [0, 1, 2])
def test_missing_each_hour_excludes_day(plan, tmp_path, missing_bucket):
    stage = event_file(plan, "2025-03-07").stages.explore

    def change(day, symbol, entries):
        if symbol == plan.symbols.eurusd:
            return [(s, b, a) for s, b, a in entries if (s + 3600) // 3600 != missing_bucket]
        return entries

    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, change)
    day = date(2025, 3, 7)
    result, reason = h7.observe(day, h7.read_quotes(path, plan, stage)[day], plan)
    assert result is None and reason == "missing_ticks"


def test_returns_use_midpoints_at_pre_post_end(plan, tmp_path):
    stage = event_file(plan, "2025-03-07").stages.explore

    def change(day, symbol, entries):
        values = { -60: "100", 900: "110", 7200: "121"} if symbol == plan.symbols.usdjpy else {
            -60: "100", 900: "90", 7200: "95"}
        return [(s, values.get(s, "100"), values.get(s, "100")) for s, _, _ in entries]

    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, change)
    day = date(2025, 3, 7)
    result, reason = h7.observe(day, h7.read_quotes(path, plan, stage)[day], plan)
    assert reason == ""
    assert result.r_uj == pytest.approx(math.log(1.1))
    assert result.r_eu == pytest.approx(math.log(0.9))
    assert result.fwd == pytest.approx(math.log(1.1))
    assert result.m == pytest.approx((math.log(1.1) - math.log(0.9)) / 2)
    assert result.x + result.m == pytest.approx(result.r_uj)


def test_partial_rank_ties_and_zero_denominator():
    assert h7.ranks([3, 1, 1, 4]) == [3, 1.5, 1.5, 4]
    # 順位の中心化平方和は 4.5, 5, 5。内積は ab=4.5, ac=1, bc=0。
    # (ab-ac*bc/cc)/sqrt((aa-ac²/cc)*(bb-bc²/cc)) = 9/sqrt(86)。
    assert h7.partial_rank([1, 1, 3, 4], [1, 2, 3, 4], [3, 1, 4, 2]) == pytest.approx(
        9 / math.sqrt(86))
    assert h7.partial_rank([1, 2, 3], [3, 2, 1], [1, 2, 3]) is None
    assert h7.partial_rank([1, 1, 1], [3, 2, 1], [1, 2, 3]) is None
    assert h7.partial_rank([], [], []) is None


@pytest.mark.parametrize(("delay", "valid"), [(0, True), (60, True), (61, False)])
def test_execution_first_at_or_after_and_delay_limit(plan, tmp_path, delay, valid):
    stage = event_file(plan, "2025-03-07").stages.explore

    def change(day, symbol, entries):
        return [(s, b, a) for s, b, a in entries if s not in (900, 7200)] + [
            (899, "99", "100"), (900 + delay, "101", "102"),
            (900 + delay, "105", "106"), (7199, "110", "111"),
            (7200 + delay, "112", "113")]

    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, change)
    day = date(2025, 3, 7)
    quotes = h7.read_quotes(path, plan, stage)[day][plan.symbols.usdjpy]
    assert quotes.after[0].bid == Decimal(101)
    fills = h7.execution_quotes(day, quotes, plan)
    assert (fills is not None) == valid
    if valid:
        assert h7.net_pips(1, *fills, Decimal("0.01")) == Decimal(1000)
        assert h7.net_pips(-1, *fills, Decimal("0.01")) == Decimal(-1200)
        assert h7.net_pips(0, *fills, Decimal("0.01")) is None


@pytest.mark.parametrize(("drop", "selected"), [((), "2025-02-28"),
    (("2025-02-28",), "2025-03-14"), (("2025-02-28", "2025-03-14"), None)])
def test_pair_preference_and_unpaired_reference(plan, tmp_path, drop, selected):
    stage = event_file(plan, "2025-03-07").stages.explore
    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, lambda day, symbol, entries: (
        [] if day.isoformat() in drop else entries))
    report = h7.measure(plan, stage, h7.read_quotes(path, plan, stage), "explore")
    assert report["paired_events"] == (1 if selected else 0)
    assert report["unpaired_reference"]["count"] == (0 if selected else 1)
    if selected:
        assert report["pairs"][0]["placebo_day"] == date.fromisoformat(selected)


def sample_pairs():
    rng = Random(821)
    pairs = []
    for i in range(35):
        rows = []
        for _ in range(2):
            m, x, fwd = (rng.uniform(-1, 1) for _ in range(3))
            rows.append(h7.Observation(date(2025, 1, 1) + timedelta(days=i), m+x, x-m, m, x, fwd))
        pairs.append(tuple(rows))
    return pairs


def test_bootstrap_deterministic_and_pairs_stay_together(plan):
    pairs = sample_pairs()
    first = h7.bootstrap(pairs, plan)
    assert first == h7.bootstrap(pairs, plan)
    assert first["valid"] == plan.bootstrap_samples
    # 同一イベントをプラセボにも置けば全ての標本で差は厳密に 0。
    same = h7.bootstrap([(a, a) for a, _ in pairs], plan)
    assert same["lower"]["delta_A"] == same["lower"]["delta_C"] == 0
    small = h7.bootstrap(pairs[:1], plan)
    assert small["undefined"] == plan.bootstrap_samples
    assert small["lower"]["A"] is None


@pytest.mark.parametrize(("stats_update", "lower_update", "pnl", "undefined", "result"), [
    ({}, {}, "1", 0, "支持"), ({"delta_C": 0}, {}, "1", 0, "棄却"),
    ({"delta_A": -0.01}, {}, "1", 0, "棄却"), ({"A": 0}, {}, "1", 0, "棄却"),
    ({}, {"C": 0}, "1", 0, "判定不能"), ({}, {}, "0", 0, "判定不能"),
    ({}, {}, "1", 2, "判定不能"), ({}, {}, "1", 1, "支持"),
    ({"A": None}, {}, "1", 0, "判定不能"),
])
def test_confirm_decision(plan, stats_update, lower_update, pnl, undefined, result):
    stats = dict.fromkeys(("A", "C", "delta_A", "delta_C"), 0.2) | stats_update
    boot = {"samples": 100, "undefined": undefined,
            "lower": dict.fromkeys(stats, 0.01) | lower_update}
    assert h7.decide(stats, boot, Decimal(pnl), plan, "confirm")["result"] == result


@pytest.mark.parametrize(("a", "c", "result"), [(0.1, 0.1, "確認へ進む"),
    (0.1, 0.099, "止める"), (None, 0.3, "止める")])
def test_explore_gate(plan, a, c, result):
    assert h7.decide({"A": a, "C": c}, {}, None, plan, "explore")["result"] == result


def test_pnl_uses_only_pairs_cost_and_zero_counts(plan, tmp_path):
    stage = event_file(plan, "2025-03-07", "2025-04-04").stages.explore
    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage)
    quotes = h7.read_quotes(path, plan, stage)
    event = h7.Observation(date(2025, 3, 7), -1, -3, 1, -2, 1)
    zero = h7.Observation(date(2025, 4, 4), 0, 0, 0, 0, 0)
    report = h7.pnl_report([(event, event), (zero, zero)], quotes, plan, "confirm")
    assert report["rules"]["m"] == {"trades": 1, "zero_signal": 1,
        "missing_execution_quote": 0, "mean_pips": Decimal(198),
        "mean_adjusted_pips": Decimal(196)}
    assert report["rules"]["r_uj"]["mean_pips"] == Decimal(-202)
    assert report["paired_difference"]["mean_pips"] == Decimal(400)
    quotes[event.day][plan.symbols.usdjpy].after[0] = None
    excluded = h7.pnl_report([(event, event)], quotes, plan, "confirm")
    assert excluded["rules"]["m"]["missing_execution_quote"] == 1
    assert excluded["rules"]["m"]["mean_adjusted_pips"] is None


def test_spreads_compute_cost_only_and_decimal_percentiles(plan, tmp_path, monkeypatch):
    stage = event_file(plan, "2025-03-07", "2025-04-04").stages.explore
    path = tmp_path / "ticks.csv.gz"

    def change(day, symbol, entries):
        spread = "100.04" if day.month == 4 else "100.02"
        return [(s, "100", spread) for s, _, _ in entries]

    write_ticks(path, plan, stage, change)
    quotes = h7.read_quotes(path, plan, stage)

    def forbidden(*args):
        raise AssertionError("コスト測定からリターン・統計計算へ到達した")

    monkeypatch.setattr(h7, "observe", forbidden)
    monkeypatch.setattr(h7, "statistics", forbidden)
    result = h7.spreads(plan, stage, quotes)
    assert result["post"] == {"count": 2, "missing_execution_quote": 0,
        "mean_pips": Decimal(3), "median_pips": Decimal(3),
        "p75_pips": Decimal("3.5"), "p90_pips": Decimal("3.8")}


def test_file_cli_no_db_hashes_and_overwrite_refused(plan, tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_DB_DSN", raising=False)
    plan_path, events_path, ticks = (tmp_path / n for n in ("plan.json", "events.json", "ticks.csv.gz"))
    h7.write_json(plan_path, plan.model_dump(mode="json"))
    events = event_file(plan, "2025-03-07", plan_hash=h7.sha256(plan_path))
    h7.write_json(events_path, events.model_dump(mode="json"))
    write_ticks(ticks, plan, events.stages.explore)
    write_manifest(ticks, plan_path, events_path)
    common = ["--plan", str(plan_path), "--events", str(events_path), "--ticks", str(ticks),
              "--stage", "explore"]
    output = tmp_path / "report"
    assert h7.main(["measure", *common, "--output-dir", str(output)]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["provenance"]["ticks_sha256"] == h7.sha256(ticks)
    assert report["provenance"]["events_sha256"] == h7.sha256(events_path)
    assert "git_commit" in report["provenance"]
    assert "止める" in (output / "report.md").read_text()
    with pytest.raises(FileExistsError):
        h7.main(["measure", *common, "--output-dir", str(output)])
    spread_file = tmp_path / "spreads.json"
    assert h7.main(["spreads", *common, "--output", str(spread_file)]) == 0
    assert "statistics" not in json.loads(spread_file.read_text())


@pytest.mark.parametrize("command", ["events", "export", "spreads", "measure"])
def test_help(command, capsys):
    with pytest.raises(SystemExit) as exc:
        h7.main([command, "--help"])
    assert exc.value.code == 0
    assert "--plan" in capsys.readouterr().out


def test_eurusd_end_quote_is_not_required_for_defined_returns(plan, tmp_path):
    stage = event_file(plan, "2025-03-07").stages.explore
    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage, lambda day, symbol, entries: [
        row for row in entries if symbol != plan.symbols.eurusd or row[0] != 7200])
    day = date(2025, 3, 7)
    result, reason = h7.observe(day, h7.read_quotes(path, plan, stage)[day], plan)
    assert result is not None and reason == ""


def test_bootstrap_reranks_each_resample_with_hand_calculated_result(plan):
    # seed=0 は添字 [3, 3, 0, 2, 4] を選ぶ。同順位を含む再順位化後の
    # C の分子は -30/19、分母の二乗は 4320/361。A は -15/2 と 135/2。
    plan = plan.model_copy(update={"bootstrap_seed": 0, "bootstrap_samples": 1})
    pairs = []
    for m, x, fwd in zip([3, 1, 4, 2, 5], [1, 5, 2, 4, 3], [2, 4, 1, 5, 3], strict=True):
        event = h7.Observation(date(2025, 1, 1), m+x, x-m, m, x, fwd)
        pairs.append((event, event))
    report = h7.bootstrap(pairs, plan)
    assert report["valid"] == 1
    assert report["lower"]["C"] == pytest.approx(-30 / math.sqrt(4320))
    assert report["lower"]["A"] == pytest.approx(-7.5 / math.sqrt(67.5))


def test_unpaired_event_does_not_enter_decision_statistics(plan, tmp_path, monkeypatch):
    days = ["2025-01-17", "2025-02-14", "2025-03-14", "2025-04-18", "2025-05-16", "2025-06-20"]
    stage = event_file(plan, *days).stages.explore
    path = tmp_path / "ticks.csv.gz"
    write_ticks(path, plan, stage)
    quotes = h7.read_quotes(path, plan, stage)
    values = {}
    expected_pairs = []
    for i, event in enumerate(stage.events):
        sample, placebo = sample_pairs()[i]
        sample = replace(sample, day=event.day)
        placebo = replace(placebo, day=event.candidates[0].day)
        values[event.day] = sample
        if i < len(stage.events) - 1:
            values[event.candidates[0].day] = placebo
            expected_pairs.append((sample, placebo))
    # ここでは対の母集団だけを確認し、価格からの計算と損益は別のテストに分ける。
    monkeypatch.setattr(h7, "observe", lambda day, samples, plan: (
        (values[day], "") if day in values else (None, "missing_ticks")))
    monkeypatch.setattr(h7, "pnl_report", lambda *args: {
        "rules": {"m": {"mean_adjusted_pips": Decimal(1)}}})
    monkeypatch.setattr(h7, "execution_quotes", lambda *args: ())
    report = h7.measure(plan, stage, quotes, "explore")
    assert report["paired_events"] == 5
    assert report["unpaired_reference"]["count"] == 1
    assert report["statistics"] == h7.pair_statistics(expected_pairs)
    all_events = h7.statistics([values[e.day] for e in stage.events])
    assert report["statistics"]["A"] != all_events["A"]


def write_manifest(ticks, plan_path, events_path):
    h7.write_json(h7.manifest_path(ticks), {
        "sha256": h7.sha256(ticks), "events_sha256": h7.sha256(events_path),
        "plan_sha256": h7.sha256(plan_path), "stage": "explore"})


@pytest.mark.parametrize("command", ["spreads", "measure"])
def test_ticks_without_manifest_are_rejected_before_any_output(plan, tmp_path, command):
    plan_path, events_path, ticks = (tmp_path / n for n in ("plan.json", "events.json", "ticks.csv.gz"))
    h7.write_json(plan_path, plan.model_dump(mode="json"))
    events = event_file(plan, "2025-03-07", plan_hash=h7.sha256(plan_path))
    h7.write_json(events_path, events.model_dump(mode="json"))
    write_ticks(ticks, plan, events.stages.explore)
    output = tmp_path / "out"
    flag = "--output-dir" if command == "measure" else "--output"
    with pytest.raises(ValueError, match="manifest"):
        h7.main([command, "--plan", str(plan_path), "--events", str(events_path),
                 "--ticks", str(ticks), "--stage", "explore", flag, str(output)])
    assert not output.exists()


def test_tick_manifest_mismatch_is_rejected(plan, tmp_path):
    plan_path, events_path, ticks = (tmp_path / n for n in ("plan.json", "events.json", "ticks.csv.gz"))
    h7.write_json(plan_path, plan.model_dump(mode="json"))
    events = event_file(plan, "2025-03-07", plan_hash=h7.sha256(plan_path))
    h7.write_json(events_path, events.model_dump(mode="json"))
    write_ticks(ticks, plan, events.stages.explore)
    h7.write_json(h7.manifest_path(ticks), {"sha256": "wrong"})
    with pytest.raises(ValueError, match="manifest"):
        h7.main(["spreads", "--plan", str(plan_path), "--events", str(events_path),
                 "--ticks", str(ticks), "--stage", "explore", "--output", str(tmp_path / "s.json")])
