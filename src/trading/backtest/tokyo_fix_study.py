"""H10 仲値前の研究。fetch / quotes / measure で入力を固定して測る。"""
from __future__ import annotations

import argparse
import calendar
import csv
import http.client
import io
import math
import os
import random
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as LocalTime
from decimal import Decimal
from pathlib import Path
from statistics import fmean, stdev
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from trading.backtest.carry_study import Record, digest, git_state, json_bytes, write_json
from trading.backtest.event_currency_strength_study import percentile
from trading.data.market.dukascopy import (
    POINT_SCALES,
    decode_bi5,
    hour_url,
    known_to_broker_label,
)
from trading.domain.market import Tick

if TYPE_CHECKING:
    from psycopg import Connection

JST = ZoneInfo("Asia/Tokyo")
USER_AGENT = "fx-trading-platform-research/1.0"
SLOTS = ("entry", "exit", "paper_entry", "paper_switch", "paper_exit")
ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Period(Record):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Period:
        if self.start > self.end:
            raise ValueError("期間の開始日は終了日以前にしてください")
        return self


class Oanda(Period):
    source: str = Field(min_length=1)
    server_ahead_of_ny_hours: int = Field(strict=True)
    max_tick_id: PositiveInt


class Plan(Record):
    study_version: str = Field(min_length=1)
    bootstrap_seed: int = Field(strict=True)
    bootstrap_samples: PositiveInt
    one_sided_level: float = Field(gt=0.5, lt=1)
    reject_threshold_bp: float
    cost_multipliers: tuple[Annotated[float, Field(ge=0)], ...]
    trim_fraction: float = Field(ge=0, lt=0.5)
    secondary_block_months: PositiveInt
    annualization_days: float = Field(gt=0)
    extreme_days: PositiveInt
    symbol: str
    pip_size: Decimal = Field(gt=0)
    holiday_csv_url: str
    bank_closed_days: tuple[str, ...]
    gotobi_days: tuple[Annotated[int, Field(ge=1, le=31, strict=True)], ...]
    entry: ClockTime
    exit: ClockTime
    paper_entry: ClockTime
    paper_switch: ClockTime
    paper_exit: ClockTime
    quote_window_seconds: PositiveInt
    fetch_range: Period
    calibration: Period
    main: Period
    post: Period
    oanda: Oanda

    @model_validator(mode="after")
    def consistent(self) -> Plan:
        if self.symbol not in POINT_SCALES:
            raise ValueError("symbol に対応する Dukascopy の point scale がありません")
        url = urlsplit(self.holiday_csv_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("holiday_csv_url は認証情報を含まない HTTPS URL にしてください")
        for day in self.bank_closed_days:
            if not re.fullmatch(r"[0-9]{2}-[0-9]{2}", day):
                raise ValueError("bank_closed_days は MM-DD にしてください")
            date.fromisoformat("2000-" + day)
        for period in (self.calibration, self.main, self.post, self.oanda):
            if not self.fetch_range.start <= period.start <= period.end <= self.fetch_range.end:
                raise ValueError("すべての期間を fetch_range 内にしてください")
        if not self.entry < self.exit or not self.paper_entry < self.paper_switch < self.paper_exit:
            raise ValueError("取引の時刻は開始・切替・終了の順にしてください")
        for slot in SLOTS:
            start, end = quote_window(date(2000, 1, 1), slot, self)
            hour = datetime(2000, 1, 1, tzinfo=UTC)
            if not hour <= start < end <= hour + timedelta(hours=1):
                raise ValueError("価格の窓は UTC 0 時台のファイル内にしてください")
        return self


def read_plan(path: Path) -> tuple[Plan, str]:
    content = path.read_bytes()
    return Plan.model_validate_json(content), digest(content)


def days(period: Period) -> list[date]:
    return [period.start + timedelta(days=i)
            for i in range((period.end - period.start).days + 1)]


def month_start(day: date, offset: int = 0) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, 1)


