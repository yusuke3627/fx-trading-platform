"""H8 の順位・コスト・統計と固定ファイルを架空値で確認する。"""
from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.support import carry_data, carry_plan
from trading.backtest import carry_study as h8
from trading.domain.swap import SwapSnapshot


@pytest.fixture
def plan():
    return carry_plan()


@pytest.fixture
def data(plan):
    return carry_data(plan)


def snapshot(currency, **overrides):
    values = {"snapshot_id": uuid4(), "symbol": currency.oanda_symbol, "swap_mode": 1,
              "swap_long": Decimal(-2), "swap_short": Decimal(1), "swap_rollover3days": 3,
              "payload_hash": "a" * 64, "known_at": datetime(2020, 7, 1, tzinfo=UTC),
              "retrieved_at": datetime(2020, 7, 1, tzinfo=UTC)}
    return SwapSnapshot(**(values | overrides))


def wedge_file(plan, data, *, plan_hash="a" * 64, manifest_hash="b" * 64, u_long=0, u_short=0):
    pairs = [h8.calculate_wedge(plan, data, c, snapshot(c), Decimal("0.001"))
             .model_copy(update={"u_long": Decimal(u_long), "u_short": Decimal(u_short)})
             for c in plan.currencies]
    return h8.WedgeFile(plan_sha256=plan_hash, manifest_sha256=manifest_hash,
                        as_of=datetime(2020, 7, 1, tzinfo=UTC), pairs=tuple(pairs))


def csv_bytes(series, values):
    return (f"observation_date,{series}\r\n"
            + "".join(f"{day},{value}\r\n" for day, value in sorted(values.items()))).encode()


def save_data(tmp_path, plan, data):
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    bodies = {series: csv_bytes(series, values) for series, values in data.items()}

    def retrieve(url):
        return bodies[parse_qs(urlparse(url).query)["id"][0]]

    directory = tmp_path / "data"
    h8.fetch(path, directory, retrieve=retrieve)
    return path, directory, bodies


def test_plan_is_frozen_rejects_unknown_and_invalid_boundary(plan):
    with pytest.raises(ValidationError):
        plan.bootstrap_seed = 99
    with pytest.raises(ValidationError):
        plan.currencies[0].code = "XYZ"
    for patch in ({"extra": 1}, {"k_divisor": 1}, {"day_count": 0},
                  {"gross_per_side": float("nan")}, {"bootstrap_seed": True},
                  {"post": {"start": "2020-03", "end": "2020-06"}},
                  {"currencies": [plan.currencies[0]] * 7}):
        with pytest.raises(ValidationError):
            h8.Plan.model_validate(plan.model_dump() | patch)
    assert plan.signal_lag_months == 3
    assert plan.transaction_cost_bp == 3
    assert plan.sensitivity.wedge_multipliers == (0, 2)


def test_fetch_preserves_bytes_manifest_and_refuses_overwrite(tmp_path, plan, data):
    data[plan.currencies[0].fx_series][date(2019, 12, 31)] = "."
    path, directory, bodies = save_data(tmp_path, plan, data)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert len(bodies) == 23
    assert manifest["plan_sha256"] == h8.digest(path.read_bytes())
    assert datetime.fromisoformat(manifest["retrieved_at"]).utcoffset() == timedelta(0)
    for series, body in bodies.items():
        assert (directory / f"{series}.csv").read_bytes() == body
        assert manifest["series"][series]["sha256"] == h8.digest(body)
        assert manifest["series"][series]["rows"] == len(data[series])
    entry = manifest["series"][plan.currencies[0].fx_series]
    assert entry["first_date"] == "2020-01-01"
    assert entry["last_date"] == "2020-07-01"
    with pytest.raises(FileExistsError):
        h8.fetch(path, directory, retrieve=lambda _: pytest.fail("取得してはいけない"))


@pytest.mark.parametrize("body", [
    "date,TEST\n2020-01-01,1\n", "observation_date,OTHER\n",
    "observation_date,TEST\n20200101,1\n", "observation_date,TEST\n2020-02-30,1\n",
    "observation_date,TEST\n2020-01-01,nope\n", "observation_date,TEST\n2020-01-01,NaN\n",
    "observation_date,TEST\n2020-01-01,Infinity\n", "observation_date,TEST\n2020-01-01,1,2\n",
    "observation_date,TEST\n2020-01-01,1\n2020-01-01,2\n",
])
def test_fetch_csv_boundary_rejects_malformed(body):
    with pytest.raises(ValueError):
        h8.parse_csv(body.encode(), "TEST")


