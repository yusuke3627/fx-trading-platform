"""H8 のキャリー研究。fetch / wedges / measure で入力を固定して測る。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import fmean, stdev
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.request import urlopen
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trading.backtest.event_currency_strength_study import percentile
from trading.backtest.run import git_state
from trading.data.macro.base import payload_hash
from trading.domain.swap import SWAP_MODE_POINTS, SwapSnapshot

if TYPE_CHECKING:
    from psycopg import Connection

Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
Series = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Data = dict[str, dict[date, Decimal]]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Rates(Record):
    rate_3m_series: Series
    rate_overnight_series: Series


class Currency(Rates):
    code: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    fx_series: Series
    fx_quote: Literal["usd_per_foreign", "foreign_per_usd"]
    first_holding_month: Month
    oanda_symbol: Annotated[str, Field(min_length=1)]
    oanda_foreign_is_base: bool


class Period(Record):
    start: Month
    end: Month

    @model_validator(mode="after")
    def ordered(self) -> Period:
        date.fromisoformat(self.start + "-01")
        date.fromisoformat(self.end + "-01")
        if self.start > self.end:
            raise ValueError("期間の開始月は終了月以前にしてください")
        return self


class Sensitivity(Record):
    wedge_multipliers: tuple[float, ...] = (0, 2)
    transaction_cost_multipliers: tuple[float, ...] = (0, 2)

    @model_validator(mode="after")
    def nonnegative(self) -> Sensitivity:
        if any(x < 0 for x in self.wedge_multipliers + self.transaction_cost_multipliers):
            raise ValueError("感応度の倍率は 0 以上にしてください")
        return self


class Plan(Record):
    study_version: Annotated[str, Field(min_length=1)]
    fred_csv_url: str = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    usd: Rates
    currencies: tuple[Currency, ...]
    full: Period = Period(start="1986-01", end="2026-08")
    post: Period = Period(start="2013-01", end="2026-08")
    pre: Period = Period(start="1986-01", end="2012-12")
    signal_lag_months: PositiveInt = 3
    k_divisor: Annotated[int, Field(ge=2, strict=True)] = 3
    gross_per_side: float = Field(default=0.5, gt=0)
    transaction_cost_bp: float = Field(default=3, ge=0)
    day_count: PositiveInt = 365
    bootstrap_samples: PositiveInt = 10000
    bootstrap_block_months: PositiveInt = 6
    bootstrap_seed: int = Field(strict=True)
    one_sided_level: float = Field(default=0.95, gt=0.5, lt=1)
    max_undefined_bootstrap_fraction: float = Field(default=0.01, ge=0, lt=1)
    reject_sharpe: float = 0.30
    sensitivity: Sensitivity = Sensitivity()
    snapshot_max_gap_minutes: PositiveInt = 10

    @model_validator(mode="after")
    def consistent(self) -> Plan:
        for field in ("code", "fx_series", "oanda_symbol"):
            if len({getattr(c, field) for c in self.currencies}) != len(self.currencies):
                raise ValueError(f"currencies.{field} は重複させないでください")
        for c in self.currencies:
            date.fromisoformat(c.first_holding_month + "-01")
            if c.code == "USD" or c.first_holding_month > self.full.end:
                raise ValueError("対象通貨と最初の保有月が不正です")
        if sum(c.first_holding_month <= self.full.start for c in self.currencies) < self.k_divisor:
            raise ValueError("最初の保有月に上下の持ち高を作れる通貨数が必要です")
        if not (self.pre.start == self.full.start and self.post.end == self.full.end
                and shift_month(self.pre.end, 1) == self.post.start
                and self.full.start <= self.pre.end < self.full.end):
            raise ValueError("pre と post は full を順に分割してください")
        if "{series}" not in self.fred_csv_url:
            raise ValueError("fred_csv_url に {series} が必要です")
        if {c.fx_series for c in self.currencies} & set(rate_series(self)):
            raise ValueError("為替と金利の系列名を分けてください")
        return self


def shift_month(month: str, offset: int) -> str:
    year, number = map(int, month.split("-"))
    year, number = divmod(year * 12 + number - 1 + offset, 12)
    return f"{year:04d}-{number + 1:02d}"


def months(period: Period) -> list[str]:
    result = []
    month = period.start
    while month <= period.end:
        result.append(month)
        month = shift_month(month, 1)
    return result


def rate_series(plan: Plan) -> list[str]:
    return [s for r in (plan.usd, *plan.currencies)
            for s in (r.rate_3m_series, r.rate_overnight_series)]


def series_names(plan: Plan) -> list[str]:
    return sorted(set(rate_series(plan)) | {c.fx_series for c in plan.currencies})


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def write_json(path: Path, value: dict) -> None:
    with path.open("xb") as output:
        output.write(json_bytes(value))


def read_plan(path: Path) -> tuple[Plan, str]:
    content = path.read_bytes()
    return Plan.model_validate_json(content), digest(content)


class SeriesManifest(Record):
    url: str
    sha256: Digest
    rows: int = Field(ge=0)
    first_date: date | None
    last_date: date | None


class Manifest(Record):
    plan_sha256: Digest
    retrieved_at: AwareDatetime
    series: dict[str, SeriesManifest]


def parse_csv(content: bytes, series: str, *, positive: bool = False) -> tuple[dict, int]:
    reader = csv.reader(io.StringIO(content.decode("utf-8")), strict=True)
    if next(reader, None) != ["observation_date", series]:
        raise ValueError(f"{series}: CSV のヘッダーが不正です")
    values: dict[date, Decimal] = {}
    seen: set[date] = set()
    count = 0
    for count, row in enumerate(reader, 1):
        if len(row) != 2:
            raise ValueError(f"{series}: {count + 1} 行目の列数が不正です")
        day = date.fromisoformat(row[0])
        if day.isoformat() != row[0] or day in seen:
            raise ValueError(f"{series}: 日付が ISO 形式でないか重複しています: {row[0]}")
        seen.add(day)
        if row[1] in ("", "."):
            continue
        try:
            value = Decimal(row[1])
        except InvalidOperation as exc:
            raise ValueError(f"{series}: 数値が不正です: {row[1]}") from exc
        if not value.is_finite() or (positive and value <= 0):
            raise ValueError(f"{series}: 有限の{'正の' if positive else ''}数値が必要です")
        values[day] = value
    return values, count


def download(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def fetch(plan_path: Path, output: Path, *, retrieve: Callable[[str], bytes] = download) -> None:
    plan, plan_hash = read_plan(plan_path)
    output.mkdir(parents=True, exist_ok=False)
    metadata = {}
    fx = {c.fx_series for c in plan.currencies}
    for series in series_names(plan):
        url = plan.fred_csv_url.format(series=series)
        content = retrieve(url)
        values, count = parse_csv(content, series, positive=series in fx)
        with (output / f"{series}.csv").open("xb") as target:
            target.write(content)
        metadata[series] = SeriesManifest(
            url=url, sha256=digest(content), rows=count,
            first_date=min(values, default=None), last_date=max(values, default=None),
        )
    manifest = Manifest(plan_sha256=plan_hash, retrieved_at=datetime.now(UTC), series=metadata)
    write_json(output / "manifest.json", manifest.model_dump(mode="json"))


def load_data(plan: Plan, plan_hash: str, directory: Path) -> tuple[Data, str, dict[str, str]]:
    content = (directory / "manifest.json").read_bytes()
    manifest = Manifest.model_validate_json(content)
    if manifest.plan_sha256 != plan_hash or set(manifest.series) != set(series_names(plan)):
        raise ValueError("manifest の Plan ハッシュまたは系列一覧が一致しません")
    data = {}
    hashes = {}
    fx = {c.fx_series for c in plan.currencies}
    for series, entry in manifest.series.items():
        raw = (directory / f"{series}.csv").read_bytes()
        if digest(raw) != entry.sha256:
            raise ValueError(f"{series}: データの sha256 が一致しません")
        values, count = parse_csv(raw, series, positive=series in fx)
        if (entry.url != plan.fred_csv_url.format(series=series)
                or (count, min(values, default=None), max(values, default=None))
                != (entry.rows, entry.first_date, entry.last_date)):
            raise ValueError(f"{series}: manifest の内容が CSV と一致しません")
        data[series], hashes[series] = values, entry.sha256
    return data, digest(content), hashes


def rate(data: Data, rates: Rates, month: str, code: str) -> Decimal:
    day = date.fromisoformat(month + "-01")
    for series in (rates.rate_3m_series, rates.rate_overnight_series):
        if day in data[series]:
            return data[series][day]
    raise ValueError(f"{code} {month}: 金利がありません "
                     f"({rates.rate_3m_series}, {rates.rate_overnight_series})")


def usd_price(data: Data, currency: Currency, day: date) -> Decimal:
    value = data[currency.fx_series][day]
    return value if currency.fx_quote == "usd_per_foreign" else Decimal(1) / value


def common_date(data: Data, currencies: Sequence[Currency], month: str) -> date:
    candidates = set.intersection(*(set(data[c.fx_series]) for c in currencies))
    days = [d for d in candidates if d.isoformat()[:7] == month]
    if not days:
        raise ValueError(f"{month}: 対象通貨すべての為替がある日がありません")
    return min(days)


def weekly_multiplier(snapshot: SwapSnapshot) -> Decimal:
    values = [snapshot.swap_sunday, snapshot.swap_monday, snapshot.swap_tuesday,
              snapshot.swap_wednesday, snapshot.swap_thursday, snapshot.swap_friday,
              snapshot.swap_saturday]
    present = [v for v in values if v is not None]
    if not present:
        return Decimal(7)
    if len(present) != 7:
        raise ValueError(f"{snapshot.symbol}: 曜日別の倍率が一部だけ記録されています")
    if any(not v.is_finite() or v < 0 for v in present):
        raise ValueError(f"{snapshot.symbol}: 曜日別の倍率が不正です")
    return sum(present, Decimal(0))


class Wedge(Record):
    code: str
    symbol: str
    snapshot_id: UUID
    known_at: AwareDatetime
    payload_hash: Digest
    swap_mode: Literal[1]
    swap_long: Decimal
    swap_short: Decimal
    W: Decimal = Field(ge=0)
    point: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    price_date: date
    theoretical_month: Month
    observed_long: Decimal
    observed_short: Decimal
    theoretical_long: Decimal
    theoretical_short: Decimal
    u_long: Decimal = Field(ge=0)
    u_short: Decimal = Field(ge=0)


class WedgeFile(Record):
    plan_sha256: Digest
    manifest_sha256: Digest
    as_of: AwareDatetime
    pairs: tuple[Wedge, ...]


def calculate_wedge(
    plan: Plan, data: Data, currency: Currency, snapshot: SwapSnapshot, point: Decimal,
) -> Wedge:
    if snapshot.swap_mode != SWAP_MODE_POINTS:
        raise ValueError(f"{snapshot.symbol}: swap_mode は points (1) だけに対応しています")
    if not point.is_finite() or point <= 0:
        raise ValueError(f"{snapshot.symbol}: point は有限の正数が必要です")
    days = [d for d in data[currency.fx_series] if d <= snapshot.known_at.astimezone(UTC).date()]
    if not days:
        raise ValueError(f"{currency.code}: 記録日以前の為替がありません")
    price_day = max(days)
    price = usd_price(data, currency, price_day)
    if not currency.oanda_foreign_is_base:
        price = Decimal(1) / price
    foreign_months = set(data[currency.rate_3m_series]) | set(data[currency.rate_overnight_series])
    usd_months = set(data[plan.usd.rate_3m_series]) | set(data[plan.usd.rate_overnight_series])
    common = [d for d in foreign_months & usd_months if d.day == 1]
    if not common:
        raise ValueError(f"{currency.code}/USD: 両方の金利がある月がありません")
    month = max(common).isoformat()[:7]
    theory = rate(data, currency, month, currency.code) - rate(data, plan.usd, month, "USD")
    if not currency.oanda_foreign_is_base:
        theory = -theory
    weekly = weekly_multiplier(snapshot)
    factor = point * weekly * plan.day_count / 7 / price * 100
    observed_long, observed_short = snapshot.swap_long * factor, snapshot.swap_short * factor
    return Wedge(
        code=currency.code, symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id,
        known_at=snapshot.known_at, payload_hash=snapshot.payload_hash, swap_mode=snapshot.swap_mode,
        swap_long=snapshot.swap_long, swap_short=snapshot.swap_short, W=weekly, point=point,
        price=price, price_date=price_day, theoretical_month=month,
        observed_long=observed_long, observed_short=observed_short,
        theoretical_long=theory, theoretical_short=-theory,
        u_long=max(Decimal(0), theory - observed_long),
        u_short=max(Decimal(0), -theory - observed_short),
    )


def wedges_from_db(
    conn: Connection, plan: Plan, data: Data, as_of: datetime,
    plan_hash: str, manifest_hash: str,
) -> WedgeFile:
    from psycopg.rows import dict_row

    as_of = as_utc(as_of)
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT DISTINCT ON (symbol) id AS snapshot_id, symbol, swap_mode,
               swap_long, swap_short, swap_rollover3days, swap_sunday, swap_monday,
               swap_tuesday, swap_wednesday, swap_thursday, swap_friday, swap_saturday,
               payload_hash, retrieved_at, known_at FROM swap_snapshots
               WHERE symbol = ANY(%s) AND known_at <= %s
               ORDER BY symbol, known_at DESC, id DESC""",
            ([c.oanda_symbol for c in plan.currencies], as_of),
        )
        snapshots = {row["symbol"]: SwapSnapshot(**row) for row in cursor.fetchall()}
        missing = {c.oanda_symbol for c in plan.currencies} - snapshots.keys()
        if missing:
            raise ValueError(f"スワップの記録がありません: {', '.join(sorted(missing))}")
        times = [s.known_at for s in snapshots.values()]
        if max(times) - min(times) > timedelta(minutes=plan.snapshot_max_gap_minutes):
            raise ValueError("同じ時刻の記録ではない: known_at の差が許容時間を超えています")
        pairs = []
        for currency in plan.currencies:
            snapshot = snapshots[currency.oanda_symbol]
            cursor.execute(
                """SELECT payload FROM events WHERE event_type = %s AND payload_hash = %s
                   AND known_at <= %s ORDER BY known_at DESC, id DESC LIMIT 1""",
                ("SWAP_SNAPSHOT_RAW", snapshot.payload_hash, as_of),
            )
            row = cursor.fetchone()
            if row is None or payload_hash(row["payload"]) != snapshot.payload_hash:
                raise ValueError(f"{snapshot.symbol}: 対応する raw payload がないかハッシュ不一致です")
            try:
                point = Decimal(str(row["payload"]["point"]))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError(f"{snapshot.symbol}: raw payload の point が不正です") from exc
            pairs.append(calculate_wedge(plan, data, currency, snapshot, point))
    return WedgeFile(plan_sha256=plan_hash, manifest_sha256=manifest_hash,
                     as_of=as_of, pairs=tuple(pairs))


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as-of にタイムゾーンを指定してください")
    return value.astimezone(UTC)


