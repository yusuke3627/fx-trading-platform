"""MOF official intervention collectors.

Two publications, two event kinds:

- Historical CSV (1991-, Shift_JIS): one row per intervention day, published
  quarterly -> INTERVENTION_OFFICIAL_DAILY_AMOUNT.
- Monthly total pages (filename = publication date): the aggregate for the
  ~27th-to-26th window, published at month-end 19:00 JST ->
  INTERVENTION_OFFICIAL_MONTHLY_AMOUNT. Zero months are ingested too:
  "no intervention in the window" is information.

known_at for daily rows: the CSV does not carry publication dates, so the
vintage bound is quarter end + 62 days at 19:00 JST (observed actual: 2024Q2
breakdown published 2024-08-07, ~5.5 weeks after quarter end; 62 days keeps a
margin). A fresh collection knows the row at fetch time, so the bound is
capped at retrieved_at. Amounts are 100-million-yen integers as published.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import raw_event
from trading.domain.event import EventEnvelope

SOURCE_MOF = "MOF"
HISTORY_CSV_URL = (
    "https://www.mof.go.jp/policy/international_policy/reference/feio/"
    "foreign_exchange_intervention_operations.csv"
)
MONTHLY_INDEX_URL = (
    "https://www.mof.go.jp/policy/international_policy/reference/feio/"
    "data/monthly/index.html"
)

DAILY_EVENT_TYPE = "INTERVENTION_OFFICIAL_DAILY_AMOUNT"
MONTHLY_EVENT_TYPE = "INTERVENTION_OFFICIAL_MONTHLY_AMOUNT"

# Quarterly breakdown publication bound (see module docstring).
QUARTERLY_PUBLICATION_LAG = timedelta(days=62)
PUBLICATION_TIME_JST = time(19, 0)
_JST = ZoneInfo("Asia/Tokyo")

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# 令和8年6月29日 -> 2026-06-29. Only eras the data can contain.
_ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925}
_WAREKI = re.compile(r"(令和|平成|昭和)(\d+)年(\d+)月(\d+)日")

# 9兆7,885億円 / 5,620億円 / 0円
_AMOUNT = re.compile(r"^(?:(\d+)兆)?(?:([\d,]+)億)?(0)?円$")

_MONTHLY_LINK = re.compile(r'href="(\d{8})\.html"[^>]*>([^<]+)<')
_MONTHLY_AMOUNT_MARKER = "における外国為替平衡操作額"
_TAGS = re.compile(r"<[^>]+>")


class InterventionBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: tuple[EventEnvelope, ...]
    raw_events: tuple[EventEnvelope, ...]


def parse_wareki_dates(text: str) -> list[date]:
    dates = [
        date(_ERA_BASE[era] + int(year), int(month), int(day))
        for era, year, month, day in _WAREKI.findall(text)
    ]
    if not dates:
        raise ValueError(f"no wareki date in {text!r}")
    return dates


def parse_amount_oku(text: str) -> int:
    """Published amount -> 100-million-yen integer."""
    cleaned = text.replace("　", "").replace(" ", "").replace("\n", "").strip()
    match = _AMOUNT.match(cleaned)
    if match is None:
        raise ValueError(f"unrecognized amount format: {text!r}")
    cho, oku, zero = match.groups()
    if zero is not None and cho is None and oku is None:
        return 0
    total = int(cho or 0) * 10_000
    total += int((oku or "0").replace(",", ""))
    return total


def _direction(pair_en: str) -> str:
    if "yen (bought)" in pair_en:
        return "JPY_BUY"
    if "yen (sold)" in pair_en:
        return "JPY_SELL"
    return "OTHER"


def _quarter_end(action_date: date) -> date:
    quarter_end_month = ((action_date.month - 1) // 3 + 1) * 3
    next_month = date(
        action_date.year + quarter_end_month // 12, quarter_end_month % 12 + 1, 1
    )
    return next_month - timedelta(days=1)


def daily_known_at(action_date: date, retrieved_at: datetime) -> datetime:
    bound = datetime.combine(
        _quarter_end(action_date) + QUARTERLY_PUBLICATION_LAG,
        PUBLICATION_TIME_JST,
        _JST,
    ).astimezone(UTC)
    # A row we just fetched is known now even if the conservative bound lies
    # in the future; both sides stay at or after the true publication.
    return min(bound, retrieved_at)


class MOFDailyCollector:
    """Historical CSV -> one event per intervention day."""

    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self) -> InterventionBatch:
        raw = self._transport.get_bytes(HISTORY_CSV_URL)
        text = raw.decode("shift_jis")
        retrieved_at = self._clock.now()

        events: list[EventEnvelope] = []
        year: int | None = None
        month: int | None = None
        for cols in csv.reader(io.StringIO(text)):
            if len(cols) < 9:
                continue
            year_col = cols[3].strip()
            month_col = cols[4].strip()
            day_col = cols[5].strip()
            pair_en = cols[8].strip()
            if year_col.isdigit():
                year = int(year_col)
            if month_col[:3] in _MONTHS:
                month = _MONTHS[month_col[:3]]
            # Data rows have a day and a currency pair; header and quarterly
            # subtotal rows have neither.
            if not day_col.isdigit() or not pair_en:
                continue
            if year is None or month is None:
                raise ValueError(f"day row before year/month context: {cols!r}")
            action_date = date(year, month, int(day_col))
            amount = int(cols[6].strip().replace(",", ""))
            events.append(
                EventEnvelope(
                    event_id=uuid5(
                        NAMESPACE_URL,
                        f"intervention-daily:{action_date}:{pair_en}",
                    ),
                    event_type=DAILY_EVENT_TYPE,
                    source=SOURCE_MOF,
                    source_uri=HISTORY_CSV_URL,
                    payload={
                        "action_date": action_date.isoformat(),
                        "amount_100m_yen": amount,
                        "direction": _direction(pair_en),
                        "pair": pair_en,
                    },
                    effective_at=datetime.combine(action_date, time(0, 0), UTC),
                    retrieved_at=retrieved_at,
                    known_at=daily_known_at(action_date, retrieved_at),
                )
            )
        if not events:
            raise ValueError("MOF history CSV yielded no intervention rows")

        archive = raw_event(
            source=SOURCE_MOF,
            source_uri=HISTORY_CSV_URL,
            payload={"csv": text},
            retrieved_at=retrieved_at,
        )
        return InterventionBatch(events=tuple(events), raw_events=(archive,))


class MOFMonthlyCollector:
    """Monthly total pages -> one event per publication."""

    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, *, published_since: date) -> InterventionBatch:
        index_html = self._transport.get_bytes(MONTHLY_INDEX_URL).decode("utf-8")
        entries = _MONTHLY_LINK.findall(index_html)
        if not entries:
            raise ValueError("MOF monthly index page has no publication links")

        events: list[EventEnvelope] = []
        raw_events: list[EventEnvelope] = []
        for stamp, period_text in entries:
            published_on = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            if published_on < published_since:
                continue
            page_url = MONTHLY_INDEX_URL.replace("index.html", f"{stamp}.html")
            page = self._transport.get_bytes(page_url).decode("utf-8")
            retrieved_at = self._clock.now()
            raw_events.append(
                raw_event(
                    source=SOURCE_MOF,
                    source_uri=page_url,
                    payload={"html": page},
                    retrieved_at=retrieved_at,
                )
            )

            marker = page.rfind(_MONTHLY_AMOUNT_MARKER)
            if marker < 0:
                raise ValueError(f"no amount marker on {page_url}")
            segment = page[marker + len(_MONTHLY_AMOUNT_MARKER) : page.find("</p>", marker)]
            amount = parse_amount_oku(_TAGS.sub("", segment))

            period_dates = parse_wareki_dates(period_text)
            period_start, period_end = period_dates[0], period_dates[-1]

            known_at = datetime.combine(
                published_on, PUBLICATION_TIME_JST, _JST
            ).astimezone(UTC)
            events.append(
                EventEnvelope(
                    event_id=uuid5(NAMESPACE_URL, f"intervention-monthly:{published_on}"),
                    event_type=MONTHLY_EVENT_TYPE,
                    source=SOURCE_MOF,
                    source_uri=page_url,
                    payload={
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                        "total_100m_yen": amount,
                    },
                    published_at=known_at,
                    retrieved_at=retrieved_at,
                    known_at=known_at,
                )
            )
        return InterventionBatch(events=tuple(events), raw_events=tuple(raw_events))
