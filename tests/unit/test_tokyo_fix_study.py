"""H10 の暦、気配、計算とファイル境界を外部接続なしで確かめる。"""
from __future__ import annotations

import http.client
import json
import lzma
import math
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.support import tokyo_fix_bi5, tokyo_fix_data, tokyo_fix_plan
from trading.backtest import tokyo_fix_study as h10


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("このテストでは外部に接続しない")

    monkeypatch.setattr(h10.http.client, "HTTPSConnection", forbidden)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=forbidden))
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=object()))


@pytest.fixture
def inputs(tmp_path):
    plan = tokyo_fix_plan()
    holiday, payload, quotes = tokyo_fix_data(plan)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    directory = tmp_path / "data"
    h10.fetch(path, directory, retrieve=lambda url: holiday if url == plan.holiday_csv_url else payload,
              sleep=lambda _: None)
    manifest, holidays, manifest_hash = h10.load_data(plan, h10.digest(path.read_bytes()), directory)
    q = h10.QuoteFile(
        plan_sha256=h10.digest(path.read_bytes()), manifest_sha256=manifest_hash,
        max_tick_id=plan.oanda.max_tick_id, quotes=quotes,
        spreads=h10.spread_summary(quotes, h10.gotobi_dates(plan.oanda, holidays, plan), plan),
    )
    quote_path = tmp_path / "quotes.json"
    h10.write_json(quote_path, q.model_dump(mode="json"))
    return SimpleNamespace(plan=plan, path=path, directory=directory, quote_path=quote_path,
                           manifest=manifest, holidays=holidays, quotes=q, payload=payload)


def test_plan_frozen_and_nested_unknown_keys():
    plan = tokyo_fix_plan()
    with pytest.raises(ValidationError):
        plan.entry = "09:31:00"
    with pytest.raises(ValidationError):
        tokyo_fix_plan(unregistered=True)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        tokyo_fix_plan(oanda=plan.oanda.model_dump() | {"unknown": True})


@pytest.mark.parametrize("scope,field", [
    *((None, name) for name in h10.Plan.model_fields),
    *(("oanda", name) for name in h10.Oanda.model_fields),
])
def test_plan_json_requires_every_field(tmp_path, scope, field):
    values = tokyo_fix_plan().model_dump(mode="json")
    target = values if scope is None else values[scope]
    del target[field]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(values))
    with pytest.raises(ValidationError) as result:
        h10.read_plan(path)
    expected_location = (field,) if scope is None else (scope, field)
    assert [(error["loc"], error["type"]) for error in result.value.errors()] == [
        (expected_location, "missing"),
    ]


@pytest.mark.parametrize("override", [
    {"pip_size": "NaN"}, {"bootstrap_samples": 0}, {"one_sided_level": 1},
    {"gotobi_days": [0]}, {"bank_closed_days": ["02-30"]}, {"entry": "9:30:00"},
    {"exit": "09:29:00"}, {"paper_exit": "10:01:00"}, {"quote_window_seconds": 0},
    {"calibration": {"start": "2019-12-31", "end": "2020-01-31"}},
    {"main": {"start": "2020-03-31", "end": "2020-02-01"}},
    {"holiday_csv_url": "http://example.invalid"}, {"symbol": "UNKNOWN"},
])
def test_plan_rejects_invalid_boundary(override):
    with pytest.raises(ValueError):
        tokyo_fix_plan(**override)


def test_holiday_csv():
    period = h10.Period(start="2019-12-01", end="2020-12-31")
    content = "国民の祝日・休日月日,国民の祝日・休日名称\r\n2019/1/1,架空休日\r\n2020/2/3,架空休日\r\n"
    assert h10.parse_holidays(content.encode("cp932"), period) == {date(2019, 1, 1), date(2020, 2, 3)}


@pytest.mark.parametrize("rows,match", [
    ("date,name\n2020/1/1,架空休日\n", "見出し"),
    ("2020-01-01,架空休日\n", "形式"), ("2020/1/1,架空休日,余分\n", "形式"),
    ("2020/1/1,架空休日\n2020/1/1,架空休日\n", "重複"),
    ("2019/1/1,架空休日\n", "年"),
])
def test_holiday_csv_rejects(rows, match):
    header = "" if rows.startswith("date,") else "国民の祝日・休日月日,国民の祝日・休日名称\n"
    with pytest.raises(ValueError, match=match):
        h10.parse_holidays((header + rows).encode("cp932"), tokyo_fix_plan().fetch_range)