def months(period: Period) -> list[date]:
    result = []
    month = month_start(period.start)
    while month <= period.end:
        result.append(month)
        month = month_start(month, 1)
    return result


def parse_holidays(content: bytes, period: Period) -> set[date]:
    reader = csv.reader(io.StringIO(content.decode("cp932")), strict=True)
    if next(reader, None) != ["国民の祝日・休日月日", "国民の祝日・休日名称"]:
        raise ValueError("祝日の CSV の見出しが不正です")
    holidays: set[date] = set()
    for row in reader:
        if len(row) != 2 or not re.fullmatch(r"[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}", row[0]):
            raise ValueError("祝日の CSV の日付の形式が不正です")
        day = date(*map(int, row[0].split("/")))
        if day in holidays:
            raise ValueError(f"祝日が重複しています: {day}")
        holidays.add(day)
    missing = set(range(period.start.year, period.end.year + 1)) - {d.year for d in holidays}
    if missing:
        raise ValueError(f"祝日の CSV に年がありません: {sorted(missing)}")
    return holidays


def is_business_day(day: date, holidays: set[date], plan: Plan) -> bool:
    return (day.weekday() < 5 and day not in holidays
            and day.strftime("%m-%d") not in plan.bank_closed_days)


def business_days(period: Period, holidays: set[date], plan: Plan) -> list[date]:
    return [d for d in days(period) if is_business_day(d, holidays, plan)]


def gotobi_dates(period: Period, holidays: set[date], plan: Plan) -> set[date]:
    result = set()
    # 翌月の支払日が前倒しされ、期間末に入る場合も含める。
    expanded = Period(start=month_start(period.start, -1), end=month_start(period.end, 1))
    for month in months(expanded):
        last = calendar.monthrange(month.year, month.month)[1]
        for number in set(plan.gotobi_days) | {last}:
            if number > last:
                continue
            day = month.replace(day=number)
            while not is_business_day(day, holidays, plan):
                day -= timedelta(days=1)
            if period.start <= day <= period.end:
                result.add(day)
    return result


def quote_window(day: date, slot: str, plan: Plan) -> tuple[datetime, datetime]:
    target = datetime.combine(day, LocalTime.fromisoformat(getattr(plan, slot)), JST)
    target = target.astimezone(UTC)
    width = timedelta(seconds=plan.quote_window_seconds)
    return (target - width, target) if slot == "paper_exit" else (target, target + width)


def decode_day(content: bytes, day: date, plan: Plan) -> list[Tick]:
    hour = datetime.combine(day, LocalTime(), UTC)
    ticks = decode_bi5(content, plan.symbol, hour, hour)
    previous = hour
    for tick in ticks:
        if not previous <= tick.time < hour + timedelta(hours=1):
            raise ValueError(f"{day}: tick の時刻・順序が不正です")
        if not tick.bid.is_finite() or not tick.ask.is_finite() or not 0 < tick.bid <= tick.ask:
            raise ValueError(f"{day}: tick の価格が不正です")
        previous = tick.time
    return ticks


class HTTPSDownloader:
    """取得中に開く接続は一つだけとし、同じホストでは再利用する。"""

    def __init__(self) -> None:
        self.connection: http.client.HTTPSConnection | None = None
        self.host: str | None = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def __call__(self, url: str) -> bytes | None:
        parts = urlsplit(url)
        if self.connection is None or self.host != parts.netloc:
            self.close()
            self.host = parts.netloc
            self.connection = http.client.HTTPSConnection(self.host, timeout=30)
        try:
            target = parts.path + ("?" + parts.query if parts.query else "")
            self.connection.request("GET", target, headers={
                "Connection": "keep-alive", "User-Agent": USER_AGENT,
            })
            response = self.connection.getresponse()
            body = response.read()
            if response.status == 404:
                return None
            if response.status != 200:
                raise OSError(f"HTTP {response.status}: {url}")
            return body
        except (OSError, http.client.HTTPException):
            self.close()
            raise