def wedge_manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def write_wedges(path: Path, result: WedgeFile) -> None:
    sidecar = wedge_manifest_path(path)
    if path.exists() or sidecar.exists():
        raise FileExistsError(f"スワップ出力が既にあります: {path}")
    content = json_bytes(result.model_dump(mode="json"))
    with path.open("xb") as output:
        output.write(content)
    write_json(sidecar, {"sha256": digest(content)})


def load_wedges(path: Path, plan: Plan, plan_hash: str, manifest_hash: str) -> tuple[WedgeFile, str, str]:
    content = path.read_bytes()
    sidecar = wedge_manifest_path(path).read_bytes()
    if json.loads(sidecar).get("sha256") != digest(content):
        raise ValueError("スワップファイルの sha256 が一致しません")
    result = WedgeFile.model_validate_json(content)
    if result.plan_sha256 != plan_hash or result.manifest_sha256 != manifest_hash:
        raise ValueError("スワップファイルの Plan / manifest ハッシュが一致しません")
    expected = {(c.code, c.oanda_symbol) for c in plan.currencies}
    if len(result.pairs) != len(expected) or {(p.code, p.symbol) for p in result.pairs} != expected:
        raise ValueError("スワップファイルの対象通貨が Plan と一致しません")
    return result, digest(content), digest(sidecar)