def test_bank_days_and_gotobi_shift_across_month_and_period():
    plan = tokyo_fix_plan()
    holidays = {date(2014, 1, 1), date(2020, 2, 5)}
    period = h10.Period(start="2013-12-28", end="2014-01-06")
    assert h10.business_days(period, holidays, plan) == [date(2013, 12, 30), date(2014, 1, 6)]
    assert h10.gotobi_dates(period, holidays, plan) == {date(2013, 12, 30)}
    # 翌月 5 日の前倒しは月末の前倒しと同じ日になり、二重に数えない。
    modified = plan.model_copy(update={"gotobi_days": (5,)})
    previous = h10.Period(start="2013-12-30", end="2013-12-30")
    assert h10.gotobi_dates(previous, holidays, modified) == {date(2013, 12, 30)}
    feb = h10.Period(start="2020-02-01", end="2020-02-29")
    assert h10.gotobi_dates(feb, holidays, plan) == {
        date(2020, 2, d) for d in (4, 10, 14, 20, 25, 28)
    }
    oct_ = h10.Period(start="2021-10-01", end="2021-10-31")
    assert h10.gotobi_dates(oct_, set(), plan) == {date(2021, 10, d) for d in (5, 8, 15, 20, 25, 29)}


def test_gotobi_next_month_can_move_before_previous_month_end():
    plan = tokyo_fix_plan(gotobi_days=(5,))
    # 翌月 5 日までを閉じても期間末より前の営業日に移る。
    holidays = {date(2020, 2, d) for d in range(1, 6)} | {date(2020, 1, 31)}
    period = h10.Period(start="2020-01-30", end="2020-01-30")
    assert h10.gotobi_dates(period, holidays, plan) == {date(2020, 1, 30)}


def test_price_windows_keep_first_entry_and_last_paper_exit_at_same_timestamp():
    plan = tokyo_fix_plan()
    day = date(2020, 2, 5)
    ticks = h10.decode_day(tokyo_fix_bi5(
        (1799999, 100000, 100000), (1800001, 101000, 101000),
        (1800001, 102000, 102000), (1860000, 103000, 103000),
        (3180000, 104000, 104000), (3540000, 105000, 105000),
        (3599999, 106000, 106000), (3599999, 107000, 107000),
    ), day, plan)
    prices = h10.prices(ticks, day, plan)
    assert prices["entry"] == Decimal(101)
    assert prices["exit"] is None  # 09:53:00 は窓に入らない。
    assert prices["paper_exit"] == Decimal(107)
    assert h10.prices([], day, plan) == dict.fromkeys(h10.SLOTS)
    boundary = h10.decode_day(tokyo_fix_bi5((3540000, 100000, 100000)), day, plan)
    assert h10.prices(boundary, day, plan)["paper_exit"] == Decimal(100)
    from tests.support import make_tick
    at_end = make_tick("100", "100", time=datetime(2020, 2, 5, 1, tzinfo=UTC))
    assert h10.prices([at_end], day, plan)["paper_exit"] is None


@pytest.mark.parametrize("payload", [b"invalid", tokyo_fix_bi5((3600000, 100, 100)),
                                     tokyo_fix_bi5((1, 100, 100), (0, 100, 100)),
                                     tokyo_fix_bi5((0, 100, 101))])
def test_decode_rejects_corrupt_or_invalid_ticks(payload):
    with pytest.raises((ValueError, lzma.LZMAError)):
        h10.decode_day(payload, date(2020, 2, 5), tokyo_fix_plan())