def test_fetch_bad_csv_has_no_success_manifest(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    with pytest.raises(ValueError):
        h8.fetch(path, tmp_path / "bad", retrieve=lambda _: b"invalid")
    assert not (tmp_path / "bad" / "manifest.json").exists()


def test_prices_skip_missing_and_require_positive():
    values, count = h8.parse_csv(b"observation_date,TEST\n2020-01-01,\n2020-01-02,.\n"
                                 b"2020-01-03,2\n", "TEST", positive=True)
    assert values == {date(2020, 1, 3): Decimal(2)}
    assert count == 3
    for value in ("0", "-1"):
        with pytest.raises(ValueError):
            h8.parse_csv(f"observation_date,TEST\n2020-01-01,{value}\n".encode(),
                         "TEST", positive=True)


def test_rate_3m_then_overnight_no_forward_fill(plan, data):
    c = plan.currencies[0]
    day = date(2020, 1, 1)
    data[c.rate_overnight_series][day] = Decimal(9)
    assert h8.rate(data, c, "2020-01", c.code) == 0
    del data[c.rate_3m_series][day]
    assert h8.rate(data, c, "2020-01", c.code) == 9
    del data[c.rate_overnight_series][day]
    data[c.rate_overnight_series][date(2020, 1, 2)] = Decimal(8)
    with pytest.raises(ValueError, match=f"AAA 2020-01.*{c.rate_3m_series}.*{c.rate_overnight_series}"):
        h8.rate(data, c, "2020-01", c.code)


def test_common_boundary_and_new_currency_gap(plan, data):
    c = plan.currencies[-1].model_copy(update={"first_holding_month": "2020-02"})
    plan = plan.model_copy(update={"currencies": (*plan.currencies[:-1], c)})
    for currency in plan.currencies:
        data[currency.fx_series][date(2020, 2, 2)] = Decimal(1)
    del data[c.fx_series][date(2020, 2, 1)]
    assert h8.common_date(data, plan.currencies[:-1], "2020-02") == date(2020, 2, 1)
    assert h8.common_date(data, plan.currencies, "2020-02") == date(2020, 2, 2)
    # 1月の終わりは6通貨で決まる。2月の開始との隙間を無視して計算しない。
    with pytest.raises(ValueError, match="前月の終わりと当月の開始日"):
        h8.monthly_returns(plan, data, wedge_file(plan, data))
    for currency in plan.currencies[:-1]:
        del data[currency.fx_series][date(2020, 2, 1)]
    rows = h8.monthly_returns(plan, data, wedge_file(plan, data))
    assert rows[0].end == rows[1].start == date(2020, 2, 2)
    assert rows[0].weights[c.code] == 0
    assert sum(w > 0 for w in rows[0].weights.values()) == 2
    with pytest.raises(ValueError, match="為替がある日"):
        h8.common_date(data, plan.currencies, "2019-12")


def test_rank_uses_three_month_lag_and_alphabetical_ties(plan, data):
    for c in plan.currencies:
        data[c.rate_3m_series][date(2019, 10, 1)] = Decimal(1)
        data[c.rate_3m_series][date(2019, 12, 1)] = Decimal(ord(c.code[0]))
    weights = h8.target_weights(plan, data, plan.currencies, "2020-01")
    assert weights == {"AAA": .25, "BBB": .25, "CCC": 0, "DDD": 0, "EEE": 0, "FFF": -.25, "GGG": -.25}
    for c in plan.currencies:
        data[c.rate_3m_series][date(2019, 12, 1)] = -data[c.rate_3m_series][date(2019, 12, 1)]
    assert h8.target_weights(plan, data, plan.currencies, "2020-01") == weights
    six = h8.target_weights(plan, data, plan.currencies[:-1], "2020-01")
    assert sum(w == .25 for w in six.values()) == sum(w == -.25 for w in six.values()) == 2
    assert six["GGG"] == 0


def test_simple_fx_interest_days_rebalance_and_exit_cost(plan, data):
    c = plan.currencies[0]
    data[c.fx_series][date(2020, 1, 1)] = Decimal(2)
    data[c.fx_series][date(2020, 2, 1)] = Decimal(4)
    # 2月は10月ではなく11月の金利で決まり、AAA が持ち高から外れる。
    data[c.rate_3m_series][date(2019, 11, 1)] = Decimal(3)
    rows = h8.monthly_returns(plan, data, wedge_file(plan, data))
    jan, feb = rows[:2]
    assert h8.usd_price(data, c, date(2020, 1, 1)) == Decimal("0.5")
    assert jan.fx_returns["AAA"] == -.5
    assert jan.fx == pytest.approx(-.25 * -.5 - .25 * .002 + .25 * .006 + .25 * .007)
    assert jan.interest == pytest.approx((-.25 * (0 - 2) - .25 * (1 - 2)
                                          + .25 * (5 - 2) + .25 * (6 - 2)) / 100 * 31 / 365)
    assert all(w == 0 for w in jan.before_weights.values())
    assert jan.turnover == 1
    assert jan.transaction_cost == .0003
    assert feb.weights["AAA"] == 0
    assert feb.before_weights["AAA"] == pytest.approx(-.25 * .5 / (1 + jan.net))
    assert feb.before_weights["GGG"] == pytest.approx(.25 * 1.007 / (1 + jan.net))
    assert feb.turnover == pytest.approx(sum(abs(feb.weights[k] - feb.before_weights[k])
                                           for k in feb.weights))
    assert feb.transaction_cost == pytest.approx(feb.turnover * .0003)
    assert rows[-1].transaction_cost == pytest.approx(rows[-1].turnover * .0003)


def test_same_currency_requires_rebalance_and_sensitivity_recomputes_nav(plan, data):
    wedges = wedge_file(plan, data, u_long=1, u_short=3)
    base = h8.monthly_returns(plan, data, wedges)
    free = h8.monthly_returns(plan, data, wedges, transaction_cost_multiplier=0)
    assert base[0].weights == base[1].weights
    assert base[1].turnover > 0
    assert free[1].before_weights != base[1].before_weights
    assert base[0].net == pytest.approx(base[0].gross - base[0].transaction_cost - base[0].wedge_cost)


@pytest.mark.parametrize(("code", "expected_u"), [("AAA", 2), ("BBB", 7), ("FFF", 2), ("GGG", 7)])
def test_wedge_direction_for_both_pair_orientations(plan, data, code, expected_u):
    wedges = wedge_file(plan, data)
    wedges = wedges.model_copy(update={"pairs": tuple(
        p.model_copy(update={"u_long": Decimal(2), "u_short": Decimal(7)}) if p.code == code else p
        for p in wedges.pairs
    )})
    rows = h8.monthly_returns(plan, data, wedges)
    # 1通貨だけに上乗せを付け、売買方向の取り違えが合計で相殺されるのを避ける。
    assert rows[0].wedge_cost == pytest.approx(.25 * expected_u / 100 * 31 / 365)


def test_weekly_multipliers_all_absent_fractional_and_partial(plan, data):
    s = snapshot(plan.currencies[0])
    for triple in range(7):
        assert h8.weekly_multiplier(s.model_copy(update={"swap_rollover3days": triple})) == 7
    values = dict(zip(("swap_sunday", "swap_monday", "swap_tuesday", "swap_wednesday",
                       "swap_thursday", "swap_friday", "swap_saturday"),
                      map(Decimal, ("0", ".5", "1", "3", "1.5", "2.5", "0")), strict=True))
    fractional = s.model_copy(update=values)
    assert h8.weekly_multiplier(fractional) == Decimal("8.5")
    c = plan.currencies[0]
    data[c.fx_series][date(2020, 7, 1)] = Decimal(100)
    wedge = h8.calculate_wedge(plan, data, c, fractional, Decimal(".01"))
    assert float(wedge.observed_long) == pytest.approx(-2 * .01 * (8.5 * 365 / 7) / 100 * 100)
    with pytest.raises(ValueError, match="一部だけ"):
        h8.weekly_multiplier(s.model_copy(update={"swap_monday": Decimal(1)}))


def test_wedge_annualization_price_cutoff_latest_common_rate_and_clamp(plan, data):
    c = plan.currencies[0]
    data[c.fx_series][date(2020, 7, 1)] = Decimal(100)
    data[c.fx_series][date(2020, 7, 2)] = Decimal(200)
    data[c.rate_3m_series][date(2020, 7, 1)] = Decimal(20)  # USD は6月まで。
    data[c.rate_3m_series].pop(date(2020, 6, 1))
    data[c.rate_overnight_series][date(2020, 6, 1)] = Decimal(1)
    s = snapshot(c, swap_long=Decimal(1), swap_short=Decimal(-10))
    w = h8.calculate_wedge(plan, data, c, s, Decimal(".01"))
    assert w.price == 100
    assert w.price_date == date(2020, 7, 1)
    assert w.theoretical_month == "2020-06"
    assert w.theoretical_long == 1
    assert w.observed_long == Decimal("3.65")
    assert w.observed_short == Decimal("-36.5")
    assert w.u_long == 0
    assert w.u_short == Decimal("35.5")
    b = plan.currencies[1]
    wb = h8.calculate_wedge(plan, data, b, snapshot(b), Decimal(".01"))
    assert wb.theoretical_long == -1
    assert wb.theoretical_short == 1
    assert wb.price == h8.usd_price(data, b, date(2020, 7, 1))
    for mode in (0, 2):
        with pytest.raises(ValueError, match="swap_mode"):
            h8.calculate_wedge(plan, data, c, s.model_copy(update={"swap_mode": mode}), Decimal(1))
    with pytest.raises(ValueError, match="point"):
        h8.calculate_wedge(plan, data, c, s, Decimal(0))


def test_sharpe_percentile_bootstrap_and_circular_blocks(plan):
    assert h8.sharpe([1, 2, 3]) == pytest.approx(2 * math.sqrt(12))
    assert h8.sharpe([1, 1]) is None
    assert h8.percentile([0, 10], .05) == pytest.approx(.5)

    class Starts:
        def __init__(self):
            self.positions = iter([3, 1])

        def randrange(self, size):
            assert size == 4
            return next(self.positions)

    assert h8.circular_sample([0, 1, 2, 3], 3, Starts()) == [3, 0, 1, 1]
    a = h8.bootstrap([-.2, .1, .3, -.1], plan, random.Random(17))
    assert a == h8.bootstrap([-.2, .1, .3, -.1], plan, random.Random(17))
    assert a["samples"] == 100
    undefined = h8.bootstrap([1, 1], plan, random.Random(17))
    assert undefined["undefined_samples"] == 100
    assert undefined["lower"] is None


@pytest.mark.parametrize(("full_patch", "post_patch", "verdict"), [
    ({}, {}, "支持"), ({"upper": .2}, {}, "棄却"), ({}, {"upper": .2}, "棄却"),
    ({"lower": 0}, {}, "判定不能"), ({}, {"sharpe": 0}, "判定不能"),
    ({"undefined_samples": 1}, {}, "支持"),
    ({"undefined_samples": 2}, {}, "判定不能"),
    ({}, {"upper": .2, "undefined_samples": 2}, "判定不能"),
    ({"sharpe": None}, {}, "判定不能"),
    ({"lower": 0, "upper": .30}, {"upper": .30}, "判定不能"),
])
def test_decision_and_rejection_priority(plan, full_patch, post_patch, verdict):
    stats = {"sharpe": .6, "lower": .1, "upper": .8, "undefined_samples": 0, "samples": 100}
    decision = h8.decide(stats | full_patch, stats | post_patch, plan)
    assert decision["verdict"] == verdict
    if max(full_patch.get("undefined_samples", 0), post_patch.get("undefined_samples", 0)) > 1:
        assert "2/100 回" in decision["reason"]
        assert "許容割合 1.0% を超える" in decision["reason"]


def test_secondary_compounding_drawdown_skew_composition_and_correlation(plan, data):
    rows = h8.monthly_returns(plan, data, wedge_file(plan, data))
    returns = [.1, -.2, -.1, .4, 0, -.1]
    rows = [replace(r, net=x, basket=x) for r, x in zip(rows, returns, strict=True)]
    s = h8.secondary(rows, plan)
    assert s["max_drawdown"]["return"] == pytest.approx(-.28)
    assert s["max_drawdown"]["peak"] == "2020-02-01"
    assert s["max_drawdown"]["trough"] == "2020-04-01"
    assert s["worst_months"]["1"]["return"] == pytest.approx(-.2)
    assert s["worst_months"]["3"]["return"] == pytest.approx(1.1 * .8 * .9 - 1)
    assert s["worst_months"]["3"]["first_month"] == "2020-01"
    assert s["worst_months"]["12"] is None
    assert s["basket_correlation"] == pytest.approx(1)
    assert s["composition"]["GGG"]["long_fraction"] == 1
    assert s["composition"]["AAA"]["short_fraction"] == 1
    assert s["annual_mean_fx"] == pytest.approx(sum(r.fx for r in rows) / 6 * 12)
    assert s["annual_mean_interest"] == pytest.approx(sum(r.interest for r in rows) / 6 * 12)


def test_measure_rng_order_and_all_secondary_reports(plan, data):
    wedges = wedge_file(plan, data)
    report = h8.measure(plan, data, wedges)
    rows = h8.monthly_returns(plan, data, wedges)
    rng = random.Random(plan.bootstrap_seed)
    for name in ("full", "post", "pre"):
        expected = h8.bootstrap([r.net for r in h8.period_rows(rows, getattr(plan, name))], plan, rng)
        assert all(report["periods"][name][k] == v for k, v in expected.items())
    assert len(report["sensitivity"]) == 4
    assert len(report["monthly"]) == 6
    free = next(s for s in report["sensitivity"] if s["component"] == "transaction_cost"
                and s["multiplier"] == 0)
    assert free["full"] == h8.sharpe([r.net for r in h8.monthly_returns(
        plan, data, wedges, transaction_cost_multiplier=0)])


def test_worst_twelve_months_uses_compound_return_and_dates(plan, data):
    template = h8.monthly_returns(plan, data, wedge_file(plan, data))[0]
    rows = [replace(template, month=h8.shift_month("2020-01", i), net=(-.1 if i == 12 else .01))
            for i in range(13)]
    worst = h8.secondary(rows, plan)["worst_months"]["12"]
    assert worst["return"] == pytest.approx(1.01 ** 11 * .9 - 1)
    assert worst["first_month"] == "2020-02"
    assert worst["last_month"] == "2021-01"


@pytest.fixture
def fixed_files(tmp_path, plan, data):
    path, directory, _ = save_data(tmp_path, plan, data)
    wedges = wedge_file(plan, data, plan_hash=h8.digest(path.read_bytes()),
                        manifest_hash=h8.digest((directory / "manifest.json").read_bytes()))
    wedge_path = tmp_path / "wedges.json"
    h8.write_wedges(wedge_path, wedges)
    return path, directory, wedge_path


def test_measure_files_cli_roundtrip_and_overwrite(tmp_path, fixed_files, monkeypatch):
    path, directory, wedge_path = fixed_files
    monkeypatch.setenv("TRADING_DB_DSN", "must-not-connect")
    output = tmp_path / "report"
    assert h8.main(["measure", "--plan", str(path), "--data-dir", str(directory),
                    "--wedges", str(wedge_path), "--output-dir", str(output)]) == 0
    report = json.loads((output / "report.json").read_bytes())
    assert report["provenance"]["wedges_sha256"] == h8.digest(wedge_path.read_bytes())
    assert len(report["provenance"]["series_sha256"]) == 23
    assert "git_commit" in report["provenance"]
    markdown = (output / "report.md").read_text()
    assert all(text in markdown for text in ("副統計", "感応度", "来歴", "発表前"))
    with pytest.raises(FileExistsError):
        h8.measure_files(path, directory, wedge_path, output)
    wedges = h8.WedgeFile.model_validate_json(wedge_path.read_bytes())
    with pytest.raises(FileExistsError):
        h8.write_wedges(wedge_path, wedges)


@pytest.mark.parametrize("target", ["plan", "manifest", "data", "wedges", "sidecar"])
def test_measure_hash_mismatch_writes_nothing(tmp_path, fixed_files, target):
    path, directory, wedge_path = fixed_files
    files = {"plan": path, "manifest": directory / "manifest.json",
             "data": next(directory.glob("*.csv")), "wedges": wedge_path,
             "sidecar": h8.wedge_manifest_path(wedge_path)}
    file = files[target]
    if target == "sidecar":
        file.write_text(json.dumps({"sha256": "0" * 64}))
    else:
        with file.open("ab") as output:
            output.write(b"\n")
    output_dir = tmp_path / "report"
    with pytest.raises(ValueError, match="ハッシュ|sha256"):
        h8.measure_files(path, directory, wedge_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize("command", ["fetch", "wedges", "measure"])
def test_cli_help(command, capsys):
    with pytest.raises(SystemExit) as exc:
        h8.main([command, "--help"])
    assert exc.value.code == 0
    assert "--plan" in capsys.readouterr().out