@dataclass(frozen=True)
class MonthlyReturn:
    month: str
    start: date
    end: date
    weights: dict[str, float]
    before_weights: dict[str, float]
    fx_returns: dict[str, float]
    fx: float
    interest: float
    gross: float
    turnover: float
    transaction_cost: float
    wedge_cost: float
    net: float
    basket: float


def target_weights(plan: Plan, data: Data, currencies: Sequence[Currency], month: str) -> dict:
    signal_month = shift_month(month, -plan.signal_lag_months)
    ordered = sorted(currencies, key=lambda c: (-rate(data, c, signal_month, c.code), c.code))
    k = len(ordered) // plan.k_divisor
    weights = {c.code: 0.0 for c in plan.currencies}
    for c in ordered[:k]:
        weights[c.code] = plan.gross_per_side / k
    for c in ordered[-k:]:
        weights[c.code] = -plan.gross_per_side / k
    return weights


def monthly_returns(
    plan: Plan, data: Data, wedges: WedgeFile, *, wedge_multiplier: float = 1,
    transaction_cost_multiplier: float = 1,
) -> list[MonthlyReturn]:
    by_code = {p.code: p for p in wedges.pairs}
    result: list[MonthlyReturn] = []
    for month in months(plan.full):
        active = [c for c in plan.currencies if c.first_holding_month <= month]
        start = common_date(data, active, month)
        end = common_date(data, active, shift_month(month, 1))
        if result and result[-1].end != start:
            raise ValueError(f"{month}: 前月の終わりと当月の開始日が異なります")
        weights = target_weights(plan, data, active, month)
        before = {c.code: 0.0 for c in plan.currencies}
        if result:
            previous = result[-1]
            for c in plan.currencies:
                if previous.weights[c.code]:
                    before[c.code] = (previous.weights[c.code]
                                      * float(usd_price(data, c, start)
                                              / usd_price(data, c, previous.start))
                                      / (1 + previous.net))
        fx_returns = {c.code: float(usd_price(data, c, end) / usd_price(data, c, start)) - 1
                      for c in active}
        years = (end - start).days / plan.day_count
        usd_rate = rate(data, plan.usd, month, "USD")
        fx = sum(weights[c.code] * fx_returns[c.code] for c in active)
        interest = sum(weights[c.code] * float(rate(data, c, month, c.code) - usd_rate)
                       / 100 * years for c in active)
        turnover = sum(abs(weights[code] - before[code]) for code in weights)
        transaction_cost = turnover * plan.transaction_cost_bp / 10000 * transaction_cost_multiplier
        wedge_cost = 0.0
        for c in active:
            weight, wedge = weights[c.code], by_code[c.code]
            pair_long = (weight > 0) == c.oanda_foreign_is_base
            u = wedge.u_long if pair_long else wedge.u_short
            wedge_cost += abs(weight) * float(u) / 100 * years * wedge_multiplier
        gross = fx + interest
        net = gross - transaction_cost - wedge_cost
        if not math.isfinite(net) or net <= -1:
            raise ValueError(f"{month}: 純リターンから正の資産額を計算できません")
        result.append(MonthlyReturn(month, start, end, weights, before, fx_returns, fx, interest,
                                    gross, turnover, transaction_cost, wedge_cost, net,
                                    fmean(fx_returns.values())))
    return result