def test_main_paper_and_oanda_returns_hand_calculated():
    plan = tokyo_fix_plan()
    price = dict(zip(h10.SLOTS, map(Decimal, ("100", "101", "100", "102", "101"))))
    spreads = {slot: h10.Spread(mean_pips=Decimal(v), days=1)
               for slot, v in zip(h10.SLOTS, (1, 3, 2, 4, 6))}
    result, absent = h10.returns(price, spreads, plan)
    assert result == {"g": 100.0, "c": 2.0, "n": 98.0}
    assert absent == []
    paper, _ = h10.returns(price, spreads, plan, paper=True)
    assert paper == pytest.approx({"g": 200 + 10000 / 102, "c": 8, "n": 192 + 10000 / 102})
    _, _, quotes = tokyo_fix_data(plan)
    oanda, _ = h10.oanda_return(next(iter(quotes.values())))
    assert oanda == {"g": 2.0, "c": 1.0, "n": 1.0}
    assert h10.returns(price | {"exit": None}, spreads, plan) == (None, ["exit"])
    assert h10.oanda_return(dict.fromkeys(h10.SLOTS)) == (None, ["entry", "exit"])
    with pytest.raises(ValueError, match="スプレッド"):
        h10.returns(price, spreads | {"entry": h10.Spread(mean_pips=None, days=0)}, plan)


def test_spread_summary_only_counts_present_gotobi_quotes():
    plan = tokyo_fix_plan()
    _, _, quotes = tokyo_fix_data(plan)
    d1, d2, other = date(2020, 2, 5), date(2020, 2, 10), date(2020, 2, 6)
    quotes[d1]["entry"] = quotes[d1]["entry"].model_copy(update={"ask": Decimal("100.025")})
    quotes[d2]["exit"] = None
    quotes[other]["entry"] = quotes[other]["entry"].model_copy(update={"ask": Decimal(200)})
    summary = h10.spread_summary(quotes, {d1, d2}, plan)
    assert summary["entry"] == h10.Spread(mean_pips=Decimal(2), days=2)
    assert summary["exit"] == h10.Spread(mean_pips=Decimal(1), days=1)
    assert h10.spread_summary(quotes, set(), plan)["entry"].mean_pips is None


def test_bootstrap_month_units_weight_event_counts_and_call_order():
    plan = tokyo_fix_plan(bootstrap_samples=1000)
    observations = [(date(2020, 1, 5), 0.0), (date(2020, 1, 10), 0.0), (date(2020, 3, 5), 9.0)]
    result = h10.bootstrap(observations, plan.fetch_range, plan, "test")
    assert result == {"lower": 0, "upper": 9, "samples": 1000, "undefined_samples": 0, "unit_months": 2}
    # 3 暦月を全部抜くブロックでは、空の 2 月を含めても合計 / 日数は常に 3。
    circular_plan = plan.model_copy(update={"secondary_block_months": 3})
    circular = h10.bootstrap(observations, plan.fetch_range, circular_plan, "circular", circular=True)
    assert circular["unit_months"] == 3
    assert circular["lower"] == circular["upper"] == 3
    h10.bootstrap(observations, plan.fetch_range, plan, "other")
    assert result == h10.bootstrap(observations, plan.fetch_range, plan, "test")


def test_bootstrap_keeps_whole_month_together_and_exposes_undefined_samples():
    plan = tokyo_fix_plan(secondary_block_months=1)
    observations = [(date(2020, 1, 5), -10.0), (date(2020, 1, 10), 10.0)]
    result = h10.bootstrap(observations, plan.fetch_range, plan, "one-month")
    assert result["lower"] == result["upper"] == 0
    circular = h10.bootstrap(observations, plan.fetch_range, plan, "sparse", circular=True)
    assert circular["undefined_samples"] > 0
    assert circular["lower"] is circular["upper"] is None
    empty = h10.bootstrap([], plan.fetch_range, plan, "empty")
    assert empty["undefined_samples"] == plan.bootstrap_samples
    assert empty["lower"] is None


def test_statistics_exclusions_trim_and_annual_sharpe():
    plan = tokyo_fix_plan(trim_fraction=0.25)
    rows = [{"date": f"2020-02-{day:02d}", "main": {"g": net + 1, "c": 1, "n": net}}
            for day, net in enumerate((-2, 1, 3, 6), 1)]
    rows.append({"date": "2020-02-05", "main": None})
    stats = h10.statistics(rows, plan.main, plan, "test", "main")
    assert stats["days"] == 4 and stats["excluded_days"] == 1
    assert stats["excluded_dates"] == ["2020-02-05"]
    assert stats["mean_g"] == 3 and stats["mean_c"] == 1 and stats["mean_n"] == 2
    assert stats["trimmed_mean_n"] == 2
    assert stats["std_n"] == pytest.approx(math.sqrt(34 / 3))
    assert stats["annual_sharpe"] == pytest.approx(2 / math.sqrt(34 / 3) * math.sqrt(4 / (60 / 365.25)))


