"""H9 のバリュー研究。fetch で物価を固定し、measure で H8 と照合して測る。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from trading.backtest import carry_study as h8
from trading.backtest.run import git_state

CpiData = dict[str, dict[str, Decimal]]


class Source(h8.Record):
    name: h8.Series
    url: Annotated[str, Field(pattern=r"^https://[^\s]+$")]


class FredSource(Source):
    format: Literal["fred"]
    series: h8.Series


class OecdSource(Source):
    format: Literal["oecd"]
    ref_area: h8.Series
    freq: Literal["M", "Q"]


class JapanSource(Source):
    format: Literal["stat_jp"]


class EurostatSource(Source):
    format: Literal["eurostat"]
    geo: h8.Series
    unit: h8.Series
    coicop18: h8.Series


class OnsSource(Source):
    format: Literal["ons"]
    cdid: h8.Series


class SnbSource(Source):
    format: Literal["snb"]
    cube: h8.Series
    d0: h8.Series


CpiSource = Annotated[
    FredSource | OecdSource | JapanSource | EurostatSource | OnsSource | SnbSource,
    Field(discriminator="format"),
]


class CarryInputs(h8.Record):
    plan_sha256: h8.Digest
    manifest_sha256: h8.Digest
    wedges_sha256: h8.Digest
    report_sha256: h8.Digest


class RevisionWindow(h8.Period):
    substitute: h8.Month

    @model_validator(mode="after")
    def before_window(self) -> RevisionWindow:
        date.fromisoformat(self.substitute + "-01")
        if self.substitute >= self.start:
            raise ValueError("置き換え先は改定期間より前の月にしてください")
        return self


class Plan(h8.Record):
    study_version: Annotated[str, Field(min_length=1)]
    bootstrap_seed: int = Field(strict=True)
    carry: CarryInputs
    full: h8.Period
    post: h8.Period
    pre: h8.Period
    first_holding_months: dict[str, h8.Month]
    cpi_lag_months: h8.PositiveInt
    cpi_max_lag_months: h8.PositiveInt
    change_months: h8.PositiveInt
    fx_average_first_offset: h8.PositiveInt
    fx_average_last_offset: h8.PositiveInt
    combination_weight: Literal[0.5] = 0.5
    sources: tuple[CpiSource, ...]
    price_sources: dict[str, tuple[h8.Series, ...]]
    jpy_link_month: h8.Month
    gbp_cpi_first_month: h8.Month
    jpy_revision_windows: tuple[RevisionWindow, ...]

    @model_validator(mode="after")
    def consistent(self) -> Plan:
        if not (self.pre.start == self.full.start and self.post.end == self.full.end
                and h8.shift_month(self.pre.end, 1) == self.post.start
                and self.full.start <= self.pre.end < self.full.end):
            raise ValueError("pre と post は full を順に分割してください")
        if (self.cpi_lag_months > self.cpi_max_lag_months
                or self.fx_average_first_offset < self.fx_average_last_offset):
            raise ValueError("物価のラグまたは為替の平均期間が逆順です")
        for month in (*self.first_holding_months.values(), self.jpy_link_month,
                      self.gbp_cpi_first_month):
            date.fromisoformat(month + "-01")
        if ("USD" in self.first_holding_months
                or set(self.price_sources) != {"USD", *self.first_holding_months}):
            raise ValueError("物価の割り当ては USD と対象通貨にそろえてください")
        sources = {s.name: s for s in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("取得元の名前が重複しています")
        used = set()
        for code, names in self.price_sources.items():
            if len(names) != (2 if code in ("JPY", "GBP") else 1):
                raise ValueError(f"{code}: 物価の系列数が不正です")
            if len(set(names)) != len(names) or not set(names) <= sources.keys():
                raise ValueError(f"{code}: 物価の取得元が不正です")
            used.update(names)
        if used != sources.keys():
            raise ValueError("割り当てのない物価の取得元があります")
        windows = sorted(self.jpy_revision_windows, key=lambda w: w.start)
        if any(a.end >= b.start for a, b in pairwise(windows)):
            raise ValueError("日本の基準改定の期間が重複しています")
        return self


def read_plan(path: Path) -> tuple[Plan, str]:
    content = path.read_bytes()
    return Plan.model_validate_json(content), h8.digest(content)


def checked_month(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError(f"月の形式が不正です: {value}")
    date.fromisoformat(value + "-01")
    return value


def parse_csv(content: bytes, source: CpiSource) -> dict[str, Decimal]:
    if isinstance(source, FredSource):
        values, _ = h8.parse_csv(content, source.series, positive=True)
        # 空欄の行も含めて、FRED の月次日付を検証する。
        for row in list(csv.reader(io.StringIO(content.decode("utf-8"))))[1:]:
            if date.fromisoformat(row[0]).day != 1:
                raise ValueError(f"{source.name}: 月の1日付けの値が必要です")
        return {day.isoformat()[:7]: value for day, value in values.items()}

    encoding = "cp932" if isinstance(source, JapanSource) else "utf-8-sig"
    delimiter = ";" if isinstance(source, SnbSource) else ","
    rows = list(csv.reader(io.StringIO(content.decode(encoding)), delimiter=delimiter, strict=True))
    observations: list[tuple[str, str]] = []
    if isinstance(source, (OecdSource, EurostatSource)):
        identifiers = ({"REF_AREA": source.ref_area, "FREQ": source.freq, "METHODOLOGY": "N",
                        "MEASURE": "CPI", "UNIT_MEASURE": "IX", "ADJUSTMENT": "N",
                        "EXPENDITURE": "_T"} if isinstance(source, OecdSource) else
                       {"freq": "M", "unit": source.unit, "coicop18": source.coicop18,
                        "geo": source.geo})
        if (not rows or len(set(rows[0])) != len(rows[0])
                or not {*identifiers, "TIME_PERIOD", "OBS_VALUE"} <= set(rows[0])):
            raise ValueError(f"{source.name}: CSV のヘッダーが不正です")
        for row in rows[1:]:
            if len(row) != len(rows[0]):
                raise ValueError(f"{source.name}: CSV の列数が不正です")
            fields = dict(zip(rows[0], row, strict=True))
            if any(fields[key] != value for key, value in identifiers.items()):
                raise ValueError(f"{source.name}: 系列の識別子が一致しません")
            period = fields["TIME_PERIOD"]
            if isinstance(source, OecdSource) and source.freq == "Q":
                if not re.fullmatch(r"[0-9]{4}-Q[1-4]", period):
                    raise ValueError(f"{source.name}: 四半期の形式が不正です: {period}")
                period = f"{period[:4]}-{int(period[-1]) * 3:02d}"
            observations.append((period, fields["OBS_VALUE"]))
    elif isinstance(source, JapanSource):
        if len(rows) < 2 or rows[0][1:2] != ["総合"] or rows[1][1:2] != ["All items"]:
            raise ValueError(f"{source.name}: 総合のヘッダーがありません")
        for row in rows[2:]:
            if not row:
                continue
            if re.fullmatch(r"[0-9]{6}", row[0]):
                if len(row) < 2:
                    raise ValueError(f"{source.name}: 値の列がありません")
                observations.append((row[0][:4] + "-" + row[0][4:], row[1]))
            elif re.match(r"[0-9]{4}", row[0]):
                raise ValueError(f"{source.name}: 期間の形式が不正です: {row[0]}")
    elif isinstance(source, OnsSource):
        if [row for row in rows if row and row[0] == "CDID"] != [["CDID", source.cdid]]:
            raise ValueError(f"{source.name}: CDID が一致しません")
        month_names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        for row in rows:
            if not row or not re.match(r"[0-9]{4}", row[0]):
                continue
            if re.fullmatch(r"[0-9]{4}( Q[1-4])?", row[0]):
                continue
            if (len(row) != 2 or not re.fullmatch(r"[0-9]{4} [A-Z]{3}", row[0])
                    or row[0][5:] not in month_names):
                raise ValueError(f"{source.name}: 月次の行が不正です: {row}")
            observations.append((f"{row[0][:4]}-{month_names.index(row[0][5:]) + 1:02d}", row[1]))
    else:
        if [row for row in rows if row and row[0] == "CubeId"] != [["CubeId", source.cube]]:
            raise ValueError(f"{source.name}: CubeId が一致しません")
        header = ["Date", "D0", "Value"]
        if rows.count(header) != 1:
            raise ValueError(f"{source.name}: Date / D0 / Value の見出しがありません")
        for row in rows[rows.index(header) + 1:]:
            if len(row) != 3:
                raise ValueError(f"{source.name}: CSV の列数が不正です")
            if row[1] == source.d0:
                observations.append((row[0], row[2]))
        if not observations:
            raise ValueError(f"{source.name}: D0 が一致する行がありません")

    result = {}
    seen = set()
    for period, text in observations:
        period = checked_month(period)
        if period in seen:
            raise ValueError(f"{source.name}: 期間が重複しています: {period}")
        seen.add(period)
        if text == "":
            continue
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{source.name}: 数値が不正です: {text}") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{source.name}: 有限の正の数値が必要です")
        result[period] = value
    return result


class SourceManifest(h8.Record):
    url: str
    sha256: h8.Digest
    values: int = Field(ge=0)
    first_month: h8.Month | None
    last_month: h8.Month | None


class Manifest(h8.Record):
    plan_sha256: h8.Digest
    retrieved_at: AwareDatetime
    sources: dict[str, SourceManifest]


def fetch(
    plan_path: Path, output: Path, *, retrieve: Callable[[str], bytes] = h8.download,
) -> None:
    plan, plan_hash = read_plan(plan_path)
    output.mkdir(parents=True, exist_ok=False)
    metadata = {}
    for source in plan.sources:
        content = retrieve(source.url)
        values = parse_csv(content, source)
        with (output / f"{source.name}.csv").open("xb") as target:
            target.write(content)
        metadata[source.name] = SourceManifest(
            url=source.url, sha256=h8.digest(content), values=len(values),
            first_month=min(values, default=None), last_month=max(values, default=None),
        )
    manifest = Manifest(plan_sha256=plan_hash, retrieved_at=datetime.now(UTC), sources=metadata)
    h8.write_json(output / "manifest.json", manifest.model_dump(mode="json"))


def load_data(plan: Plan, plan_hash: str, directory: Path) -> tuple[CpiData, str, dict[str, str]]:
    content = (directory / "manifest.json").read_bytes()
    manifest = Manifest.model_validate_json(content)
    if manifest.plan_sha256 != plan_hash or set(manifest.sources) != {s.name for s in plan.sources}:
        raise ValueError("物価 manifest の Plan ハッシュまたは取得元一覧が一致しません")
    data, hashes = {}, {}
    for source in plan.sources:
        entry = manifest.sources[source.name]
        raw = (directory / f"{source.name}.csv").read_bytes()
        if h8.digest(raw) != entry.sha256:
            raise ValueError(f"{source.name}: 物価データの sha256 が一致しません")
        values = parse_csv(raw, source)
        if (entry.url != source.url
                or (len(values), min(values, default=None), max(values, default=None))
                != (entry.values, entry.first_month, entry.last_month)):
            raise ValueError(f"{source.name}: manifest の内容が CSV と一致しません")
        data[source.name], hashes[source.name] = values, entry.sha256
    return data, h8.digest(content), hashes


def carry_view(plan: Plan, carry_plan: h8.Plan, *, combo: bool = False) -> h8.Plan:
    if set(plan.first_holding_months) != {c.code for c in carry_plan.currencies}:
        raise ValueError("最初の保有月の通貨一覧が H8 の Plan と一致しません")
    values = carry_plan.model_dump()
    values.update({key: getattr(plan, key) for key in ("study_version", "bootstrap_seed",
                                                      "full", "post", "pre")})
    if not combo:
        values["currencies"] = [c.model_dump() | {
            "first_holding_month": plan.first_holding_months[c.code],
        } for c in carry_plan.currencies]
    return h8.Plan.model_validate(values)


def price_change(plan: Plan, data: CpiData, code: str, month: str) -> dict:
    names = plan.price_sources[code]
    if code == "JPY":
        old, new = (data[name] for name in names)
        link = plan.jpy_link_month
        if link not in old or link not in new:
            raise ValueError(f"JPY {link}: 接続月の物価がありません")
        ratio = new[link] / old[link]
        values = {m: v * ratio for m, v in old.items() if m < link}
        values.update({m: v for m, v in new.items() if m >= link})
    elif code == "GBP":
        values = {m: v for index, name in enumerate(names) for m, v in data[name].items()
                  if (h8.shift_month(m, -plan.change_months) >= plan.gbp_cpi_first_month)
                  == bool(index)}
    else:
        values = data[names[0]]
    cutoff = h8.shift_month(month, -plan.cpi_lag_months)
    available = [m for m in values if m <= cutoff]
    if not available:
        raise ValueError(f"{code} {month}: 公表済みとみなせる物価がありません")
    original = e = max(available)
    substituted = False
    if code == "JPY":
        for window in plan.jpy_revision_windows:
            if window.start <= e <= window.end:
                e, substituted = window.substitute, True
                break
    if not substituted and e < h8.shift_month(month, -plan.cpi_max_lag_months):
        raise ValueError(f"{code} {month}: 物価のラグが上限を超えています")
    previous = h8.shift_month(e, -plan.change_months)
    used = names
    if code == "GBP":
        name = names[int(previous >= plan.gbp_cpi_first_month)]
        values, used = data[name], (name,)
    elif code == "JPY" and previous >= plan.jpy_link_month:
        used = (names[1],)
    if e not in values or previous not in values:
        raise ValueError(f"{code} {month}: {e} または {previous} の物価がありません")
    return {"change": math.log(values[e]) - math.log(values[previous]), "e": e,
            "sources": list(used), "substituted": substituted,
            "e_before_substitution": original}


def month_signals(
    plan: Plan, data: h8.Data, cpi: CpiData, month: str, active: Sequence[h8.Currency],
) -> dict:
    start = h8.common_date(data, active, month)
    usd = price_change(plan, cpi, "USD", month)
    signals = {}
    for currency in active:
        days = [d for d in data[currency.fx_series] if d < start]
        if not days:
            raise ValueError(f"{currency.code} {month}: 建て替え日より前の為替がありません")
        before = max(days)
        history = []
        for historical_month in h8.months(h8.Period(
            start=h8.shift_month(month, -plan.fx_average_first_offset),
            end=h8.shift_month(month, -plan.fx_average_last_offset),
        )):
            day = h8.common_date(data, [currency], historical_month)
            history.append(h8.usd_price(data, currency, day))
        average = sum(history, Decimal(0)) / len(history)
        prices = price_change(plan, cpi, currency.code, month)
        value = -(math.log(h8.usd_price(data, currency, before) / average)
                  + prices["change"] - usd["change"])
        signals[currency.code] = {"V": value, "b": before.isoformat(), "S_bar": float(average),
                                  **prices}
    return {"d_m": start.isoformat(), "e_USD": usd["e"], "currencies": signals}


def value_weights(view: h8.Plan, signals: dict) -> dict[str, float]:
    ordered = sorted(signals["currencies"], key=lambda c: (-signals["currencies"][c]["V"], c))
    k = len(ordered) // view.k_divisor
    weights = {c.code: 0.0 for c in view.currencies}
    for code in ordered[:k]:
        weights[code] = view.gross_per_side / k
    for code in ordered[-k:]:
        weights[code] = -view.gross_per_side / k
    return weights


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2:
        return None
    x, y = fmean(left), fmean(right)
    cross = sum((a - x) * (b - y) for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum((a - x) ** 2 for a in left) * sum((b - y) ** 2 for b in right))
    return cross / denominator if denominator else None


def measure(
    plan: Plan, carry_plan: h8.Plan, data: h8.Data, cpi: CpiData, wedges: h8.WedgeFile,
    carry_rows: Sequence[h8.MonthlyReturn],
) -> dict:
    view = carry_view(plan, carry_plan)
    signals = {}
    weights = {}

    def weights_for(month: str, active: Sequence[h8.Currency]) -> dict[str, float]:
        if month not in weights:
            signals[month] = month_signals(plan, data, cpi, month, active)
            weights[month] = value_weights(view, signals[month])
        return weights[month]

    report = h8.measure(view, data, wedges, weights_for=weights_for)
    combo_view = carry_view(plan, carry_plan, combo=True)

    def combo_weights(month: str, active: Sequence[h8.Currency]) -> dict[str, float]:
        carry = h8.target_weights(carry_plan, data, active, month)
        return {code: plan.combination_weight * carry[code]
                + plan.combination_weight * weights[month][code] for code in carry}

    combo = h8.monthly_returns(combo_view, data, wedges, weights_for=combo_weights)
    carry_net = {r.month: r.net for r in carry_rows}
    correlations, combinations = {}, {}
    for name in ("full", "post", "pre"):
        period = getattr(view, name)
        selected = [r for r in report["monthly"] if period.start <= r["month"] <= period.end
                    and r["month"] in carry_net]
        correlations[name] = correlation([r["net"] for r in selected],
                                          [carry_net[r["month"]] for r in selected])
        combined = h8.period_rows(combo, period)
        combinations[name] = {"sharpe": h8.sharpe([r.net for r in combined]),
                              "annual_mean_net": fmean(r.net for r in combined) * 12}
    report.update(signals=signals, carry_correlation=correlations, combination={
        "periods": combinations,
        "monthly": [{**asdict(r), "start": r.start.isoformat(), "end": r.end.isoformat()}
                    for r in combo],
    })
    return report


def render_markdown(report: dict) -> str:
    lines = [h8.render_markdown(report, title="H9 バリュー研究"),
             "## H8 との相関と 50/50 の組み合わせ（副）", "",
             "| 期間 | H8 との純リターンの相関 | 組み合わせの純 Sharpe | 年率平均純リターン |",
             "| --- | ---: | ---: | ---: |"]
    for name, label in (("full", "全期間"), ("post", "発表後"), ("pre", "発表前")):
        combo = report["combination"]["periods"][name]
        lines.append(f"| {label} | {report['carry_correlation'][name]} | {combo['sharpe']} | "
                     f"{combo['annual_mean_net']} |")
    replaced = [month for month, entry in report["signals"].items()
                if entry["currencies"].get("JPY", {}).get("substituted")]
    lines += ["", "## JPY の基準改定で物価の月を置き換えた保有月", "",
              "、".join(replaced) if replaced else "該当なし。", ""]
    return "\n".join(lines)


def measure_files(
    plan_path: Path, cpi_dir: Path, carry_plan_path: Path, carry_data_dir: Path,
    wedge_path: Path, carry_report_path: Path, output: Path,
) -> dict:
    plan, plan_hash = read_plan(plan_path)
    cpi, cpi_manifest_hash, cpi_hashes = load_data(plan, plan_hash, cpi_dir)
    carry_plan, carry_plan_hash = h8.read_plan(carry_plan_path)
    data, carry_manifest_hash, carry_hashes = h8.load_data(
        carry_plan, carry_plan_hash, carry_data_dir,
    )
    wedges, wedge_hash, sidecar_hash = h8.load_wedges(
        wedge_path, carry_plan, carry_plan_hash, carry_manifest_hash,
    )
    raw_report = carry_report_path.read_bytes()
    carry_hashes_expected = {"plan_sha256": carry_plan_hash, "manifest_sha256": carry_manifest_hash,
                            "wedges_sha256": wedge_hash, "report_sha256": h8.digest(raw_report)}
    if plan.carry.model_dump() != carry_hashes_expected:
        raise ValueError("H8 の入力の sha256 が H9 の Plan と一致しません")
    carry_report = json.loads(raw_report)
    provenance = carry_report.get("provenance", {})
    if any(provenance.get(key) != value for key, value in carry_hashes_expected.items()
           if key != "report_sha256"):
        raise ValueError("H8 report の provenance が入力と一致しません")
    carry_rows = h8.monthly_returns(carry_plan, data, wedges)
    expected = [{"month": r.month, "net": r.net} for r in carry_rows]
    recorded = [{"month": r.get("month"), "net": r.get("net")}
                for r in carry_report.get("monthly", [])]
    if recorded != expected:
        raise ValueError("H8 の月次純リターンの再計算が report と一致しません")
    if output.exists():
        raise FileExistsError(f"出力先が既にあります: {output}")
    report = measure(plan, carry_plan, data, cpi, wedges, carry_rows)
    report.update(plan=plan.model_dump(mode="json"), provenance={
        "plan_sha256": plan_hash, "manifest_sha256": cpi_manifest_hash,
        "sources_sha256": cpi_hashes, "carry": {
            **carry_hashes_expected, "series_sha256": carry_hashes,
            "wedges_manifest_sha256": sidecar_hash,
        }, **git_state(),
    })
    content = h8.json_bytes(report)
    markdown = render_markdown(report)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "report.json").open("xb") as target:
        target.write(content)
    with (output / "report.md").open("x", encoding="utf-8") as target:
        target.write(markdown)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (("fetch", "物価 CSV の元のバイト列と manifest.json を保存"),
                               ("measure", "固定ファイルを照合し report.json と report.md を出力")):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--plan", required=True, type=Path)
        command.add_argument("--output-dir", required=True, type=Path)
        if name == "measure":
            for argument in ("cpi-dir", "carry-plan", "carry-data-dir", "wedges", "carry-report"):
                command.add_argument(f"--{argument}", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch(args.plan, args.output_dir)
    else:
        measure_files(args.plan, args.cpi_dir, args.carry_plan, args.carry_data_dir,
                      args.wedges, args.carry_report, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
