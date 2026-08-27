"""GBP/EUR official collectors: parsing, PIT mapping, composition drift.

All payloads are fictional test data shaped like the real API responses.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import FakeTransport, FixedClock
from trading.data.macro.boe import BOECollector
from trading.data.macro.ecb import ECBCollector
from trading.data.macro.eurostat import EurostatCollector
from trading.data.macro.ons import ONSCollector
from trading.data.macro.registry import (
    EA_DEPOSIT_FACILITY_RATE,
    EA_HICP_HEADLINE_YOY_NSA,
    EA_REAL_GDP_GROWTH_QOQ_SCA,
    EA_UNEMPLOYMENT_RATE_SA,
    EA_YIELD_CURVE_2Y,
    INDICATORS,
    PIT_UNVERIFIED,
    PIT_VERIFIED,
    UK_BANK_RATE,
    UK_CPI_HEADLINE_YOY_NSA,
    UK_OIS_2Y,
    UK_REAL_GDP_GROWTH_QOQ_SA,
    UK_UNEMPLOYMENT_RATE_SA,
    US_CPI_HEADLINE_SA,
)

RETRIEVED = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)
YEARS = [2025, 2026]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_gbp_eur_series_are_pit_unverified():
    for series in (
        UK_BANK_RATE,
        UK_CPI_HEADLINE_YOY_NSA,
        UK_UNEMPLOYMENT_RATE_SA,
        UK_REAL_GDP_GROWTH_QOQ_SA,
        UK_OIS_2Y,
        EA_DEPOSIT_FACILITY_RATE,
        EA_HICP_HEADLINE_YOY_NSA,
        EA_UNEMPLOYMENT_RATE_SA,
        EA_REAL_GDP_GROWTH_QOQ_SCA,
        EA_YIELD_CURVE_2Y,
    ):
        assert INDICATORS[series].pit_classification == PIT_UNVERIFIED
    assert INDICATORS[US_CPI_HEADLINE_SA].pit_classification == PIT_VERIFIED


def test_release_instant_follows_london_dst():
    spec = INDICATORS[UK_CPI_HEADLINE_YOY_NSA]
    # 8月: BST = UTC+1 なので 07:00 ロンドンは 06:00Z。
    assert spec.release_instant(date(2026, 8, 19)) == datetime(
        2026, 8, 19, 6, 0, tzinfo=UTC
    )
    # 1月: GMT = UTC なので 07:00Z。
    assert spec.release_instant(date(2026, 1, 21)) == datetime(
        2026, 1, 21, 7, 0, tzinfo=UTC
    )


def test_release_instant_follows_brussels_dst():
    spec = INDICATORS[EA_HICP_HEADLINE_YOY_NSA]
    # 8月: CEST = UTC+2 なので 11:00 ブリュッセルは 09:00Z。
    assert spec.release_instant(date(2026, 8, 19)) == datetime(
        2026, 8, 19, 9, 0, tzinfo=UTC
    )
    # 1月: CET = UTC+1 なので 10:00Z。
    assert spec.release_instant(date(2026, 1, 21)) == datetime(
        2026, 1, 21, 10, 0, tzinfo=UTC
    )


# ---------------------------------------------------------------------------
# BOE
# ---------------------------------------------------------------------------


def test_boe_parses_iadb_csv():
    csv_bytes = b"DATE,IUDBEDR\r\n02 Jan 2026,3.75\r\n05 Jan 2026,4\r\n"
    transport = FakeTransport([csv_bytes])
    batch = BOECollector(transport, clock=FixedClock(RETRIEVED)).collect(YEARS)

    assert [o.observation_period for o in batch.observations] == [
        "2026-01-02",
        "2026-01-05",
    ]
    first = batch.observations[0]
    assert first.series == UK_BANK_RATE
    assert first.value == Decimal("3.75")
    assert first.unit == "percent"
    assert first.source == "BOE"
    assert first.known_at == RETRIEVED

    url = transport.byte_calls[0]
    assert "Datefrom=01%2FJan%2F2025" in url
    assert "Dateto=15%2FAug%2F2026" in url
    assert batch.raw_events[0].payload == {"csv": csv_bytes.decode()}


def test_boe_stamps_known_at_after_the_fetch():
    class TickingClock:
        """now() を呼ぶたび 1 分進む。取得に時間が掛かる状況の再現。"""

        def __init__(self, start: datetime) -> None:
            self._now = start

        def now(self) -> datetime:
            self._now += timedelta(minutes=1)
            return self._now

    transport = FakeTransport([b"DATE,IUDBEDR\r\n02 Jan 2026,3.75\r\n"])
    batch = BOECollector(transport, clock=TickingClock(RETRIEVED)).collect(YEARS)

    # 1 回目はクエリの Dateto、2 回目が取得完了時刻。取得前の時刻を known_at
    # にすると、その間に置いた replay clock からまだ受け取っていない値が
    # 見えてしまう。
    assert batch.observations[0].known_at == RETRIEVED + timedelta(minutes=2)
    assert batch.raw_events[0].retrieved_at == RETRIEVED + timedelta(minutes=2)


def test_boe_rejects_html_response():
    # 不正クエリへの IADB の応答は HTTP 200 の HTML ページ。
    transport = FakeTransport([b"<!DOCTYPE html><html>Data Series</html>"])
    with pytest.raises(ValueError, match="unexpected IADB response header"):
        BOECollector(transport, clock=FixedClock(RETRIEVED)).collect(YEARS)


def test_boe_rejects_header_without_rows():
    transport = FakeTransport([b"DATE,IUDBEDR\r\n"])
    with pytest.raises(ValueError, match="no observations"):
        BOECollector(transport, clock=FixedClock(RETRIEVED)).collect(YEARS)


# ---------------------------------------------------------------------------
# ONS
# ---------------------------------------------------------------------------


def test_ons_maps_months_filters_years_and_blanks():
    payload = {
        "months": [
            {
                "date": "2024 DEC",
                "value": "2.0",
                "year": "2024",
                "updateDate": "2026-01-20T00:00:00.000Z",
            },
            {
                "date": "2026 JUL",
                "value": "2.9",
                "year": "2026",
                "updateDate": "2026-08-18T23:00:00.000Z",
            },
            {"date": "2026 AUG", "value": "", "year": "2026"},
        ]
    }
    transport = FakeTransport([payload])
    batch = ONSCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        UK_CPI_HEADLINE_YOY_NSA, YEARS
    )

    (obs,) = batch.observations
    assert obs.observation_period == "2026-07"
    assert obs.value == Decimal("2.9")
    assert obs.unit == "percent"
    assert obs.published_at == datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
    assert obs.known_at == RETRIEVED
    url, _ = transport.get_calls[0]
    assert url.endswith("/timeseries/d7g7/mm23/data")
    assert batch.raw_events[0].payload == payload


def test_ons_maps_quarters():
    payload = {
        "quarters": [
            {
                "date": "2026 Q2",
                "value": "0.4",
                "year": "2026",
                "updateDate": "2026-08-12T23:00:00.000Z",
            }
        ]
    }
    transport = FakeTransport([payload])
    batch = ONSCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        UK_REAL_GDP_GROWTH_QOQ_SA, YEARS
    )
    (obs,) = batch.observations
    assert obs.observation_period == "2026Q2"
    assert obs.value == Decimal("0.4")


def test_ons_empty_rows_raise():
    transport = FakeTransport([{"months": []}])
    with pytest.raises(ValueError, match="no months rows"):
        ONSCollector(transport, clock=FixedClock(RETRIEVED)).collect(
            UK_CPI_HEADLINE_YOY_NSA, YEARS
        )


# ---------------------------------------------------------------------------
# ECB
# ---------------------------------------------------------------------------


def _ecb_payload(series: dict, times: list[str]) -> dict:
    return {
        "dataSets": [{"series": series}],
        "structure": {
            "dimensions": {
                "observation": [
                    {"id": "TIME_PERIOD", "values": [{"id": t} for t in times]}
                ]
            }
        },
    }


def test_ecb_parses_daily_levels_and_skips_nulls():
    payload = _ecb_payload(
        {
            "0:0:0:0:0:0:0": {
                "observations": {
                    "0": [2.0, 0, 0, None, None],
                    "1": [None],
                    "2": [2.25, 0, 0, None, None],
                }
            }
        },
        ["2026-06-16", "2026-06-17", "2026-06-18"],
    )
    transport = FakeTransport([payload])
    batch = ECBCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        EA_DEPOSIT_FACILITY_RATE, YEARS
    )

    assert [(o.observation_period, o.value) for o in batch.observations] == [
        ("2026-06-16", Decimal("2.0")),
        ("2026-06-18", Decimal("2.25")),
    ]
    assert batch.observations[0].known_at == RETRIEVED
    _, params = transport.get_calls[0]
    assert params == {"format": "jsondata", "startPeriod": "2025-01-01"}


def test_ecb_requires_exactly_one_series():
    payload = _ecb_payload(
        {
            "0:0:0:0:0:0:0": {"observations": {}},
            "0:0:0:0:0:1:0": {"observations": {}},
        },
        [],
    )
    transport = FakeTransport([payload])
    with pytest.raises(ValueError, match="exactly one ECB series"):
        ECBCollector(transport, clock=FixedClock(RETRIEVED)).collect(
            EA_DEPOSIT_FACILITY_RATE, YEARS
        )


def test_ecb_missing_datasets_raise():
    transport = FakeTransport([{"dataSets": []}])
    with pytest.raises(ValueError, match="no dataSets"):
        ECBCollector(transport, clock=FixedClock(RETRIEVED)).collect(
            EA_DEPOSIT_FACILITY_RATE, YEARS
        )


# ---------------------------------------------------------------------------
# Eurostat
# ---------------------------------------------------------------------------


def _jsonstat(
    *,
    geo_index: dict[str, int],
    time_index: dict[str, int],
    values: dict[str, float],
    unit_index: dict[str, int] | None = None,
) -> dict:
    unit_index = unit_index or {"PC": 0}
    return {
        "id": ["freq", "unit", "geo", "time"],
        "size": [1, len(unit_index), len(geo_index), len(time_index)],
        "dimension": {
            "freq": {"category": {"index": {"Q": 0}}},
            "unit": {"category": {"index": unit_index}},
            "geo": {"category": {"index": geo_index}},
            "time": {"category": {"index": time_index}},
        },
        "value": values,
    }


def test_eurostat_prefers_newest_composition_per_period():
    # flat index = geo * len(time) + time。EA21/Q1=0, EA21/Q2=1, EA20/Q1=2。
    payload = _jsonstat(
        geo_index={"EA21": 0, "EA20": 1},
        time_index={"2026-Q1": 0, "2026-Q2": 1},
        values={"0": 0.1, "1": 0.4, "2": 0.2},
    )
    transport = FakeTransport([payload])
    batch = EurostatCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        EA_REAL_GDP_GROWTH_QOQ_SCA, YEARS
    )

    by_period = {o.observation_period: o.value for o in batch.observations}
    # Q1 は EA21 と EA20 の両方に値がある: 新しい構成 EA21 の 0.1 を採る。
    assert by_period == {"2026Q1": Decimal("0.1"), "2026Q2": Decimal("0.4")}
    _, params = transport.get_calls[0]
    assert params["geo"] == ["EA21", "EA20", "EA"]
    assert params["sinceTimePeriod"] == "2025"


def test_eurostat_takes_older_composition_when_newest_is_absent():
    # EA21 が dataset 未移行の期間は EA20 の値で埋まる（HICP の実測形）。
    payload = _jsonstat(
        geo_index={"EA20": 0},
        time_index={"2025-12": 0},
        values={"0": 2.0},
    )
    transport = FakeTransport([payload])
    batch = EurostatCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        EA_HICP_HEADLINE_YOY_NSA, YEARS
    )
    (obs,) = batch.observations
    assert obs.observation_period == "2025-12"
    assert obs.value == Decimal("2.0")


def test_eurostat_empty_values_raise():
    payload = _jsonstat(geo_index={}, time_index={}, values={})
    transport = FakeTransport([payload])
    with pytest.raises(ValueError, match="no observations"):
        EurostatCollector(transport, clock=FixedClock(RETRIEVED)).collect(
            EA_UNEMPLOYMENT_RATE_SA, YEARS
        )


def test_eurostat_unpinned_dimension_raises():
    payload = _jsonstat(
        geo_index={"EA21": 0},
        time_index={"2026-06": 0},
        values={"0": 6.3, "1": 6.4},
        unit_index={"PC_ACT": 0, "THS_PER": 1},
    )
    transport = FakeTransport([payload])
    with pytest.raises(ValueError, match="not pinned down"):
        EurostatCollector(transport, clock=FixedClock(RETRIEVED)).collect(
            EA_UNEMPLOYMENT_RATE_SA, YEARS
        )


def test_ecb_collects_the_yield_curve_two_year_spot():
    payload = _ecb_payload(
        {"0:0:0:0:0:0:0": {"observations": {"0": [2.7963537279, 0, 0, None, None]}}},
        ["2026-08-24"],
    )
    transport = FakeTransport([payload])

    batch = ECBCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        EA_YIELD_CURVE_2Y, YEARS
    )

    assert [(o.observation_period, o.value) for o in batch.observations] == [
        ("2026-08-24", Decimal("2.7963537279"))
    ]
    url, _ = transport.get_calls[0]
    # AAA ソブリンカーブ（G_N_A）の spot（SV_C_YM）2 年点。
    assert url.endswith("/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")