@pytest.mark.parametrize("lower,upper,o_mean,o_upper,verdict,support,reject", [
    (0.1, 0.7, 0.1, 0.8, "支持", True, False),
    (-1, 0.49, 1, 1, "棄却", False, True),
    (0, 0.5, 0, 0.5, "判定不能", False, False),
    (0.1, 0.7, 0.1, 0.49, "棄却", True, True),
    (0.1, 0.49, 0.1, 0.8, "棄却", True, True),
    (None, None, None, None, "判定不能", False, False),
])
def test_decision(lower, upper, o_mean, o_upper, verdict, support, reject):
    result = h10.decide({"primary_interval": {"lower": lower, "upper": upper}},
                        {"mean_n": o_mean, "primary_interval": {"upper": o_upper}}, tokyo_fix_plan())
    assert result == {"verdict": verdict, "support": support, "reject": reject}


def test_fetch_exact_bytes_missing_empty_resume_and_manifest(inputs):
    p = inputs
    manifest = json.loads((p.directory / "manifest.json").read_bytes())
    assert manifest["plan_sha256"] == h10.digest(p.path.read_bytes())
    for day, info in manifest["files"].items():
        payload = (p.directory / "dukascopy" / f"{day}.bi5").read_bytes()
        assert payload == p.payload
        assert info == {"sha256": h10.digest(payload), "bytes": len(payload), "ticks": 5}
    with pytest.raises(FileExistsError):
        h10.fetch(p.path, p.directory)
    (p.directory / "manifest.json").unlink()
    selected = sorted(manifest["files"])[:3]
    for day in selected[:2]:
        (p.directory / "dukascopy" / f"{day}.bi5").unlink()
    (p.directory / "dukascopy" / f"{selected[2]}.bi5").write_bytes(b"")
    (p.directory / "dukascopy" / "unrelated.bi5").write_bytes(b"ignored")
    calls, waits = [], []

    def retrieve(url):
        calls.append(url)
        return None if len(calls) == 1 else b""

    h10.fetch(p.path, p.directory, retrieve=retrieve, sleep=waits.append)
    result = json.loads((p.directory / "manifest.json").read_bytes())
    assert len(calls) == 2 and waits == [0.5]
    assert result["missing"] == selected[:1]
    assert (p.directory / "dukascopy" / f"{selected[0]}.missing").read_bytes() == b"404"
    assert result["files"][selected[1]]["ticks"] == result["files"][selected[2]]["ticks"] == 0
    assert "unrelated" not in result["files"]
    (p.directory / "manifest.json").unlink()
    h10.fetch(p.path, p.directory, retrieve=lambda _: pytest.fail("再取得しない"), sleep=waits.append)


def test_fetch_retries_and_keeps_files_after_exhaustion(tmp_path):
    plan = tokyo_fix_plan()
    holiday, payload, _ = tokyo_fix_data(plan)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    directory = tmp_path / "data"
    waits, calls = [], []

    def retrieve(url):
        calls.append(url)
        if len(calls) == 1:
            return holiday
        if len(calls) in (2, 3):
            raise TimeoutError("架空タイムアウト")
        if len(calls) == 4:
            return payload
        raise http.client.RemoteDisconnected("架空切断")

    with pytest.raises(RuntimeError, match="同じコマンドで再開"):
        h10.fetch(path, directory, retrieve=retrieve, sleep=waits.append)
    assert (directory / "syukujitsu.csv").read_bytes() == holiday
    assert (directory / "dukascopy" / "2020-01-06.bi5").read_bytes() == payload
    assert not (directory / "manifest.json").exists()
    assert [wait for wait in waits if wait != 0.5] == [5, 15, 5, 15, 45, 120, 300] + [600] * 12


