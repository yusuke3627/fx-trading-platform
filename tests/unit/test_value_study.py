"""H9 の入力境界・時点・順位・合成コストを架空値で検証する。"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.support import carry_data, carry_plan, value_cpi_data, value_plan
from tests.unit.test_carry_study import save_data, wedge_file
from trading.backtest import carry_study as h8
from trading.backtest import value_study as h9


@pytest.fixture
def plan():
    return value_plan()


@pytest.fixture
def carry(plan):
    base = carry_plan()
    currencies = [c.model_dump() | {"code": code}
                  for c, code in zip(base.currencies, plan.first_holding_months, strict=True)]
    return carry_plan(currencies=currencies)


@pytest.fixture
def data(carry):
    result = carry_data(carry)
    for c in carry.currencies:
        for month in h8.months(h8.Period(start="2013-01", end="2019-12")):
            result[c.fx_series][date.fromisoformat(month + "-01")] = Decimal(1)
        result[c.fx_series][date(2019, 12, 31)] = Decimal(1)
    return result


@pytest.fixture
def cpi(plan):
    return value_cpi_data(plan)


def source_for(plan, name):
    return next(s for s in plan.sources if s.name == name)


def encoded_csv(source, observations):
    """公式ファイルと同じ構造で、架空の値だけをシリアライズする。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";" if source.format == "snb" else ",")
    if source.format == "fred":
        writer.writerow(["observation_date", source.series])
        writer.writerows((month + "-01", value) for month, value in observations)
    elif source.format == "oecd":
        writer.writerow(["DATAFLOW", "REF_AREA", "FREQ", "METHODOLOGY", "MEASURE", "UNIT_MEASURE",
                         "EXPENDITURE", "ADJUSTMENT", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"])
        for month, value in observations:
            period = month if source.freq == "M" else f"{month[:4]}-Q{int(month[-2:]) // 3}"
            writer.writerow(["TEST", source.ref_area, source.freq, "N", "CPI", "IX", "_T", "N",
                             period, value, "A"])
    elif source.format == "stat_jp":
        writer.writerows([["類・品目", "総合", "別の項目"], ["Group/Item", "All items", "Other"],
                          ["ウエイト", "10000", "100"]])
        writer.writerows((month.replace("-", ""), value, 5) for month, value in observations)
    elif source.format == "eurostat":
        writer.writerow(["freq", "unit", "coicop18", "geo", "TIME_PERIOD", "OBS_VALUE", "OBS_FLAG"])
        writer.writerows(("M", source.unit, source.coicop18, source.geo, month, value, "")
                         for month, value in observations)
    elif source.format == "ons":
        writer.writerows([["Title", "Test index"], ["CDID", source.cdid], ["Unit", "Index"],
                          ["Important notes", ""], ["2019", 999], ["2020 Q1", 888]])
        names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        writer.writerows((f"{month[:4]} {names[int(month[-2:]) - 1]}", value)
                         for month, value in observations)
    else:
        writer.writerows([["CubeId", source.cube], ["PublishingDate", "2020-08-01 10:00"], [],
                          ["Date", "D0", "Value"]])
        for month, value in observations:
            writer.writerows([[month, source.d0, value], [month, "OTHER", ""]])
    encoding = "cp932" if source.format == "stat_jp" else "utf-8-sig"
    if source.format == "fred":
        encoding = "utf-8"
    return buffer.getvalue().encode(encoding)


def test_plan_and_view_validate_boundaries_and_preserve_h8_settings(plan, carry):
    with pytest.raises(ValidationError):
        plan.bootstrap_seed = 8
    patches = [
        {"unknown": 1}, {"bootstrap_seed": True}, {"cpi_lag_months": 0},
        {"cpi_max_lag_months": 1}, {"fx_average_last_offset": 67}, {"combination_weight": .6},
        {"post": {"start": "2020-03", "end": "2020-06"}},
        {"sources": [plan.sources[0]] * 10},
        {"price_sources": plan.price_sources | {"USD": ["absent"]}},
        {"price_sources": plan.price_sources | {"JPY": ["jpy_old"]}},
        {"jpy_revision_windows": [plan.jpy_revision_windows[0]] * 2},
        {"carry": plan.carry.model_dump() | {"report_sha256": "bad"}},
        {"sources": [plan.sources[0].model_dump() | {"extra": 1}, *plan.sources[1:]]},
    ]
    for patch in patches:
        with pytest.raises(ValidationError):
            h9.Plan.model_validate(plan.model_dump() | patch)
    view = h9.carry_view(plan, carry)
    for field in h8.Plan.model_fields:
        if field not in ("study_version", "bootstrap_seed", "full", "post", "pre", "currencies"):
            assert getattr(view, field) == getattr(carry, field)
    assert view.bootstrap_seed == plan.bootstrap_seed
    assert view.currencies[1].first_holding_month == "2020-04"
    assert h9.carry_view(plan, carry, combo=True).currencies == carry.currencies
    with pytest.raises(ValueError, match="通貨一覧"):
        h9.carry_view(plan.model_copy(update={"first_holding_months": {"AAA": "2020-01"}}), carry)


@pytest.mark.parametrize("name", ["usd", "jpy_old", "jpy_new", "eur", "rpi", "aud", "chf"])
def test_parse_formats_unsorted_and_empty(plan, name):
    source = source_for(plan, name)
    raw = encoded_csv(source, [("2020-06", "102"), ("2019-12", ""), ("2020-03", "101.25")])
    assert h9.parse_csv(raw, source) == {"2020-03": Decimal("101.25"), "2020-06": Decimal(102)}


@pytest.mark.parametrize("name", ["usd", "jpy_old", "jpy_new", "eur", "rpi", "aud", "chf"])
@pytest.mark.parametrize("value", ["-1", "0", "NaN", "Infinity", "abc"])
def test_parse_rejects_invalid_values(plan, name, value):
    source = source_for(plan, name)
    with pytest.raises(ValueError):
        h9.parse_csv(encoded_csv(source, [("2020-03", value)]), source)


@pytest.mark.parametrize("name", ["usd", "jpy_old", "jpy_new", "eur", "rpi", "aud", "chf"])
def test_parse_rejects_duplicate_period_including_empty(plan, name):
    source = source_for(plan, name)
    with pytest.raises(ValueError):
        h9.parse_csv(encoded_csv(source, [("2020-03", ""), ("2020-03", "101")]), source)


@pytest.mark.parametrize("name,old,new", [
    ("usd", "TEST_USD", "OTHER"), ("usd", "2020-03-01", "2020-03-02"),
    ("usd", "2020-03-01", "2020-13-01"),
    ("jpy_old", "TIME_PERIOD", "PERIOD"), ("jpy_old", "EXPENDITURE", "OTHER"),
    ("jpy_old", "TEST_JP", "OTHER"), ("jpy_old", ",M,N,CPI,IX,_T,N,", ",M,Y,CPI,IX,_T,N,"),
    ("jpy_old", ",M,N,CPI,IX,_T,N,", ",M,N,PPI,IX,_T,N,"),
    ("jpy_old", ",M,N,CPI,IX,_T,N,", ",M,N,CPI,PCT,_T,N,"),
    ("jpy_old", ",M,N,CPI,IX,_T,N,", ",M,N,CPI,IX,_X,N,"),
    ("jpy_old", ",M,N,CPI,IX,_T,N,", ",M,N,CPI,IX,_T,Y,"),
    ("jpy_old", ",M,N,CPI,IX,_T,N,", ",Q,N,CPI,IX,_T,N,"),
    ("jpy_old", "2020-03", "2020-3"), ("aud", "2020-Q1", "2020-Q5"),
    ("aud", "2020-Q1", "2020-03"),
    ("jpy_new", "総合", "食料"), ("jpy_new", "All items", "Other"),
    ("jpy_new", "202003", "202013"), ("jpy_new", "202003", "2020-03"),
    ("eur", "OBS_VALUE", "VALUE"), ("eur", "TEST_DE", "OTHER"),
    ("eur", "M,I25,TOTAL,", "Q,I25,TOTAL,"), ("eur", "I25", "OTHER"),
    ("eur", "TOTAL", "OTHER"), ("eur", "2020-03", "2020-00"),
    ("rpi", "CDID", "ID"), ("rpi", "TEST_RPI", "OTHER"),
    ("rpi", "2020 MAR", "2020 XYZ"), ("rpi", "2020 MAR", "2020 Mar"),
    ("chf", "CubeId", "Id"), ("chf", "test_cube", "OTHER"),
    ("chf", "TEST_INDEX", "OTHER"), ("chf", "Value", "Other"),
    ("chf", "2020-03", "2020-13"),
])
def test_parse_rejects_headers_identifiers_and_periods(plan, name, old, new):
    source = source_for(plan, name)
    encoding = "cp932" if name == "jpy_new" else "utf-8"
    raw = encoded_csv(source, [("2020-03", 101)]).decode(encoding).replace(old, new).encode(encoding)
    with pytest.raises(ValueError):
        h9.parse_csv(raw, source)


def save_cpi(tmp_path, plan, cpi):
    path = tmp_path / "value_plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    bodies = {s.url: encoded_csv(s, list(reversed(list(cpi[s.name].items())))) for s in plan.sources}
    directory = tmp_path / "cpi"
    h9.fetch(path, directory, retrieve=bodies.__getitem__)
    return path, directory, bodies


def test_fetch_preserves_bytes_manifest_and_refuses_overwrite(tmp_path, plan, cpi):
    path, directory, bodies = save_cpi(tmp_path, plan, cpi)
    manifest = h9.Manifest.model_validate_json((directory / "manifest.json").read_bytes())
    assert manifest.plan_sha256 == h8.digest(path.read_bytes())
    assert manifest.retrieved_at.utcoffset() == timedelta(0)
    for source in plan.sources:
        raw = bodies[source.url]
        assert (directory / f"{source.name}.csv").read_bytes() == raw
        entry = manifest.sources[source.name]
        assert entry.sha256 == h8.digest(raw)
        assert entry.url == source.url
        assert entry.values == len(cpi[source.name])
        assert entry.first_month == min(cpi[source.name])
        assert entry.last_month == max(cpi[source.name])
    assert h9.load_data(plan, manifest.plan_sha256, directory)[0] == cpi
    with pytest.raises(FileExistsError):
        h9.fetch(path, directory, retrieve=lambda _: pytest.fail("取得してはいけない"))


def test_download_sends_user_agent(monkeypatch):
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response(b"body")

    monkeypatch.setattr(h9, "urlopen", fake_urlopen)
    assert h9.download("https://example.invalid/cpi.csv") == b"body"
    request, timeout = requests[0]
    assert request.full_url == "https://example.invalid/cpi.csv"
    assert request.get_header("User-agent") == h9.USER_AGENT
    assert timeout == 60


def test_fetch_invalid_response_has_no_manifest(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    with pytest.raises(ValueError):
        h9.fetch(path, tmp_path / "bad", retrieve=lambda _: b"bad CSV")
    assert not (tmp_path / "bad" / "manifest.json").exists()


def test_monthly_fallback_lag_limit_and_missing_old_value(plan):
    data = {"usd": {"2014-09": Decimal(100), "2019-09": Decimal(120)}}
    result = h9.price_change(plan, data, "USD", "2019-12")
    assert result["e"] == "2019-09"
    assert result["change"] == pytest.approx(math.log(1.2))
    assert h9.price_change(plan, data, "USD", "2020-01")["e"] == "2019-09"
    with pytest.raises(ValueError, match="ラグ"):
        h9.price_change(plan, data, "USD", "2020-02")
    del data["usd"]["2014-09"]
    with pytest.raises(ValueError, match="2014-09"):
        h9.price_change(plan, data, "USD", "2019-12")
    with pytest.raises(ValueError, match="公表済み"):
        h9.price_change(plan, data, "USD", "2014-01")


@pytest.mark.parametrize("month,e", [("2020-01", "2019-09"), ("2020-02", "2019-12"),
                                       ("2020-03", "2019-12"), ("2020-04", "2019-12"),
                                       ("2020-05", "2020-03")])
def test_quarter_availability(plan, cpi, month, e):
    assert h9.price_change(plan, cpi, "AUD", month)["e"] == e


def test_jpy_link_and_revision_exemption(plan):
    data = {"jpy_old": {"2014-12": Decimal(50), "2019-12": Decimal(100),
                         "2015-01": Decimal(900), "2020-01": Decimal(200)},
            "jpy_new": {"2020-01": Decimal(100), "2021-06": Decimal(700),
                         "2021-07": Decimal(110)}}
    result = h9.price_change(plan, data, "JPY", "2021-08")
    assert result["substituted"] and result["e"] == "2019-12"
    assert result["e_before_substitution"] == "2021-06"
    assert result["change"] == pytest.approx(math.log(2))
    data["jpy_old"]["2016-07"] = Decimal(100)
    result = h9.price_change(plan, data, "JPY", "2021-09")
    assert not result["substituted"]
    assert result["e"] == "2021-07"
    assert result["change"] == pytest.approx(math.log(110 / 50))
    assert result["sources"] == ["jpy_old", "jpy_new"]
    del data["jpy_old"]["2020-01"]
    with pytest.raises(ValueError, match="接続月"):
        h9.price_change(plan, data, "JPY", "2021-09")


@pytest.mark.parametrize("window_index", range(9))
def test_jpy_all_revision_windows(plan, window_index):
    window = plan.jpy_revision_windows[window_index]
    data = {"jpy_old": {plan.jpy_link_month: Decimal(100), window.end: Decimal(900),
                         window.substitute: Decimal(110),
                         h8.shift_month(window.substitute, -60): Decimal(100)},
            "jpy_new": {plan.jpy_link_month: Decimal(100), window.end: Decimal(900)}}
    result = h9.price_change(plan, data, "JPY", h8.shift_month(window.end, 2))
    assert result["e"] == window.substitute
    assert result["change"] == pytest.approx(math.log(1.1))


def test_gbp_switch_uses_same_series_at_both_ends(plan):
    data = {"rpi": {"1995-12": Decimal(100), "2000-12": Decimal(150),
                     "1996-01": Decimal(100), "2001-01": Decimal(300)},
            "cpi": {"1995-12": Decimal(10), "2000-12": Decimal(100),
                     "1996-01": Decimal(50), "2001-01": Decimal(60)}}
    before = h9.price_change(plan, data, "GBP", "2001-02")
    after = h9.price_change(plan, data, "GBP", "2001-03")
    assert before["sources"] == ["rpi"]
    assert before["change"] == pytest.approx(math.log(1.5))
    assert after["sources"] == ["cpi"]
    assert after["change"] == pytest.approx(math.log(1.2))
    del data["cpi"]["2001-01"]
    assert h9.price_change(plan, data, "GBP", "2001-03")["sources"] == ["rpi"]


def test_signal_previous_date_average_and_value(plan, carry, data, cpi):
    view = h9.carry_view(plan, carry)
    active = [c for c in view.currencies if c.first_holding_month <= "2020-01"]
    currency = next(c for c in active if c.code == "GBP")
    # 逆数建ての通貨も USD 価格へそろえ、各月の最初の観測だけを平均する。
    for index, month in enumerate(h8.months(h8.Period(start="2014-07", end="2015-07"))):
        first = date.fromisoformat(month + "-02")
        data[currency.fx_series].pop(date.fromisoformat(month + "-01"))
        price = Decimal(index + 1)
        data[currency.fx_series][first] = Decimal(1) / price
        data[currency.fx_series][date.fromisoformat(month + "-03")] = Decimal("0.001")
    data[currency.fx_series][date(2019, 12, 31)] = Decimal(1) / Decimal(14)
    cpi["usd"].update({"2014-11": Decimal(100), "2019-11": Decimal(110)})
    cpi["cpi"].update({"2014-11": Decimal(100), "2019-11": Decimal(120)})
    signals = h9.month_signals(plan, data, cpi, "2020-01", active)
    signal = signals["currencies"]["GBP"]
    assert signals["d_m"] == "2020-01-01"
    assert signals["e_USD"] == "2019-11"
    assert signal["b"] == "2019-12-31"
    assert signal["S_bar"] == pytest.approx(7)
    assert signal["V"] == pytest.approx(-(math.log(2) + math.log(1.2) - math.log(1.1)))
    weights = h9.value_weights(view, signals)
    data[currency.fx_series][date(2020, 1, 1)] = Decimal("0.00001")
    changed = h9.month_signals(plan, data, cpi, "2020-01", active)
    assert changed == signals
    assert h9.value_weights(view, changed) == weights
    data[currency.fx_series] = {d: v for d, v in data[currency.fx_series].items()
                                if d.isoformat()[:7] != "2014-07"}
    with pytest.raises(ValueError, match="2014-07"):
        h9.month_signals(plan, data, cpi, "2020-01", active)
    data[currency.fx_series] = {date(2020, 1, 1): Decimal(1)}
    with pytest.raises(ValueError, match="建て替え日より前"):
        h9.month_signals(plan, data, cpi, "2020-01", active)


def test_rank_value_ties_and_eur_entry_keep_two_per_side(plan, carry, data, cpi):
    view = h9.carry_view(plan, carry)
    for month in ("2020-03", "2020-04"):
        active = [c for c in view.currencies if c.first_holding_month <= month]
        signals = h9.month_signals(plan, data, cpi, month, active)
        for signal in signals["currencies"].values():
            signal["V"] = 1.0
        weights = h9.value_weights(view, signals)
        assert weights == {"AUD": .25, "CAD": .25, "CHF": 0, "EUR": 0,
                           "GBP": 0, "JPY": -.25, "NZD": -.25}
        signals["currencies"]["JPY"]["V"] = 2
        weights = h9.value_weights(view, signals)
        assert weights["JPY"] == .25 and weights["AUD"] == .25
        assert sum(w > 0 for w in weights.values()) == 2
        assert sum(w < 0 for w in weights.values()) == 2


def test_callback_applies_to_main_and_all_sensitivities(plan, carry, data, cpi):
    wedges = wedge_file(carry, data, u_long=1, u_short=2)
    view = h9.carry_view(plan, carry)
    calls = []

    def weights_for(month, active):
        calls.append(month)
        signals = h9.month_signals(plan, data, cpi, month, active)
        return h9.value_weights(view, signals)

    report = h8.measure(view, data, wedges, weights_for=weights_for)
    assert len(calls) == len(h8.months(view.full)) * 5
    assert report["monthly"][0]["weights"] != h8.target_weights(view, data, view.currencies, "2020-01")
    for sensitivity in report["sensitivity"]:
        changed = h8.monthly_returns(view, data, wedges, weights_for=weights_for,
                                     **{sensitivity["component"] + "_multiplier": sensitivity["multiplier"]})
        for name in ("full", "post"):
            assert sensitivity[name] == h8.sharpe([r.net for r in h8.period_rows(changed, getattr(view, name))])


def test_combination_nets_positions_wedges_and_rebalance(plan, carry, data, cpi, monkeypatch):
    wedges = wedge_file(carry, data, u_long=1, u_short=3)
    # キャリーの下位 AUD/CAD と上位 NZD/JPY に対し、一部だけ反対向きにする。
    carry_weights = {"AUD": -.25, "CAD": -.25, "CHF": 0, "EUR": 0,
                     "GBP": 0, "JPY": .25, "NZD": .25}
    value = {"AUD": .25, "CAD": 0, "CHF": .25, "EUR": 0,
             "GBP": -.25, "JPY": -.25, "NZD": 0}
    monkeypatch.setattr(h8, "target_weights", lambda *args: carry_weights)
    monkeypatch.setattr(h9, "value_weights", lambda *args: value)
    carry_rows = h8.monthly_returns(carry, data, wedges)
    report = h9.measure(plan, carry, data, cpi, wedges, carry_rows)
    first, second = report["combination"]["monthly"][:2]
    expected = {"AUD": 0, "CAD": -.125, "CHF": .125, "EUR": 0,
                "GBP": -.125, "JPY": 0, "NZD": .125}
    assert first["weights"] == expected
    assert first["turnover"] == .5
    assert first["transaction_cost"] == pytest.approx(.5 * 3 / 10000)
    # CAD は直接建ての売り、CHF/NZD は逆建ての買い、GBP は逆建ての売り。
    expected_wedge = .125 * (3 + 3 + 1 + 3) / 100 * 31 / 365
    assert first["wedge_cost"] == pytest.approx(expected_wedge)
    expected_before = {
        c.code: expected[c.code] * (1 + first["fx_returns"][c.code]) / (1 + first["net"])
        for c in carry.currencies
    }
    assert second["before_weights"] == pytest.approx(expected_before)
    turnover = sum(abs(expected[code] - expected_before[code]) for code in expected)
    assert second["turnover"] == pytest.approx(turnover)
    assert turnover > 0
    assert second["transaction_cost"] == pytest.approx(turnover * .0003)
    for name, stats in report["combination"]["periods"].items():
        period = getattr(plan, name)
        values = [r["net"] for r in report["combination"]["monthly"]
                  if period.start <= r["month"] <= period.end]
        assert stats["sharpe"] == h8.sharpe(values)
        assert stats["annual_mean_net"] == pytest.approx(sum(values) / len(values) * 12)


def test_correlation_aligns_holding_months(plan, carry, data, cpi):
    wedges = wedge_file(carry, data)
    rows = h8.monthly_returns(carry, data, wedges)
    report = h9.measure(plan, carry, data, cpi, wedges, rows)
    value_rows = {r["month"]: r["net"] for r in report["monthly"]}
    synthetic = [replace(r, net=-value_rows[r.month]) for r in reversed(rows)]
    synthetic.append(replace(rows[0], month="1900-01", net=1000))
    report = h9.measure(plan, carry, data, cpi, wedges, synthetic)
    assert report["carry_correlation"] == pytest.approx({"full": -1, "post": -1, "pre": -1})
    assert h9.correlation([1, 1], [1, 2]) is None
    assert h9.correlation([], []) is None


def test_combination_boundary_uses_carry_universe_before_eur_entry(plan, carry, data, cpi):
    for currency in carry.currencies:
        data[currency.fx_series][date(2020, 1, 2)] = Decimal(1)
        if currency.code == "EUR":
            del data[currency.fx_series][date(2020, 1, 1)]
    wedges = wedge_file(carry, data)
    rows = h8.monthly_returns(carry, data, wedges)
    report = h9.measure(plan, carry, data, cpi, wedges, rows)
    assert report["monthly"][0]["start"] == "2020-01-01"
    assert report["monthly"][0]["weights"]["EUR"] == 0
    assert report["combination"]["monthly"][0]["start"] == "2020-01-02"
    assert report["combination"]["monthly"][0]["weights"]["EUR"] == -.125


@pytest.fixture
def fixed_files(tmp_path, plan, carry, data, cpi, monkeypatch):
    monkeypatch.setattr(h8, "git_state", lambda: {"git_sha": "test", "git_dirty": True})
    monkeypatch.setattr(h9, "git_state", lambda: {"git_sha": "test", "git_dirty": True})
    carry_path, carry_dir, _ = save_data(tmp_path, carry, data)
    manifest_hash = h8.digest((carry_dir / "manifest.json").read_bytes())
    wedges = wedge_file(carry, data, plan_hash=h8.digest(carry_path.read_bytes()),
                        manifest_hash=manifest_hash, u_long=1, u_short=2)
    wedge_path = tmp_path / "wedges.json"
    h8.write_wedges(wedge_path, wedges)
    carry_output = tmp_path / "carry_run"
    h8.measure_files(carry_path, carry_dir, wedge_path, carry_output)
    report_path = carry_output / "report.json"
    inputs = {"plan_sha256": h8.digest(carry_path.read_bytes()), "manifest_sha256": manifest_hash,
              "wedges_sha256": h8.digest(wedge_path.read_bytes()),
              "report_sha256": h8.digest(report_path.read_bytes())}
    plan = h9.Plan.model_validate(plan.model_dump() | {"carry": inputs})
    path, cpi_dir, _ = save_cpi(tmp_path, plan, cpi)
    return [path, cpi_dir, carry_path, carry_dir, wedge_path, report_path]


def test_measure_files_cli_roundtrip_and_overwrite(tmp_path, fixed_files):
    output = tmp_path / "result"
    args = ["measure"]
    for key, path in zip(("plan", "cpi-dir", "carry-plan", "carry-data-dir", "wedges", "carry-report"),
                         fixed_files, strict=True):
        args += [f"--{key}", str(path)]
    assert h9.main(args + ["--output-dir", str(output)]) == 0
    report = json.loads((output / "report.json").read_bytes())
    assert report["provenance"]["git_sha"] == "test"
    assert report["provenance"]["git_dirty"] is True
    assert len(report["provenance"]["sources_sha256"]) == 10
    assert len(report["provenance"]["carry"]["series_sha256"]) == 23
    assert report["provenance"]["carry"]["report_sha256"] == h8.digest(fixed_files[5].read_bytes())
    assert report["signals"]["2020-03"]["currencies"]["JPY"]["substituted"]
    assert "EUR" not in report["signals"]["2020-03"]["currencies"]
    assert "EUR" in report["signals"]["2020-04"]["currencies"]
    markdown = (output / "report.md").read_text()
    assert markdown.startswith("# H9 バリュー研究\n")
    assert "H8 との相関" in markdown and "2020-03、2020-04、2020-05、2020-06" in markdown
    before = (output / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        h9.main(args + ["--output-dir", str(output)])
    assert (output / "report.json").read_bytes() == before


def rewrite(path, edit):
    value = json.loads(path.read_bytes())
    edit(value)
    path.write_bytes(h8.json_bytes(value))


def resign_value_plan_and_manifest(plan_path, cpi_dir, **patch):
    rewrite(plan_path, lambda p: p["carry"].update(patch))
    rewrite(cpi_dir / "manifest.json", lambda m: m.update(plan_sha256=h8.digest(plan_path.read_bytes())))


@pytest.mark.parametrize("target", ["plan", "cpi_manifest", "cpi_file", "carry_plan",
                                     "carry_manifest", "carry_file", "wedges", "wedges_manifest",
                                     "carry_report"])
def test_measure_hash_mismatch_writes_nothing(tmp_path, fixed_files, target):
    p, cpi, cp, cd, w, r = fixed_files
    paths = {"plan": p, "cpi_manifest": cpi / "manifest.json", "cpi_file": cpi / "usd.csv",
             "carry_plan": cp, "carry_manifest": cd / "manifest.json",
             "carry_file": cd / "FXAAA.csv", "wedges": w,
             "wedges_manifest": h8.wedge_manifest_path(w), "carry_report": r}
    if target == "cpi_manifest":
        rewrite(paths[target], lambda m: m.update(plan_sha256="b" * 64))
    elif target == "wedges_manifest":
        rewrite(paths[target], lambda m: m.update(sha256="b" * 64))
    else:
        paths[target].write_bytes(paths[target].read_bytes() + b" ")
    output = tmp_path / "result"
    with pytest.raises(ValueError):
        h9.measure_files(*fixed_files, output)
    assert not output.exists()


@pytest.mark.parametrize("field,value", [("url", "https://example.invalid/wrong"), ("values", 0),
                                         ("first_month", "2000-01"), ("last_month", "2026-09")])
def test_manifest_metadata_mismatch_writes_nothing(tmp_path, fixed_files, field, value):
    rewrite(fixed_files[1] / "manifest.json", lambda m: m["sources"]["usd"].update({field: value}))
    output = tmp_path / "result"
    with pytest.raises(ValueError, match="manifest の内容"):
        h9.measure_files(*fixed_files, output)
    assert not output.exists()


@pytest.mark.parametrize("field", ["plan_sha256", "manifest_sha256", "wedges_sha256"])
def test_report_provenance_mismatch_writes_nothing(tmp_path, fixed_files, field):
    report_path = fixed_files[5]
    rewrite(report_path, lambda r: r["provenance"].update({field: "b" * 64}))
    resign_value_plan_and_manifest(*fixed_files[:2], report_sha256=h8.digest(report_path.read_bytes()))
    output = tmp_path / "result"
    with pytest.raises(ValueError, match="provenance"):
        h9.measure_files(*fixed_files, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["net", "order", "month", "missing"])
def test_h8_recalculation_must_match_exactly(tmp_path, fixed_files, kind):
    def change(report):
        if kind == "net":
            report["monthly"][0]["net"] = math.nextafter(report["monthly"][0]["net"], math.inf)
        elif kind == "order":
            report["monthly"].reverse()
        elif kind == "month":
            report["monthly"][0]["month"] = "1900-01"
        else:
            report["monthly"].pop()
    rewrite(fixed_files[5], change)
    resign_value_plan_and_manifest(*fixed_files[:2], report_sha256=h8.digest(fixed_files[5].read_bytes()))
    output = tmp_path / "result"
    with pytest.raises(ValueError, match="再計算"):
        h9.measure_files(*fixed_files, output)
    assert not output.exists()


@pytest.mark.parametrize("command", ["fetch", "measure"])
def test_cli_help(command, capsys):
    with pytest.raises(SystemExit) as error:
        h9.main([command, "--help"])
    assert error.value.code == 0
    assert "--plan" in capsys.readouterr().out