def fetch(
    plan_path: Path, output: Path, *, retrieve: Callable[[str], bytes | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    plan, plan_hash = read_plan(plan_path)
    if (output / "manifest.json").exists():
        raise FileExistsError("manifest.json が既にあります")
    downloader = HTTPSDownloader() if retrieve is None else None
    get = downloader if downloader is not None else retrieve
    requested = False

    def download(url: str) -> bytes | None:
        nonlocal requested
        waits = (5, 15, 45, 120, 300) + (600,) * 12
        for attempt in range(len(waits) + 1):
            if requested:
                sleep(0.5)
            requested = True
            try:
                return get(url)
            except (OSError, http.client.HTTPException) as exc:
                if attempt == len(waits):
                    raise RuntimeError("取得を中断しました。同じコマンドで再開できます") from exc
                sleep(waits[attempt])

    try:
        # 再開で、別の Plan のもとで取ったファイルを黙って使わない。
        marker = output / "plan.sha256"
        if output.exists():
            if not marker.exists() or marker.read_text(encoding="ascii") != plan_hash:
                raise ValueError("再開先の plan.sha256 が Plan と一致しません")
        else:
            output.mkdir(parents=True)
            marker.write_text(plan_hash, encoding="ascii")
        holiday_path = output / "syukujitsu.csv"
        if holiday_path.exists():
            content = holiday_path.read_bytes()
        else:
            content = download(plan.holiday_csv_url)
            if content is None:
                raise ValueError("祝日の CSV が HTTP 404 です")
            parse_holidays(content, plan.fetch_range)
            with holiday_path.open("xb") as target:
                target.write(content)
        holidays = parse_holidays(content, plan.fetch_range)
        holiday_manifest = {"url": plan.holiday_csv_url, "sha256": digest(content),
                            "bytes": len(content)}
        directory = output / "dukascopy"
        directory.mkdir(exist_ok=True)
        files, missing = {}, []
        for day in business_days(plan.fetch_range, holidays, plan):
            path = directory / f"{day}.bi5"
            marker = path.with_suffix(".missing")
            if marker.exists():
                if path.exists() or marker.read_bytes() != b"404":
                    raise ValueError(f"{day}: 欠測ファイルが不正です")
                missing.append(day.isoformat())
                continue
            if path.exists():
                payload = path.read_bytes()
            else:
                payload = download(hour_url(plan.symbol, datetime.combine(day, LocalTime(), UTC)))
                if payload is None:
                    with marker.open("xb") as target:
                        target.write(b"404")
                    missing.append(day.isoformat())
                    continue
            ticks = decode_day(payload, day, plan)
            if not path.exists():
                with path.open("xb") as target:
                    target.write(payload)
            files[day.isoformat()] = {"sha256": digest(payload), "bytes": len(payload),
                                      "ticks": len(ticks)}
        write_json(output / "manifest.json", {
            "plan_sha256": plan_hash, "completed_at": datetime.now(UTC).isoformat(),
            "holiday_csv": holiday_manifest, "business_days": len(files) + len(missing),
            "files": files, "missing": missing,
        })
    finally:
        if downloader is not None:
            downloader.close()


class FileInfo(Record):
    sha256: Digest
    bytes: int = Field(ge=0)
    ticks: int = Field(ge=0)


class HolidayInfo(Record):
    url: str
    sha256: Digest
    bytes: int = Field(ge=0)


class Manifest(Record):
    plan_sha256: Digest
    completed_at: AwareDatetime
    holiday_csv: HolidayInfo
    business_days: int = Field(ge=0)
    files: dict[date, FileInfo]
    missing: tuple[date, ...]


def load_data(plan: Plan, plan_hash: str, directory: Path) -> tuple[Manifest, set[date], str]:
    content = (directory / "manifest.json").read_bytes()
    manifest = Manifest.model_validate_json(content)
    if manifest.plan_sha256 != plan_hash:
        raise ValueError("manifest の Plan の sha256 が一致しません")
    holiday = (directory / "syukujitsu.csv").read_bytes()
    if (digest(holiday) != manifest.holiday_csv.sha256
            or len(holiday) != manifest.holiday_csv.bytes
            or manifest.holiday_csv.url != plan.holiday_csv_url):
        raise ValueError("祝日の CSV の sha256・bytes・URL が一致しません")
    holidays = parse_holidays(holiday, plan.fetch_range)
    expected = set(business_days(plan.fetch_range, holidays, plan))
    missing = set(manifest.missing)
    if (set(manifest.files) | missing != expected or set(manifest.files) & missing
            or len(missing) != len(manifest.missing) or manifest.business_days != len(expected)):
        raise ValueError("manifest の営業日の一覧が一致しません")
    for day, info in manifest.files.items():
        payload = (directory / "dukascopy" / f"{day}.bi5").read_bytes()
        if digest(payload) != info.sha256 or len(payload) != info.bytes:
            raise ValueError(f"{day}: bi5 の sha256・bytes が一致しません")
    for day in missing:
        if (directory / "dukascopy" / f"{day}.missing").read_bytes() != b"404":
            raise ValueError(f"{day}: 欠測ファイルが不正です")
    return manifest, holidays, digest(content)


class Quote(Record):
    id: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    event_time: AwareDatetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Quote:
        if self.bid > self.ask:
            raise ValueError("bid は ask 以下にしてください")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class Spread(Record):
    mean_pips: Decimal | None
    days: int = Field(ge=0)


class QuoteFile(Record):
    plan_sha256: Digest
    manifest_sha256: Digest
    max_tick_id: PositiveInt
    quotes: dict[date, dict[str, Quote | None]]
    spreads: dict[str, Spread]


def spread_summary(
    quotes: dict[date, dict[str, Quote | None]], gotobi: set[date], plan: Plan,
) -> dict[str, Spread]:
    result = {}
    for slot in SLOTS:
        values = [(slots[slot].ask - slots[slot].bid) / plan.pip_size
                  for day, slots in quotes.items() if day in gotobi and slots[slot] is not None]
        result[slot] = Spread(mean_pips=sum(values) / len(values) if values else None,
                              days=len(values))
    return result


def quotes_from_db(
    conn: Connection, plan: Plan, holidays: set[date], plan_hash: str, manifest_hash: str,
) -> QuoteFile:
    from psycopg.rows import dict_row

    quotes = {}
    offset = timedelta(hours=plan.oanda.server_ahead_of_ny_hours)
    with conn.cursor(row_factory=dict_row) as cursor:
        for day in business_days(plan.oanda, holidays, plan):
            slots = {}
            for slot in SLOTS:
                start, end = (known_to_broker_label(t, offset)
                              for t in quote_window(day, slot, plan))
                order = "event_time DESC, id DESC" if slot == "paper_exit" else "event_time, id"
                cursor.execute(
                    "SELECT id, event_time, bid, ask FROM market_ticks "
                    "WHERE symbol = %s AND source = %s AND id <= %s "
                    "AND event_time >= %s AND event_time < %s "
                    f"ORDER BY {order} LIMIT 1",
                    (plan.symbol, plan.oanda.source, plan.oanda.max_tick_id, start, end),
                )
                row = cursor.fetchone()
                # セッションのタイムゾーンで表記が変わり、固定したファイルのハッシュがずれないようにする。
                slots[slot] = None if row is None else Quote(**(row | {
                    "id": str(row["id"]), "event_time": row["event_time"].astimezone(UTC)}))
            quotes[day] = slots
    return QuoteFile(plan_sha256=plan_hash, manifest_sha256=manifest_hash,
                     max_tick_id=plan.oanda.max_tick_id, quotes=quotes,
                     spreads=spread_summary(quotes, gotobi_dates(plan.oanda, holidays, plan), plan))


def load_quotes(
    path: Path, plan: Plan, holidays: set[date], plan_hash: str, manifest_hash: str,
) -> tuple[QuoteFile, str]:
    content = path.read_bytes()
    result = QuoteFile.model_validate_json(content)
    if (result.plan_sha256 != plan_hash or result.manifest_sha256 != manifest_hash
            or result.max_tick_id != plan.oanda.max_tick_id):
        raise ValueError("quotes の sha256・max_tick_id が一致しません")
    if set(result.quotes) != set(business_days(plan.oanda, holidays, plan)):
        raise ValueError("quotes の営業日の一覧が一致しません")
    offset = timedelta(hours=plan.oanda.server_ahead_of_ny_hours)
    for day, slots in result.quotes.items():
        if set(slots) != set(SLOTS):
            raise ValueError(f"{day}: quotes の時刻の一覧が一致しません")
        for slot, quote in slots.items():
            if quote is None:
                continue
            start, end = (known_to_broker_label(t, offset) for t in quote_window(day, slot, plan))
            if int(quote.id) > result.max_tick_id or not start <= quote.event_time < end:
                raise ValueError(f"{day}: quotes の id・event_time が範囲外です")
    if result.spreads != spread_summary(
        result.quotes, gotobi_dates(plan.oanda, holidays, plan), plan,
    ):
        raise ValueError("quotes のスプレッドの要約が一致しません")
    return result, digest(content)


def prices(ticks: Sequence[Tick], day: date, plan: Plan) -> dict[str, Decimal | None]:
    result = {}
    for slot in SLOTS:
        start, end = quote_window(day, slot, plan)
        selected = None
        for tick in ticks:
            if tick.time >= end:
                break
            if tick.time < start:
                continue
            selected = tick
            if slot != "paper_exit":
                break
        result[slot] = selected.mid if selected is not None else None
    return result


def returns(
    price: dict[str, Decimal | None], spreads: dict[str, Spread], plan: Plan, *, paper: bool = False,
) -> tuple[dict | None, list[str]]:
    slots = ("paper_entry", "paper_switch", "paper_exit") if paper else ("entry", "exit")
    absent = [slot for slot in slots if price[slot] is None]
    if absent:
        return None, absent
    if any(spreads[slot].mean_pips is None for slot in slots):
        raise ValueError("コストに使うスプレッドの平均がありません")
    entry, exit_ = price[slots[0]], price[slots[-1]]
    if paper:
        switch = price["paper_switch"]
        gross = ((switch - entry) / entry - (exit_ - switch) / switch) * 10000
        cost = (spreads[slots[0]].mean_pips / 2 + spreads["paper_switch"].mean_pips
                + spreads[slots[-1]].mean_pips / 2) * plan.pip_size / entry * 10000
    else:
        gross = (exit_ - entry) / entry * 10000
        cost = (spreads["entry"].mean_pips + spreads["exit"].mean_pips) / 2
        cost = cost * plan.pip_size / entry * 10000
    return {"g": float(gross), "c": float(cost), "n": float(gross - cost)}, []


def oanda_return(slots: dict[str, Quote | None]) -> tuple[dict | None, list[str]]:
    absent = [slot for slot in ("entry", "exit") if slots[slot] is None]
    if absent:
        return None, absent
    entry, exit_ = slots["entry"], slots["exit"]
    gross = (exit_.mid - entry.mid) / entry.mid * 10000
    net = (exit_.bid - entry.ask) / entry.mid * 10000
    return {"g": float(gross), "c": float(gross - net), "n": float(net)}, []


def bootstrap(
    observations: Sequence[tuple[date, float]], period: Period, plan: Plan, name: str,
    *, circular: bool = False,
) -> dict:
    grouped = defaultdict(list)
    for day, value in observations:
        grouped[month_start(day)].append(value)
    units = months(period) if circular else sorted(grouped)
    aggregates = [(sum(grouped[m]), len(grouped[m])) for m in units]
    rng = random.Random(f"{plan.bootstrap_seed}:{name}")
    values, undefined = [], 0
    for _ in range(plan.bootstrap_samples):
        if not observations:
            undefined += 1
            continue
        if circular:
            indices = []
            while len(indices) < len(units):
                start = rng.randrange(len(units))
                indices.extend((start + j) % len(units) for j in range(plan.secondary_block_months))
            selected = [aggregates[i] for i in indices[:len(units)]]
        else:
            selected = [aggregates[rng.randrange(len(units))] for _ in units]
        count = sum(n for _, n in selected)
        if count:
            values.append(sum(total for total, _ in selected) / count)
        else:
            undefined += 1
    # 事象ゼロの抽出は平均が未定義。除いて別の分布の区間を作らない。
    return {"lower": None if undefined else percentile(values, 1 - plan.one_sided_level),
            "upper": None if undefined else percentile(values, plan.one_sided_level),
            "samples": plan.bootstrap_samples, "undefined_samples": undefined,
            "unit_months": len(units)}


def statistics(rows: Sequence[dict], period: Period, plan: Plan, name: str, trade: str) -> dict:
    valid = [row for row in rows if row[trade] is not None]
    net = [row[trade]["n"] for row in valid]
    ordered = sorted(net)
    trim = int(len(net) * plan.trim_fraction)
    trimmed = ordered[trim:len(ordered) - trim]
    sd = stdev(net) if len(net) > 1 else None
    years = ((period.end - period.start).days + 1) / plan.annualization_days
    observations = [(date.fromisoformat(row["date"]), row[trade]["n"]) for row in valid]
    return {
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
        "days": len(valid), "excluded_days": len(rows) - len(valid),
        "excluded_dates": [row["date"] for row in rows if row[trade] is None],
        "mean_g": fmean(row[trade]["g"] for row in valid) if valid else None,
        "mean_c": fmean(row[trade]["c"] for row in valid) if valid else None,
        "mean_n": fmean(net) if net else None, "std_n": sd,
        "trimmed_mean_n": fmean(trimmed) if trimmed else None,
        "annual_sharpe": fmean(net) / sd * math.sqrt(len(net) / years) if sd else None,
        "primary_interval": bootstrap(observations, period, plan, name + ":primary"),
        "secondary_interval": bootstrap(observations, period, plan, name + ":secondary",
                                        circular=True),
    }


def decide(main: dict, oanda: dict, plan: Plan) -> dict:
    lower = main["primary_interval"]["lower"]
    upper = main["primary_interval"]["upper"]
    oanda_upper = oanda["primary_interval"]["upper"]
    support = lower is not None and lower > 0 and oanda["mean_n"] is not None and oanda["mean_n"] > 0
    reject = any(v is not None and v < plan.reject_threshold_bp for v in (upper, oanda_upper))
    return {"verdict": "棄却" if reject else "支持" if support else "判定不能",
            "support": support, "reject": reject}


def measure(plan: Plan, directory: Path, manifest: Manifest, holidays: set[date], q: QuoteFile) -> dict:
    gotobi = gotobi_dates(plan.fetch_range, holidays, plan)
    daily = []
    for day in business_days(plan.fetch_range, holidays, plan):
        ticks = []
        if day in manifest.files:
            payload = (directory / "dukascopy" / f"{day}.bi5").read_bytes()
            if digest(payload) != manifest.files[day].sha256:
                raise ValueError(f"{day}: 測定時の bi5 の sha256 が一致しません")
            ticks = decode_day(payload, day, plan)
        price = prices(ticks, day, plan)
        main, main_absent = returns(price, q.spreads, plan)
        paper, paper_absent = returns(price, q.spreads, plan, paper=True)
        row = {"date": day.isoformat(), "gotobi": day in gotobi,
               "prices": {slot: str(value) if value is not None else None
                          for slot, value in price.items()}, "main": main, "paper": paper,
               "excluded_reasons": {"main": main_absent, "paper": paper_absent}}
        if day in q.quotes:
            oanda, absent = oanda_return(q.quotes[day])
            row.update(oanda=oanda)
            row["excluded_reasons"]["oanda"] = absent
        daily.append(row)

    def select(period: Period, is_gotobi: bool = True) -> list[dict]:
        return [row for row in daily if period.start.isoformat() <= row["date"] <= period.end.isoformat()
                and row["gotobi"] == is_gotobi]

    groups = {}
    for name, period, is_gotobi, trade in (
        ("main_gotobi", plan.main, True, "main"),
        ("oanda_gotobi", plan.oanda, True, "oanda"),
        ("main_other", plan.main, False, "main"),
        ("post_gotobi", plan.post, True, "main"),
        ("calibration_gotobi", plan.calibration, True, "main"),
        ("calibration_other", plan.calibration, False, "main"),
        ("paper_main_gotobi", plan.main, True, "paper"),
        ("paper_calibration_gotobi", plan.calibration, True, "paper"),
    ):
        groups[name] = statistics(select(period, is_gotobi), period, plan, name, trade)
    main_rows = [row for row in select(plan.main) if row["main"] is not None]
    sensitivity = []
    for multiplier in plan.cost_multipliers:
        observations = [(date.fromisoformat(row["date"]), row["main"]["g"] - multiplier * row["main"]["c"])
                        for row in main_rows]
        name = f"cost:{multiplier}"
        sensitivity.append({"multiplier": multiplier,
                            "mean_n": fmean(v for _, v in observations) if observations else None,
                            "primary_interval": bootstrap(observations, plan.main, plan, name + ":primary"),
                            "secondary_interval": bootstrap(observations, plan.main, plan, name + ":secondary",
                                                            circular=True)})
    annual = []
    for year in range(plan.main.start.year, plan.main.end.year + 1):
        values = [row["main"]["n"] for row in main_rows if row["date"].startswith(str(year))]
        annual.append({"year": year, "days": len(values), "mean_n": fmean(values) if values else None})
    extremes = sorted(main_rows, key=lambda row: row["main"]["n"])
    paired = [row for row in select(plan.oanda) if row["oanda"] is not None and row["main"] is not None]
    differences = [row["main"]["g"] - row["oanda"]["g"] for row in paired]
    main_mean, other_mean = groups["main_gotobi"]["mean_n"], groups["main_other"]["mean_n"]
    return {
        "decision": decide(groups["main_gotobi"], groups["oanda_gotobi"], plan),
        "groups": groups, "gotobi_minus_other_mean_n": (
            main_mean - other_mean if main_mean is not None and other_mean is not None else None),
        "cost_sensitivity": sensitivity, "annual": annual,
        "worst_days": [{"date": row["date"], **row["main"]} for row in extremes[:plan.extreme_days]],
        "best_days": [{"date": row["date"], **row["main"]} for row in reversed(extremes[-plan.extreme_days:])],
        "broker_difference": {"days": len(paired),
                              "mean_bp": fmean(differences) if differences else None,
                              "std_bp": stdev(differences) if len(differences) > 1 else None,
                              "excluded_dates": [row["date"] for row in select(plan.oanda)
                                                 if row["oanda"] is not None and row["main"] is None]},
        "daily": daily,
    }


def render_markdown(report: dict) -> str:
    def number(value: float | None) -> str:
        return "—" if value is None else f"{value:.3f}"

    lines = ["# H10 仲値前の研究", "", f"判定: **{report['decision']['verdict']}**", "",
             "単位は bp。主区間は月単位、副区間は循環ブロックの片側区間。", "",
             "| 期間・日・取引 | 日数 | 除外 | 粗利 | コスト | 純利益 | 主区間 下限 | 主区間 上限 | 副区間 下限 | 副区間 上限 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    labels = {"main_gotobi": "主・五十日", "oanda_gotobi": "OANDA・五十日", "main_other": "主・五十日以外",
              "post_gotobi": "公開後・五十日", "calibration_gotobi": "見当付け・五十日",
              "calibration_other": "見当付け・五十日以外", "paper_main_gotobi": "論文の形・主・五十日",
              "paper_calibration_gotobi": "論文の形・見当付け・五十日"}
    for name, group in report["groups"].items():
        primary, secondary = group["primary_interval"], group["secondary_interval"]
        lines.append(f"| {labels[name]} | {group['days']} | {group['excluded_days']} | {number(group['mean_g'])} | "
                     f"{number(group['mean_c'])} | {number(group['mean_n'])} | "
                     f"{number(primary['lower'])} | {number(primary['upper'])} | "
                     f"{number(secondary['lower'])} | {number(secondary['upper'])} |")
    lines += ["", "## コスト感応度", "", "| 倍率 | 平均純利益 | 主区間 下限 | 主区間 上限 |",
              "| ---: | ---: | ---: | ---: |"]
    for item in report["cost_sensitivity"]:
        interval = item["primary_interval"]
        lines.append(f"| {number(item['multiplier'])} | {number(item['mean_n'])} | "
                     f"{number(interval['lower'])} | {number(interval['upper'])} |")
    lines += ["", "## 年ごとの純利益", "", "| 年 | 日数 | 平均純利益 |", "| ---: | ---: | ---: |"]
    lines += [f"| {item['year']} | {item['days']} | {number(item['mean_n'])} |" for item in report["annual"]]
    lines += ["", f"五十日とそれ以外の平均の差: {number(report['gotobi_minus_other_mean_n'])} bp。",
              "", "価格・除外日・標準偏差・Sharpe・刈り込み平均・最良最悪日・業者間の差は report.json に収録。",
              "区間の抽出に事象ゼロが含まれた場合は区間を null とし、未定義の抽出回数を記録する。",
              "", "## 入力とコードの来歴", "", "```json",
              json_bytes(report["provenance"]).decode().rstrip(), "```", ""]
    return "\n".join(lines)


def measure_files(plan_path: Path, data_dir: Path, quote_path: Path, output: Path) -> dict:
    plan, plan_hash = read_plan(plan_path)
    manifest, holidays, manifest_hash = load_data(plan, plan_hash, data_dir)
    quotes, quote_hash = load_quotes(quote_path, plan, holidays, plan_hash, manifest_hash)
    if output.exists():
        raise FileExistsError(f"出力先が既にあります: {output}")
    report = measure(plan, data_dir, manifest, holidays, quotes)
    report.update(plan=plan.model_dump(mode="json"), provenance={
        "plan_sha256": plan_hash, "manifest_sha256": manifest_hash,
        "quotes_sha256": quote_hash, **git_state(),
    })
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "report.json", report)
    with (output / "report.md").open("x", encoding="utf-8") as target:
        target.write(render_markdown(report))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (("fetch", "祝日の CSV と Dukascopy を再開可能な形で固定"),
                              ("quotes", "研究用 DB の OANDA 気配とスプレッドを固定"),
                              ("measure", "固定ファイルから統計量と判定を出力")):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--plan", type=Path, required=True)
        if name != "fetch":
            command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--output" if name == "quotes" else "--output-dir", type=Path, required=True)
        if name == "measure":
            command.add_argument("--quotes", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch(args.plan, args.output_dir)
    elif args.command == "measure":
        measure_files(args.plan, args.data_dir, args.quotes, args.output_dir)
    else:
        plan, plan_hash = read_plan(args.plan)
        _, holidays, manifest_hash = load_data(plan, plan_hash, args.data_dir)
        if args.output.exists():
            raise FileExistsError(f"出力先が既にあります: {args.output}")
        import psycopg

        with psycopg.connect(os.environ["TRADING_DB_DSN"],
                             options="-c default_transaction_read_only=on") as conn:
            result = quotes_from_db(conn, plan, holidays, plan_hash, manifest_hash)
        write_json(args.output, result.model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
