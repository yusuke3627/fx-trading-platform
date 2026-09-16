"""系列の鮮度判定と収集後の失敗通知。応答はすべて架空データ。"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from tests.support import FakeTransport, FixedClock
from trading.data.macro import collector
from trading.data.macro.freshness import (
    StaleSeries,
    merge_latest_periods,
    period_end,
    stale_series,
)
from trading.data.macro.registry import (
    INDICATORS,
    UK_CPI_HEADLINE_YOY_NSA,
    UK_REAL_GDP_GROWTH_QOQ_SA,
    UK_UNEMPLOYMENT_RATE_SA,
    US_CPI_HEADLINE_SA,
    US_REAL_GDP_GROWTH_SAAR,
    US_TREASURY_2Y_YIELD,
)
from trading.domain.economic import EconomicObservation

RETRIEVED = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("2026-09-14", date(2026, 9, 14)),
        ("2026-07", date(2026, 7, 31)),
        ("2026-02", date(2026, 2, 28)),
        ("2024-02", date(2024, 2, 29)),
        ("2026-12", date(2026, 12, 31)),
        ("2026Q1", date(2026, 3, 31)),
        ("2026Q2", date(2026, 6, 30)),
        ("2026Q3", date(2026, 9, 30)),
        ("2026Q4", date(2026, 12, 31)),
    ],
)
def test_period_end(period: str, expected: date) -> None:
    assert period_end(period) == expected


@pytest.mark.parametrize(
    ("series", "period", "end", "limit_days"),
    [
        (US_TREASURY_2Y_YIELD, "2026-09-14", datetime(2026, 9, 14, tzinfo=UTC), 21),
        (US_CPI_HEADLINE_SA, "2026-07", datetime(2026, 7, 31, tzinfo=UTC), 75),
        (US_REAL_GDP_GROWTH_SAAR, "2026Q2", datetime(2026, 6, 30, tzinfo=UTC), 180),
        (UK_UNEMPLOYMENT_RATE_SA, "2026-05", datetime(2026, 5, 31, tzinfo=UTC), 130),
    ],
)
def test_stale_series_requires_age_to_exceed_limit(
    series: str, period: str, end: datetime, limit_days: int
) -> None:
    clock = FixedClock(end + timedelta(days=limit_days))
    latest = {series: period}

    assert stale_series(latest, clock.now()) == ()

    clock.advance(days=1)
    assert stale_series(latest, clock.now()) == (
        StaleSeries(
            series=series,
            latest_period=period,
            age_days=limit_days + 1,
            limit_days=limit_days,
        ),
    )


def test_unemployment_override_allows_106_days_but_other_monthly_series_does_not() -> None:
    clock = FixedClock(datetime(2026, 9, 14, tzinfo=UTC))
    latest = {
        UK_UNEMPLOYMENT_RATE_SA: "2026-05",
        UK_CPI_HEADLINE_YOY_NSA: "2026-05",
    }

    assert stale_series(latest, clock.now()) == (
        StaleSeries(
            series=UK_CPI_HEADLINE_YOY_NSA,
            latest_period="2026-05",
            age_days=106,
            limit_days=75,
        ),
    )


def test_stale_series_are_sorted_by_series_name() -> None:
    latest = {
        US_TREASURY_2Y_YIELD: "2026-07-31",
        US_CPI_HEADLINE_SA: "2026-05",
        UK_CPI_HEADLINE_YOY_NSA: "2026-05",
    }

    assert [item.series for item in stale_series(latest, FixedClock(RETRIEVED).now())] == [
        UK_CPI_HEADLINE_YOY_NSA,
        US_CPI_HEADLINE_SA,
        US_TREASURY_2Y_YIELD,
    ]


def _observation(series: str, period: str) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series=series,
        observation_period=period,
        value=Decimal("1.25"),
        unit=INDICATORS[series].unit,
        source="TEST",
        retrieved_at=RETRIEVED,
        known_at=RETRIEVED,
    )


def test_merge_latest_periods_across_unordered_batches_preserves_inputs() -> None:
    initial = {US_CPI_HEADLINE_SA: "2026-07", UK_CPI_HEADLINE_YOY_NSA: "2026-06"}
    first_batch = (
        _observation(US_TREASURY_2Y_YIELD, "2026-09-14"),
        _observation(US_CPI_HEADLINE_SA, "2026-06"),
        _observation(US_REAL_GDP_GROWTH_SAAR, "2026Q2"),
        _observation(US_TREASURY_2Y_YIELD, "2026-09-01"),
        _observation(US_REAL_GDP_GROWTH_SAAR, "2026Q1"),
    )
    second_batch = (
        _observation(US_CPI_HEADLINE_SA, "2026-08"),
        _observation(US_TREASURY_2Y_YIELD, "2026-09-15"),
        _observation(US_REAL_GDP_GROWTH_SAAR, "2025Q4"),
        _observation(US_CPI_HEADLINE_SA, "2026-07"),
        _observation(US_TREASURY_2Y_YIELD, "2026-09-15"),
    )
    snapshots = [observation.model_dump() for observation in first_batch + second_batch]

    first = merge_latest_periods(initial, iter(first_batch))
    latest = merge_latest_periods(first, iter(second_batch))

    assert latest == {
        UK_CPI_HEADLINE_YOY_NSA: "2026-06",
        US_CPI_HEADLINE_SA: "2026-08",
        US_TREASURY_2Y_YIELD: "2026-09-15",
        US_REAL_GDP_GROWTH_SAAR: "2026Q2",
    }
    assert first == {
        UK_CPI_HEADLINE_YOY_NSA: "2026-06",
        US_CPI_HEADLINE_SA: "2026-07",
        US_TREASURY_2Y_YIELD: "2026-09-14",
        US_REAL_GDP_GROWTH_SAAR: "2026Q2",
    }
    assert initial == {US_CPI_HEADLINE_SA: "2026-07", UK_CPI_HEADLINE_YOY_NSA: "2026-06"}
    assert [observation.model_dump() for observation in first_batch + second_batch] == snapshots


def _patch_ons_run(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict],
    series: list[str],
    observation_repo: Mock,
) -> tuple[FakeTransport, Mock]:
    """collector.main() を ONS ソースで回すための差し替え一式。"""
    transport = FakeTransport(list(payloads))
    event_repo = Mock()
    postgres = SimpleNamespace(
        connect=Mock(return_value=object()),
        PostgresMacroObservationRepository=Mock(return_value=observation_repo),
        PostgresEventRepository=Mock(return_value=event_repo),
    )
    monkeypatch.setitem(sys.modules, "trading.storage.postgres", postgres)
    monkeypatch.setenv("TEST_FRESHNESS_DSN", "test-dsn")
    monkeypatch.setattr("trading.config.load_config", lambda _: SimpleNamespace(
        storage=SimpleNamespace(dsn_env="TEST_FRESHNESS_DSN"),
        macro_data=SimpleNamespace(),
    ))
    argv = ["collector", "--source", "ons"]
    for name in series:
        argv += ["--series", name]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(collector, "SystemClock", lambda: FixedClock(RETRIEVED))
    monkeypatch.setattr(collector, "HttpTransport", lambda: transport)
    return transport, event_repo


@pytest.mark.parametrize("new_per_batch", [0, 1])
@pytest.mark.parametrize(
    ("months", "is_stale"),
    [(("APR", "MAY"), True), (("JUN", "JUL"), False)],
)
def test_collector_checks_freshness_after_storing_all_batches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    months: tuple[str, str],
    is_stale: bool,
    new_per_batch: int,
) -> None:
    observation_repo = Mock()
    observation_repo.insert_many.return_value = new_per_batch
    transport, event_repo = _patch_ons_run(
        monkeypatch,
        [
            {"months": [{"date": f"2026 {month}", "value": "1.25", "year": "2026"}]}
            for month in months
        ],
        [UK_UNEMPLOYMENT_RATE_SA, UK_CPI_HEADLINE_YOY_NSA],
        observation_repo,
    )

    if is_stale:
        with pytest.raises(SystemExit) as error:
            collector.main()
        assert str(error.value) == (
            "ons: collection freshness check failed\n"
            "uk_cpi_headline_yoy_nsa: latest period 2026-05, age 109 days, limit 75 days\n"
            "uk_unemployment_rate_sa: latest period 2026-04, age 140 days, limit 130 days"
        )
    else:
        collector.main()

    assert capsys.readouterr().out == (
        f"ons: parsed 2 observations, stored {2 * new_per_batch} new\n"
    )
    assert len(transport.get_calls) == 2
    assert observation_repo.insert_many.call_count == 2
    assert event_repo.insert_raw_archive.call_count == 2
    stored_series = [
        call.args[0][0].series for call in observation_repo.insert_many.call_args_list
    ]
    assert stored_series == [UK_UNEMPLOYMENT_RATE_SA, UK_CPI_HEADLINE_YOY_NSA]


@pytest.mark.parametrize(
    ("cpi_month", "stale_detail"),
    [
        ("JUL", ""),
        ("MAY", "\nuk_cpi_headline_yoy_nsa: latest period 2026-05, age 109 days, limit 75 days"),
    ],
)
def test_collector_reports_series_outside_collection_window_after_all_batches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cpi_month: str,
    stale_detail: str,
) -> None:
    observation_repo = Mock()
    observation_repo.insert_many.side_effect = len
    transport, event_repo = _patch_ons_run(
        monkeypatch,
        [
            {"months": [{"date": "2024 DEC", "value": "1.25", "year": "2024"}]},
            {"months": [{"date": f"2026 {cpi_month}", "value": "1.25", "year": "2026"}]},
            {"quarters": [{"date": "2026 Q2", "value": "1.25", "year": "2026"}]},
        ],
        [UK_UNEMPLOYMENT_RATE_SA, UK_CPI_HEADLINE_YOY_NSA, UK_REAL_GDP_GROWTH_QOQ_SA],
        observation_repo,
    )

    with pytest.raises(SystemExit) as error:
        collector.main()

    assert len(transport.get_calls) == 3
    assert str(error.value) == (
        "ons: collection freshness check failed\n"
        f"no observations: {transport.get_calls[0][0]}{stale_detail}"
    )
    assert capsys.readouterr().out == "ons: parsed 2 observations, stored 2 new\n"
    assert observation_repo.insert_many.call_count == 3
    assert event_repo.insert_raw_archive.call_count == 3
    stored_batches = [call.args[0] for call in observation_repo.insert_many.call_args_list]
    assert stored_batches[0] == ()
    assert [batch[0].series for batch in stored_batches[1:]] == [
        UK_CPI_HEADLINE_YOY_NSA,
        UK_REAL_GDP_GROWTH_QOQ_SA,
    ]
    archived_uris = [
        call.args[0].source_uri for call in event_repo.insert_raw_archive.call_args_list
    ]
    assert archived_uris == [url for url, _ in transport.get_calls]