def test_fetch_rejects_corrupt_payload_before_saving(tmp_path):
    plan = tokyo_fix_plan()
    holiday, _, _ = tokyo_fix_data(plan)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    directory = tmp_path / "data"
    with pytest.raises(lzma.LZMAError):
        h10.fetch(path, directory, retrieve=lambda url: holiday if url == plan.holiday_csv_url else b"bad",
                  sleep=lambda _: None)
    assert not (directory / "manifest.json").exists()
    assert not list((directory / "dukascopy").iterdir())


def test_https_keep_alive_status_reconnect_and_close(monkeypatch):
    instances = []
    replies = iter([(200, b"one"), (404, b"not found"), (500, b"error"), (200, b"two")])

    class Connection:
        def __init__(self, host, timeout):
            self.host, self.timeout, self.closed, self.requests = host, timeout, False, []
            instances.append(self)

        def request(self, method, path, headers):
            self.requests.append((method, path, headers))

        def getresponse(self):
            status, body = next(replies)
            return SimpleNamespace(status=status, read=lambda: body)

        def close(self):
            self.closed = True

    monkeypatch.setattr(h10.http.client, "HTTPSConnection", Connection)
    download = h10.HTTPSDownloader()
    assert download("https://example.invalid/a") == b"one"
    assert download("https://example.invalid/b") is None
    assert len(instances) == 1
    with pytest.raises(OSError, match="500"):
        download("https://example.invalid/c")
    assert instances[0].closed
    assert download("https://example.invalid/d?query=1") == b"two"
    assert len(instances) == 2
    expected_headers = {"Connection": "keep-alive", "User-Agent": "fx-trading-platform-research/1.0"}
    assert instances[0].requests == [("GET", path, expected_headers) for path in ("/a", "/b", "/c")]
    assert instances[1].requests == [("GET", "/d?query=1", expected_headers)]
    download.close()
    assert instances[1].closed


@pytest.mark.parametrize("target", ["plan", "manifest_plan", "holiday", "bi5", "missing",
                                     "quotes_plan", "quotes_manifest", "quotes_max_tick_id",
                                     "quote_window", "spread", "manifest_coverage"])
