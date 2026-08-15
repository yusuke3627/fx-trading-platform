"""Economic release collectors: PIT mapping, vintage known_at, parsing.

All payloads are fictional test data shaped like the real API responses.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.support import FixedClock
from trading.data.macro.alfred import AlfredCollector
from trading.data.macro.base import payload_hash
from trading.data.macro.bea import BEACollector
from trading.data.macro.bls import BLSCollector
from trading.data.macro.census import CensusCollector
from trading.data.macro.registry import (
    INDICATORS,
    US_CPI_HEADLINE_SA,
    US_REAL_GDP_GROWTH_SAAR,
    US_RETAIL_SALES_ADVANCE_SA,
    US_UNEMPLOYMENT_RATE_SA,
    period_from_date,
)
from trading.domain.economic import EconomicObservation

RETRIEVED = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict) -> object:
        self.get_calls.append((url, dict(params)))
        return self._responses.pop(0)

    def post_json(self, url: str, body: dict) -> object:
        self.post_calls.append((url, dict(body)))
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_release_instant_follows_us_dst():
    spec = INDICATORS[US_CPI_HEADLINE_SA]
    # August: ET = UTC-4, so 08:30 ET is 12:30Z.
    assert spec.release_instant(date(2026, 8, 12)) == datetime(
        2026, 8, 12, 12, 30, tzinfo=UTC
    )
    # January: ET = UTC-5, so 08:30 ET is 13:30Z.
    assert spec.release_instant(date(2026, 1, 13)) == datetime(
        2026, 1, 13, 13, 30, tzinfo=UTC
    )


def test_period_from_date_monthly_and_quarterly():
    assert period_from_date(date(2026, 7, 1), "monthly") == "2026-07"
    assert period_from_date(date(2026, 4, 1), "quarterly") == "2026Q2"
    assert period_from_date(date(2026, 10, 1), "quarterly") == "2026Q4"


def test_observation_period_format_is_validated():
    with pytest.raises(ValueError, match="observation_period"):
        EconomicObservation(
            observation_id="00000000-0000-0000-0000-000000000001",
            series=US_CPI_HEADLINE_SA,
            observation_period="2026/07",
            value=Decimal("321.5"),
            unit="index",
            source="ALFRED",
            retrieved_at=RETRIEVED,
            known_at=RETRIEVED,
        )


def test_payload_hash_is_key_order_independent():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# ALFRED
# ---------------------------------------------------------------------------


def _alfred_page(observations: list[dict], count: int) -> dict:
    return {"count": count, "observations": observations}


def test_alfred_maps_vintages_to_release_time_known_at():
    transport = FakeTransport(
        [
            _alfred_page(
                [
                    {
                        "realtime_start": "2026-08-12",
                        "realtime_end": "2026-09-10",
                        "date": "2026-07-01",
                        "value": "321.500",
                    },
                    {
                        "realtime_start": "2026-09-11",
                        "realtime_end": "9999-12-31",
                        "date": "2026-07-01",
                        "value": "321.700",
                    },
                    {
                        "realtime_start": "2026-08-12",
                        "realtime_end": "9999-12-31",
                        "date": "2026-08-01",
                        "value": ".",
                    },
                ],
                count=3,
            )
        ]
    )
    batch = AlfredCollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
        US_CPI_HEADLINE_SA
    )

    assert len(batch.observations) == 2  # "." is skipped
    first, revision = batch.observations
    assert first.series == US_CPI_HEADLINE_SA
    assert first.observation_period == "2026-07"
    assert first.value == Decimal("321.500")
    assert first.unit == "index"
    # Vintage date + 08:30 ET release time, not midnight.
    assert first.known_at == datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
    assert revision.value == Decimal("321.700")
    assert revision.known_at == datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    assert revision.known_at > first.known_at

    assert len(batch.raw_events) == 1
    assert batch.raw_events[0].payload_hash == payload_hash(batch.raw_events[0].payload)
    # Observations point back at the archived page they were parsed from.
    assert first.payload_hash == batch.raw_events[0].payload_hash


def test_alfred_paginates_until_count_reached():
    page1 = _alfred_page(
        [
            {
                "realtime_start": "2026-08-12",
                "realtime_end": "9999-12-31",
                "date": "2026-07-01",
                "value": "321.5",
            }
        ],
        count=2,
    )
    page2 = _alfred_page(
        [
            {
                "realtime_start": "2026-09-11",
                "realtime_end": "9999-12-31",
                "date": "2026-08-01",
                "value": "322.1",
            }
        ],
        count=2,
    )
    transport = FakeTransport([page1, page2])
    batch = AlfredCollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
        US_CPI_HEADLINE_SA
    )

    assert len(batch.observations) == 2
    assert len(batch.raw_events) == 2
    assert [call[1]["offset"] for call in transport.get_calls] == ["0", "1"]


def test_alfred_observation_start_is_forwarded():
    transport = FakeTransport([_alfred_page([], count=0)])
    AlfredCollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
        US_CPI_HEADLINE_SA, observation_start=date(2015, 1, 1)
    )
    assert transport.get_calls[0][1]["observation_start"] == "2015-01-01"


def test_alfred_malformed_response_raises():
    transport = FakeTransport([{"error_message": "Bad Request"}])
    with pytest.raises(ValueError, match="no observations"):
        AlfredCollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
            US_CPI_HEADLINE_SA
        )


# ---------------------------------------------------------------------------
# BLS
# ---------------------------------------------------------------------------


def _bls_payload() -> dict:
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CUSR0000SA0",
                    "data": [
                        {"year": "2026", "period": "M07", "value": "321.500"},
                        {"year": "2025", "period": "M13", "value": "310.000"},
                    ],
                },
                {
                    "seriesID": "LNS14000000",
                    "data": [{"year": "2026", "period": "M07", "value": "4.2"}],
                },
            ]
        },
    }


def test_bls_maps_monthly_series_and_skips_annual_average():
    transport = FakeTransport([_bls_payload()])
    batch = BLSCollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
        [US_CPI_HEADLINE_SA, US_UNEMPLOYMENT_RATE_SA], years=[2025, 2026]
    )

    by_series = {o.series: o for o in batch.observations}
    assert set(by_series) == {US_CPI_HEADLINE_SA, US_UNEMPLOYMENT_RATE_SA}  # M13 skipped
    cpi = by_series[US_CPI_HEADLINE_SA]
    assert cpi.observation_period == "2026-07"
    assert cpi.value == Decimal("321.500")
    # Forward collection: visibility starts at retrieval, never earlier.
    assert cpi.known_at == RETRIEVED
    assert cpi.retrieved_at == RETRIEVED

    assert transport.post_calls[0][1]["registrationkey"] == "test-key"
    assert transport.post_calls[0][1]["startyear"] == "2025"
    assert transport.post_calls[0][1]["endyear"] == "2026"


def test_bls_key_is_optional():
    transport = FakeTransport([_bls_payload()])
    BLSCollector(transport, None, clock=FixedClock(RETRIEVED)).collect(
        [US_CPI_HEADLINE_SA], years=[2026]
    )
    assert "registrationkey" not in transport.post_calls[0][1]


def test_bls_failure_status_raises():
    transport = FakeTransport(
        [{"status": "REQUEST_NOT_PROCESSED", "message": ["daily threshold reached"]}]
    )
    with pytest.raises(ValueError, match="BLS request failed"):
        BLSCollector(transport, None, clock=FixedClock(RETRIEVED)).collect(
            [US_CPI_HEADLINE_SA], years=[2026]
        )


# ---------------------------------------------------------------------------
# BEA
# ---------------------------------------------------------------------------


def test_bea_maps_gdp_line_only():
    payload = {
        "BEAAPI": {
            "Results": {
                "Data": [
                    {
                        "LineNumber": "1",
                        "SeriesCode": "A191RL1",
                        "TimePeriod": "2026Q1",
                        "DataValue": "3.0",
                    },
                    {
                        "LineNumber": "2",
                        "SeriesCode": "DPCERL1",
                        "TimePeriod": "2026Q1",
                        "DataValue": "2.1",
                    },
                ]
            }
        }
    }
    transport = FakeTransport([payload])
    batch = BEACollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(
        years=[2025, 2026]
    )

    assert len(batch.observations) == 1
    gdp = batch.observations[0]
    assert gdp.series == US_REAL_GDP_GROWTH_SAAR
    assert gdp.observation_period == "2026Q1"
    assert gdp.value == Decimal("3.0")
    assert gdp.known_at == RETRIEVED
    assert transport.get_calls[0][1]["Year"] == "2025,2026"


def test_bea_error_raises():
    payload = {"BEAAPI": {"Results": {"Error": {"APIErrorCode": "3"}}}}
    transport = FakeTransport([payload])
    with pytest.raises(ValueError, match="BEA request failed"):
        BEACollector(transport, "test-key", clock=FixedClock(RETRIEVED)).collect(years=[2026])


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


def test_census_parses_tabular_response():
    rows = [
        ["cell_value", "category_code", "time", "us"],
        ["605,690", "44X72", "2026-06", "1"],
        ["607123", "44X72", "2026-07", "1"],
    ]
    transport = FakeTransport([rows])
    batch = CensusCollector(transport, None, clock=FixedClock(RETRIEVED)).collect(
        years=[2026]
    )

    assert [o.observation_period for o in batch.observations] == ["2026-06", "2026-07"]
    assert batch.observations[0].value == Decimal(605690)
    assert batch.observations[0].series == US_RETAIL_SALES_ADVANCE_SA
    assert batch.observations[0].known_at == RETRIEVED
    # Array payload is archived wrapped in an object.
    assert batch.raw_events[0].payload == {"rows": rows}


def test_census_non_tabular_response_raises():
    transport = FakeTransport([{"error": "unsupported"}])
    with pytest.raises(ValueError, match="not tabular"):
        CensusCollector(transport, None, clock=FixedClock(RETRIEVED)).collect(years=[2026])