def sharpe(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    deviation = stdev(values)
    return fmean(values) / deviation * math.sqrt(12) if deviation else None


def circular_sample(values: Sequence[float], block: int, rng: random.Random) -> list[float]:
    sample = []
    while len(sample) < len(values):
        start = rng.randrange(len(values))
        sample.extend(values[(start + i) % len(values)] for i in range(block))
    return sample[:len(values)]


def bootstrap(values: Sequence[float], plan: Plan, rng: random.Random) -> dict:
    estimates = [sharpe(circular_sample(values, plan.bootstrap_block_months, rng))
                 for _ in range(plan.bootstrap_samples)]
    defined = [x for x in estimates if x is not None]
    return {"sharpe": sharpe(values), "lower": percentile(defined, 1 - plan.one_sided_level),
            "upper": percentile(defined, plan.one_sided_level),
            "undefined_samples": len(estimates) - len(defined), "samples": len(estimates)}


def decide(full: dict, post: dict, plan: Plan) -> dict:
    for name, stats in (("full", full), ("post", post)):
        if stats["undefined_samples"] / stats["samples"] > plan.max_undefined_bootstrap_fraction:
            return {"verdict": "判定不能", "reason": (
                f"{name}: 未定義のブートストラップが {stats['undefined_samples']}/{stats['samples']} 回で、"
                f"許容割合 {plan.max_undefined_bootstrap_fraction:.1%} を超える"
            )}
        if any(stats[k] is None for k in ("sharpe", "lower", "upper")):
            return {"verdict": "判定不能", "reason": f"{name}: シャープレシオまたは区間が未定義"}
    if full["upper"] < plan.reject_sharpe or post["upper"] < plan.reject_sharpe:
        return {"verdict": "棄却", "reason": "全期間または発表後の片側上限が棄却閾値を下回る"}
    if full["lower"] > 0 and post["sharpe"] > 0:
        return {"verdict": "支持", "reason": "全期間の片側下限と発表後の点推定がともに正"}
    return {"verdict": "判定不能", "reason": "支持・棄却のいずれの条件も満たさない"}


def period_rows(rows: Sequence[MonthlyReturn], period: Period) -> list[MonthlyReturn]:
    return [r for r in rows if period.start <= r.month <= period.end]


def secondary(rows: Sequence[MonthlyReturn], plan: Plan) -> dict:
    values = [r.net for r in rows]
    mean = fmean(values)
    variance = fmean((x - mean) ** 2 for x in values)
    skew = fmean((x - mean) ** 3 for x in values) / variance ** 1.5 if variance else None
    wealth = peak = 1.0
    peak_date = rows[0].start
    drawdown = {"return": 0.0, "peak": None, "trough": None}
    for row in rows:
        wealth *= 1 + row.net
        if wealth > peak:
            peak, peak_date = wealth, row.end
        depth = wealth / peak - 1
        if depth < drawdown["return"]:
            drawdown = {"return": depth, "peak": peak_date.isoformat(), "trough": row.end.isoformat()}
    worst = {}
    for window in (1, 3, 12):
        candidates = [(math.prod(1 + r.net for r in rows[i:i + window]) - 1, i)
                      for i in range(len(rows) - window + 1)]
        if not candidates:
            worst[str(window)] = None
        else:
            value, index = min(candidates)
            worst[str(window)] = {"return": value, "first_month": rows[index].month,
                                  "last_month": rows[index + window - 1].month}
    basket = [r.basket for r in rows]
    basket_mean = fmean(basket)
    cross = sum((x - mean) * (y - basket_mean) for x, y in zip(values, basket, strict=True))
    denominator = math.sqrt(sum((x - mean) ** 2 for x in values)
                            * sum((y - basket_mean) ** 2 for y in basket))
    return {
        "gross_sharpe": sharpe([r.gross for r in rows]),
        "annual_mean_fx": fmean(r.fx for r in rows) * 12,
        "annual_mean_interest": fmean(r.interest for r in rows) * 12,
        "annual_mean_net": mean * 12,
        "max_drawdown": drawdown, "skewness": skew,
        "skewness_method": "第3中心モーメント / 第2中心モーメントの1.5乗（補正なし）",
        "worst_months": worst,
        "composition": {c.code: {"long_fraction": sum(r.weights[c.code] > 0 for r in rows) / len(rows),
                                  "short_fraction": sum(r.weights[c.code] < 0 for r in rows) / len(rows)}
                        for c in plan.currencies},
        "mean_monthly_turnover": fmean(r.turnover for r in rows),
        "basket_correlation": cross / denominator if denominator else None,
    }


def measure(plan: Plan, data: Data, wedges: WedgeFile) -> dict:
    rows = monthly_returns(plan, data, wedges)
    rng = random.Random(plan.bootstrap_seed)
    periods = {}
    for name in ("full", "post", "pre"):
        selected = period_rows(rows, getattr(plan, name))
        periods[name] = {"period": getattr(plan, name).model_dump(), "months": len(selected),
                         **bootstrap([r.net for r in selected], plan, rng),
                         "secondary": secondary(selected, plan)}
    sensitivities = []
    for component, multipliers in (
        ("wedge", plan.sensitivity.wedge_multipliers),
        ("transaction_cost", plan.sensitivity.transaction_cost_multipliers),
    ):
        for multiplier in multipliers:
            changed = monthly_returns(plan, data, wedges, **{f"{component}_multiplier": multiplier})
            sensitivities.append({"component": component, "multiplier": multiplier,
                                  **{name: sharpe([r.net for r in period_rows(changed, getattr(plan, name))])
                                     for name in ("full", "post")}})
    monthly = [{**asdict(r), "start": r.start.isoformat(), "end": r.end.isoformat()} for r in rows]
    return {"decision": decide(periods["full"], periods["post"], plan), "periods": periods,
            "sensitivity": sensitivities, "monthly": monthly}


def render_markdown(report: dict) -> str:
    decision = report["decision"]
    lines = ["# H8 キャリー研究", "", f"判定: **{decision['verdict']}**。{decision['reason']}。", "",
             "片側区間は純リターンの循環ブロック・ブートストラップ。副統計は判定に使わない。", "",
             "| 期間 | 月数 | 純 Sharpe | 片側下限 | 片側上限 | 未定義の抽出回数 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, label in (("full", "全期間"), ("post", "発表後"), ("pre", "発表前（副）")):
        p = report["periods"][name]
        lines.append(f"| {label} {p['period']['start']}〜{p['period']['end']} | {p['months']} | "
                     f"{p['sharpe']} | {p['lower']} | {p['upper']} | {p['undefined_samples']}/{p['samples']} |")
    lines += ["", "## 副統計", "", "リターンは小数比率。年率平均は月次平均の12倍。", ""]
    for name, p in report["periods"].items():
        s = p["secondary"]
        lines += [f"### {name}", "", (f"粗 Sharpe: {s['gross_sharpe']}。年率平均: "
                  f"為替 {s['annual_mean_fx']}、金利差 {s['annual_mean_interest']}、純 {s['annual_mean_net']}。"),
                  "", (f"最大ドローダウン: {s['max_drawdown']}。歪度: {s['skewness']} "
                  f"（{s['skewness_method']}）。"), "",
                  (f"平均月次回転: {s['mean_monthly_turnover']}。対ドル等加重バスケットとの相関: "
                  f"{s['basket_correlation']}。"), "", "最悪の複利リターン:", ""]
        lines += [f"- {window}か月: {value}" for window, value in s["worst_months"].items()]
        lines += ["", "| 通貨 | 買い側の月の割合 | 売り側の月の割合 |",
                  "| --- | ---: | ---: |"]
        lines += [f"| {code} | {v['long_fraction']} | {v['short_fraction']} |"
                  for code, v in s["composition"].items()]
        lines += [""]
    lines += ["## 感応度（純 Sharpe の点推定）", "", "| 変更するコスト | 倍率 | 全期間 | 発表後 |",
              "| --- | ---: | ---: | ---: |"]
    lines += [f"| {s['component']} | {s['multiplier']} | {s['full']} | {s['post']} |"
              for s in report["sensitivity"]]
    lines += ["", "月ごとの持ち高・リターン内訳は report.json の monthly に収録。", "",
              "## 入力とコードの来歴", "", "```json",
              json.dumps(report["provenance"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def measure_files(plan_path: Path, data_dir: Path, wedge_path: Path, output: Path) -> dict:
    plan, plan_hash = read_plan(plan_path)
    data, manifest_hash, hashes = load_data(plan, plan_hash, data_dir)
    wedges, wedge_hash, sidecar_hash = load_wedges(wedge_path, plan, plan_hash, manifest_hash)
    if output.exists():
        raise FileExistsError(f"出力先が既にあります: {output}")
    report = measure(plan, data, wedges)
    report.update(plan=plan.model_dump(mode="json"), provenance={
        "plan_sha256": plan_hash, "manifest_sha256": manifest_hash, "series_sha256": hashes,
        "wedges_sha256": wedge_hash, "wedges_manifest_sha256": sidecar_hash, **git_state(),
    })
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "report.json", report)
    with (output / "report.md").open("x", encoding="utf-8") as target:
        target.write(render_markdown(report))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("fetch", "FRED CSV を取得し、元のバイト列と manifest.json を保存"),
        ("wedges", "DB を読み、上乗せと <output>.manifest.json を保存"),
        ("measure", "固定ファイルだけから report.json と report.md を出力"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--plan", required=True, type=Path)
        if name != "fetch":
            command.add_argument("--data-dir", required=True, type=Path)
        if name == "wedges":
            command.add_argument("--as-of", required=True, type=datetime.fromisoformat,
                                 help="タイムゾーン付き ISO 日時")
            command.add_argument("--output", required=True, type=Path)
        else:
            command.add_argument("--output-dir", required=True, type=Path)
        if name == "measure":
            command.add_argument("--wedges", required=True, type=Path,
                                 help="wedges が出力した JSON（隣の .manifest.json も必要）")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch(args.plan, args.output_dir)
    elif args.command == "measure":
        measure_files(args.plan, args.data_dir, args.wedges, args.output_dir)
    else:
        import psycopg

        plan, plan_hash = read_plan(args.plan)
        data, manifest_hash, _ = load_data(plan, plan_hash, args.data_dir)
        as_of = as_utc(args.as_of)
        if args.output.exists() or wedge_manifest_path(args.output).exists():
            raise FileExistsError(f"スワップ出力が既にあります: {args.output}")
        with psycopg.connect(os.environ["TRADING_DB_DSN"],
                             options="-c default_transaction_read_only=on") as conn:
            result = wedges_from_db(conn, plan, data, as_of, plan_hash, manifest_hash)
        write_wedges(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