def test_measure_integrity_failures_leave_no_output(inputs, tmp_path, target):
    p = inputs
    manifest_path = p.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    q = json.loads(p.quote_path.read_bytes())
    if target == "plan":
        p.path.write_bytes(p.path.read_bytes() + b" ")
    elif target == "manifest_plan":
        manifest["plan_sha256"] = "0" * 64
    elif target == "holiday":
        with (p.directory / "syukujitsu.csv").open("ab") as out:
            out.write(b" ")
    elif target == "bi5":
        day = next(iter(manifest["files"]))
        (p.directory / "dukascopy" / f"{day}.bi5").write_bytes(b"modified")
    elif target == "missing":
        day = next(iter(manifest["files"]))
        del manifest["files"][day]
        manifest["missing"].append(day)
    elif target.startswith("quotes_"):
        field = {"quotes_plan": "plan_sha256", "quotes_manifest": "manifest_sha256",
                 "quotes_max_tick_id": "max_tick_id"}[target]
        q[field] = 9999 if field == "max_tick_id" else "0" * 64
    elif target == "quote_window":
        q["quotes"]["2020-02-05"]["entry"]["event_time"] = "2020-02-05T02:31:00Z"
    elif target == "spread":
        q["spreads"]["entry"]["mean_pips"] = "0"
    else:
        manifest["files"].pop(next(iter(manifest["files"])))
    manifest_path.write_bytes(h10.json_bytes(manifest))
    p.quote_path.write_bytes(h10.json_bytes(q))
    with pytest.raises((ValueError, FileNotFoundError)):
        h10.measure_files(p.path, p.directory, p.quote_path, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_measure_report_expected_values_and_no_overwrite(inputs, tmp_path, monkeypatch):
    p = inputs
    monkeypatch.setattr(h10, "git_state", lambda: {"git_commit": "test-code", "git_dirty": True})
    output = tmp_path / "report"
    assert h10.main(["measure", "--plan", str(p.path), "--data-dir", str(p.directory),
                     "--quotes", str(p.quote_path), "--output-dir", str(output)]) == 0
    report = json.loads((output / "report.json").read_bytes())
    assert report["decision"]["verdict"] == "支持"
    main = report["groups"]["main_gotobi"]
    assert main["days"] == 13 and main["excluded_days"] == 0
    assert (main["mean_g"], main["mean_c"], main["mean_n"]) == (2, 1, 1)
    assert main["primary_interval"]["lower"] == main["primary_interval"]["upper"] == 1
    assert report["groups"]["oanda_gotobi"]["mean_n"] == 1
    assert report["cost_sensitivity"][0]["mean_n"] == 2
    assert report["cost_sensitivity"][1]["mean_n"] == 0
    assert report["broker_difference"] == {"days": 13, "mean_bp": 0, "std_bp": 0, "excluded_dates": []}
    assert report["gotobi_minus_other_mean_n"] == 0
    assert report["annual"] == [{"year": 2020, "days": 13, "mean_n": 1}]
    assert len(report["best_days"]) == len(report["worst_days"]) == 5
    assert len(report["daily"]) == p.manifest.business_days
    assert report["plan"] == p.plan.model_dump(mode="json")
    assert report["provenance"]["quotes_sha256"] == h10.digest(p.quote_path.read_bytes())
    assert (output / "report.md").read_text().startswith("# H10 仲値前の研究\n")
    with pytest.raises(FileExistsError):
        h10.measure_files(p.path, p.directory, p.quote_path, output)


@pytest.mark.parametrize("value,formatted", [
    (1.23456, "1.235"), (-1.23456, "-1.235"), (0, "0.000"), (None, "—"),
])
def test_markdown_formats_estimates_intervals_and_missing_values(value, formatted):
    interval = {"lower": value, "upper": value}
    report = {
        "decision": {"verdict": "判定不能"},
        "groups": {"main_gotobi": {"days": 13, "excluded_days": 0,
                                    "mean_g": value, "mean_c": value, "mean_n": value,
                                    "primary_interval": interval, "secondary_interval": interval}},
        "cost_sensitivity": [{"multiplier": 2, "mean_n": value, "primary_interval": interval}],
        "annual": [{"year": 2020, "days": 13, "mean_n": value}],
        "gotobi_minus_other_mean_n": value, "provenance": {},
    }
    markdown = h10.render_markdown(report)
    assert "| 主・五十日 | 13 | 0 | " + " | ".join([formatted] * 7) + " |" in markdown
    assert "| 2.000 | " + " | ".join([formatted] * 3) + " |" in markdown
    assert f"| 2020 | 13 | {formatted} |" in markdown
    assert f"五十日とそれ以外の平均の差: {formatted} bp。" in markdown
    assert "None" not in markdown


def test_measure_missing_ticks_excluded_per_trade(inputs, tmp_path):
    p = inputs
    (p.directory / "dukascopy" / "2020-02-05.bi5").write_bytes(b"")
    only_main = tokyo_fix_bi5((1800000, 100000, 100000), (3120000, 100020, 100020))
    (p.directory / "dukascopy" / "2020-02-10.bi5").write_bytes(only_main)
    missing = p.directory / "dukascopy" / "2020-02-14.bi5"
    missing.unlink()
    missing.with_suffix(".missing").write_bytes(b"404")
    (p.directory / "manifest.json").unlink()
    h10.fetch(p.path, p.directory, retrieve=lambda _: pytest.fail("再取得しない"), sleep=lambda _: None)
    q = p.quotes.model_copy(update={"manifest_sha256": h10.digest((p.directory / "manifest.json").read_bytes())})
    p.quote_path.write_text(q.model_dump_json())
    report = h10.measure_files(p.path, p.directory, p.quote_path, tmp_path / "report")
    assert report["groups"]["main_gotobi"]["excluded_dates"] == ["2020-02-05", "2020-02-14"]
    assert report["groups"]["paper_main_gotobi"]["excluded_dates"] == ["2020-02-05", "2020-02-10", "2020-02-14"]
    assert report["broker_difference"]["excluded_dates"] == ["2020-02-05", "2020-02-14"]
    row = next(row for row in report["daily"] if row["date"] == "2020-02-05")
    assert row["main"] is None
    assert row["excluded_reasons"]["main"] == ["entry", "exit"]


def test_measure_rechecks_bytes_read_after_initial_validation(inputs, tmp_path, monkeypatch):
    p = inputs
    original = h10.load_quotes

    def replace_after_validation(*args):
        result = original(*args)
        (p.directory / "dukascopy" / "2020-02-05.bi5").write_bytes(b"")
        return result

    monkeypatch.setattr(h10, "load_quotes", replace_after_validation)
    with pytest.raises(ValueError, match="測定時の bi5 の sha256"):
        h10.measure_files(p.path, p.directory, p.quote_path, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_oanda_exclusions_and_broker_difference_use_paired_gross_returns(inputs, tmp_path):
    p = inputs
    selected = {date(2020, 2, 5): Decimal("100.01"), date(2020, 2, 10): Decimal("100.03")}
    quotes = {}
    for day, slots in p.quotes.quotes.items():
        if day in selected:
            quotes[day] = slots | {"exit": slots["exit"].model_copy(update={
                "bid": selected[day] - Decimal(".005"),
                "ask": selected[day] + Decimal(".005"),
            })}
        else:
            quotes[day] = slots | {"entry": None, "exit": None}
    spreads = h10.spread_summary(quotes, h10.gotobi_dates(p.plan.oanda, p.holidays, p.plan), p.plan)
    q = p.quotes.model_copy(update={"quotes": quotes, "spreads": spreads})
    p.quote_path.write_text(q.model_dump_json())
    report = h10.measure_files(p.path, p.directory, p.quote_path, tmp_path / "paired")
    oanda = report["groups"]["oanda_gotobi"]
    assert oanda["days"] == 2 and oanda["excluded_days"] == 11
    assert oanda["mean_n"] == 1
    # Dukascopy - OANDA は +1 bp と -1 bp。コスト控除後の値を引かない。
    assert report["broker_difference"]["days"] == 2
    assert report["broker_difference"]["mean_bp"] == 0
    assert report["broker_difference"]["std_bp"] == pytest.approx(math.sqrt(2))


def test_quotes_cli_readonly_and_prechecks(inputs, tmp_path, monkeypatch):
    p = inputs
    calls, queries = [], []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self, **kwargs):
            return self

        def execute(self, sql, params):
            queries.append((sql, params))

        def fetchone(self):
            return None

    def connect(dsn, **kwargs):
        assert dsn == "test-dsn"
        assert kwargs == {"options": "-c default_transaction_read_only=on"}
        calls.append(True)
        return Connection()

    monkeypatch.setenv("TRADING_DB_DSN", "test-dsn")
    monkeypatch.setattr(sys.modules["psycopg"], "connect", connect)
    output = tmp_path / "db-quotes.json"
    args = ["quotes", "--plan", str(p.path), "--data-dir", str(p.directory), "--output", str(output)]
    assert h10.main(args) == 0
    result = h10.QuoteFile.model_validate_json(output.read_bytes())
    assert len(calls) == 1 and len(queries) == len(result.quotes) * 5
    assert all(spread.days == 0 for spread in result.spreads.values())
    sql, params = queries[0]
    assert "symbol = %s AND source = %s AND id <= %s" in sql
    assert "ORDER BY event_time, id LIMIT 1" in sql
    assert params[:3] == (p.plan.symbol, "MT5", 10000)
    assert "ORDER BY event_time DESC, id DESC LIMIT 1" in queries[4][0]
    assert params[3:] == (datetime(2020, 2, 3, 2, 30, tzinfo=UTC), datetime(2020, 2, 3, 2, 31, tzinfo=UTC))
    # 夏時間開始後はサーバーラベルが 1 時間進む。
    assert any(args[3] == datetime(2020, 3, 9, 3, 30, tzinfo=UTC) for _, args in queries)
    with pytest.raises(FileExistsError):
        h10.main(args)
    assert len(calls) == 1
    output.unlink()
    p.path.write_bytes(p.path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="sha256"):
        h10.main(args)
    assert len(calls) == 1 and not output.exists()


@pytest.mark.parametrize("command", ["fetch", "quotes", "measure"])
def test_command_help(command, capsys):
    with pytest.raises(SystemExit) as result:
        h10.main([command, "--help"])
    assert result.value.code == 0
    assert "--plan" in capsys.readouterr().out
