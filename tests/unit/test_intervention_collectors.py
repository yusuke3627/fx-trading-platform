"""Intervention PIT collectors and risk inputs.

All payloads are fictional test data shaped like the real MOF publications.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from tests.support import FixedClock
from trading.data.intervention.episodes import (
    event_from_recognition,
    load_episodes,
)
from trading.data.intervention.features import intervention_risk_inputs
from trading.data.intervention.mof import (
    MOFDailyCollector,
    MOFMonthlyCollector,
    daily_known_at,
    parse_amount_oku,
    parse_wareki_dates,
)
from trading.domain.event import EventEnvelope
from trading.intelligence.intervention import (
    InterventionRiskConfig,
    intervention_risk_score,
)

RETRIEVED = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self._responses = responses
        self.urls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.urls.append(url)
        return self._responses[url]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_amount_oku():
    assert parse_amount_oku("0円") == 0
    assert parse_amount_oku("9兆7,885億円") == 97_885
    assert parse_amount_oku("5,620億円") == 5_620
    assert parse_amount_oku("2兆円") == 20_000
    assert parse_amount_oku("　　9兆7,885億円 ") == 97_885
    with pytest.raises(ValueError, match="amount"):
        parse_amount_oku("非公表")


def test_parse_wareki_dates():
    dates = parse_wareki_dates("令和8年6月29日～令和8年7月29日")
    assert dates == [date(2026, 6, 29), date(2026, 7, 29)]
    assert parse_wareki_dates("平成3年5月13日") == [date(1991, 5, 13)]
    with pytest.raises(ValueError, match="wareki"):
        parse_wareki_dates("2026-06-29")


def test_daily_known_at_is_quarter_end_plus_lag_capped_at_fetch():
    late_fetch = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    # 2024-04-29 -> quarter end 2024-06-30 + 62d = 2024-08-31 19:00 JST.
    assert daily_known_at(date(2024, 4, 29), late_fetch) == datetime(
        2024, 8, 31, 10, 0, tzinfo=UTC
    )
    # Fresh row: the bound lies in the future, so the fetch time wins.
    fresh_fetch = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
    assert daily_known_at(date(2026, 5, 6), fresh_fetch) == fresh_fetch


# ---------------------------------------------------------------------------
# MOF daily CSV
# ---------------------------------------------------------------------------

DAILY_CSV = """財務省,,,,,,,,
実施年月日,,,Intervention Date,,,,,
年,月,日,Year,Month,Day,金額(amount),売買通貨,Currency pairs
,,,,,,,,
平成3年,5月,13日,1991,May,13,139,米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)
,6月,10日,,Jun,10,211,米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)
平成3年4〜6月期計,,,April - June 1991,,,350,,
令和6年,4月,29日,2024,Apr,29,"59,185",米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)
,,,,,,,,
(注1) 注記行,,,,,,,,
""".encode("shift_jis")

CSV_URL = (
    "https://www.mof.go.jp/policy/international_policy/reference/feio/"
    "foreign_exchange_intervention_operations.csv"
)


def test_mof_daily_parses_carry_forward_and_skips_subtotals():
    transport = FakeTransport({CSV_URL: DAILY_CSV})
    batch = MOFDailyCollector(transport, clock=FixedClock(RETRIEVED)).collect()

    assert [e.payload["action_date"] for e in batch.events] == [
        "1991-05-13",
        "1991-06-10",  # carried-forward year
        "2024-04-29",
    ]
    latest = batch.events[-1]
    assert latest.payload["amount_100m_yen"] == 59_185  # comma stripped
    assert latest.payload["direction"] == "JPY_BUY"
    assert latest.known_at == datetime(2024, 8, 31, 10, 0, tzinfo=UTC)
    # Raw CSV is archived alongside the parsed events.
    assert len(batch.raw_events) == 1
    assert "米ドル売り" in batch.raw_events[0].payload["csv"]


def test_mof_daily_event_ids_are_deterministic():
    transport = FakeTransport({CSV_URL: DAILY_CSV})
    collector = MOFDailyCollector(transport, clock=FixedClock(RETRIEVED))
    first = collector.collect()
    second = collector.collect()
    assert [e.event_id for e in first.events] == [e.event_id for e in second.events]


def test_mof_daily_empty_csv_raises():
    header_only = "年,月,日,Year,Month,Day,金額,売買通貨,Currency pairs".encode("shift_jis")
    transport = FakeTransport({CSV_URL: header_only})
    with pytest.raises(ValueError, match="no intervention rows"):
        MOFDailyCollector(transport, clock=FixedClock(RETRIEVED)).collect()


# ---------------------------------------------------------------------------
# MOF monthly pages
# ---------------------------------------------------------------------------

MONTHLY_BASE = (
    "https://www.mof.go.jp/policy/international_policy/reference/feio/data/monthly/"
)


def _monthly_page(amount_text: str) -> bytes:
    return (
        "<html><body><p>○期間における外国為替平衡操作額"
        f"<span>　　{amount_text}</span></p></body></html>"
    ).encode()


def test_mof_monthly_parses_amounts_and_publication_known_at():
    index = (
        '<li><a href="20260731.html">令和8年6月29日～令和8年7月29日</a></li>'
        '<li><a href="20240531.html">令和6年4月26日～令和6年5月29日</a></li>'
    ).encode()
    transport = FakeTransport(
        {
            f"{MONTHLY_BASE}index.html": index,
            f"{MONTHLY_BASE}20260731.html": _monthly_page("0円"),
            f"{MONTHLY_BASE}20240531.html": _monthly_page("9兆7,885億円"),
        }
    )
    batch = MOFMonthlyCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        published_since=date(2024, 1, 1)
    )

    by_pub = {e.source_uri.rsplit("/", 1)[-1]: e for e in batch.events}
    zero = by_pub["20260731.html"]
    assert zero.payload["total_100m_yen"] == 0  # zero months are information
    assert zero.payload["period_start"] == "2026-06-29"
    assert zero.payload["period_end"] == "2026-07-29"
    # Publication date 19:00 JST -> 10:00Z.
    assert zero.known_at == datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    assert by_pub["20240531.html"].payload["total_100m_yen"] == 97_885
    assert len(batch.raw_events) == 2  # pages archived


def test_mof_monthly_since_filter_skips_older_publications():
    index = (
        '<li><a href="20260731.html">令和8年6月29日～令和8年7月29日</a></li>'
        '<li><a href="20240531.html">令和6年4月26日～令和6年5月29日</a></li>'
    ).encode()
    transport = FakeTransport(
        {
            f"{MONTHLY_BASE}index.html": index,
            f"{MONTHLY_BASE}20260731.html": _monthly_page("0円"),
        }
    )
    batch = MOFMonthlyCollector(transport, clock=FixedClock(RETRIEVED)).collect(
        published_since=date(2026, 1, 1)
    )
    assert len(batch.events) == 1
    assert f"{MONTHLY_BASE}20240531.html" not in transport.urls


# ---------------------------------------------------------------------------
# Curated recognition timeline
# ---------------------------------------------------------------------------


def test_committed_episode_file_loads_and_maps():
    entries = load_episodes("config/intervention_episodes.yaml")
    assert entries, "seed file must contain at least one recognition"
    event = event_from_recognition(entries[0], FixedClock(RETRIEVED))
    assert event.event_type == "INTERVENTION_REPORTED"
    assert event.known_at == entries[0].known_at
    # The later official figure never leaks into the recognition layer.
    assert "amount_100m_yen" not in event.payload
    again = event_from_recognition(entries[0], FixedClock(RETRIEVED))
    assert event.event_id == again.event_id


def test_committed_government_confirmed_entries_map_to_official_action_events():
    entries = load_episodes("config/intervention_episodes.yaml")
    confirmed = [e for e in entries if e.kind == "GOVERNMENT_CONFIRMED"]
    assert {e.action_date for e in confirmed} == {date(2022, 9, 22), date(2026, 7, 31)}
    for entry in confirmed:
        event = event_from_recognition(entry, FixedClock(RETRIEVED))
        assert event.event_type == "INTERVENTION_GOVERNMENT_CONFIRMED"
        assert event.known_at == entry.known_at
        # 同じ action_date の REPORTED と event_id が衝突しない（kind がキーに含まれる）
        reported = next(
            e for e in entries if e.kind == "REPORTED" and e.action_date == entry.action_date
        )
        assert event.event_id != event_from_recognition(reported, FixedClock(RETRIEVED)).event_id
        assert event.event_id == event_from_recognition(entry, FixedClock(RETRIEVED)).event_id


def test_episode_loader_rejects_duplicates(tmp_path):
    entry = """
  - kind: REPORTED
    action_date: 2026-07-30
    known_at: 2026-07-30T14:59:00+00:00
    direction: JPY_BUY
    verified: false
    source_uri: https://example.invalid
"""
    path = tmp_path / "episodes.yaml"
    path.write_text(f"recognitions:{entry}{entry}")
    with pytest.raises(ValueError, match="duplicate"):
        load_episodes(path)


# ---------------------------------------------------------------------------
# Risk inputs
# ---------------------------------------------------------------------------


def _event(event_type: str, payload: dict, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source="TEST",
        payload=payload,
        retrieved_at=known_at,
        known_at=known_at,
    )


def test_risk_inputs_empty_without_recent_intervention():
    assert intervention_risk_inputs([], RETRIEVED) == {}
    old = _event(
        "INTERVENTION_REPORTED",
        {"action_date": "2024-07-12", "direction": "JPY_BUY", "verified": False},
        datetime(2024, 7, 12, 14, 59, tzinfo=UTC),
    )
    assert intervention_risk_inputs([old], RETRIEVED) == {}


def test_risk_inputs_decay_and_verification_stage():
    t = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)  # 9 days after 5/6
    reported = _event(
        "INTERVENTION_REPORTED",
        {"action_date": "2026-05-06", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 5, 6, 14, 59, tzinfo=UTC),
    )
    inputs = intervention_risk_inputs([reported], t)
    assert inputs["days_since_intervention"] == pytest.approx(1 - 9 / 90)
    assert inputs["verification_state"] == pytest.approx(0.6)  # MEDIA_CONFIRMED

    official = _event(
        "INTERVENTION_OFFICIAL_DAILY_AMOUNT",
        {"action_date": "2026-05-06", "amount_100m_yen": 46_759,
         "direction": "JPY_BUY", "pair": "fictional pair"},
        datetime(2026, 8, 10, 3, 0, tzinfo=UTC),
    )
    upgraded = intervention_risk_inputs([reported, official], datetime(2026, 8, 3, tzinfo=UTC))
    assert upgraded["verification_state"] == 1.0


def test_government_confirmation_raises_stage_without_moving_recency():
    reported = _event(
        "INTERVENTION_REPORTED",
        {"action_date": "2026-07-31", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 8, 1, 14, 59, tzinfo=UTC),
    )
    confirmed = _event(
        "INTERVENTION_GOVERNMENT_CONFIRMED",
        {"action_date": "2026-07-31", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
    )
    t = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    only_reported = intervention_risk_inputs([reported], t)
    both = intervention_risk_inputs([reported, confirmed], t)
    assert only_reported["verification_state"] == pytest.approx(0.6)  # MEDIA_CONFIRMED
    assert both["verification_state"] == pytest.approx(0.8)  # OFFICIAL_ACTION_CONFIRMED
    assert both["days_since_intervention"] == only_reported["days_since_intervention"]


def test_risk_inputs_use_monthly_period_end_only_when_positive():
    zero_month = _event(
        "INTERVENTION_OFFICIAL_MONTHLY_AMOUNT",
        {"period_start": "2026-06-29", "period_end": "2026-07-29", "total_100m_yen": 0},
        datetime(2026, 7, 31, 10, 0, tzinfo=UTC),
    )
    assert intervention_risk_inputs([zero_month], datetime(2026, 8, 1, tzinfo=UTC)) == {}

    positive_month = _event(
        "INTERVENTION_OFFICIAL_MONTHLY_AMOUNT",
        {"period_start": "2026-06-29", "period_end": "2026-07-29",
         "total_100m_yen": 12_345},
        datetime(2026, 7, 31, 10, 0, tzinfo=UTC),
    )
    inputs = intervention_risk_inputs([positive_month], datetime(2026, 8, 1, tzinfo=UTC))
    assert inputs["days_since_intervention"] == pytest.approx(1 - 3 / 90)
    assert inputs["verification_state"] == 1.0


def test_risk_inputs_feed_existing_score():
    config = InterventionRiskConfig(
        version="test",
        weights={"days_since_intervention": 0.5, "verification_state": 0.5},
    )
    reported = _event(
        "INTERVENTION_REPORTED",
        {"action_date": "2026-05-06", "direction": "JPY_BUY", "verified": False},
        datetime(2026, 5, 6, 14, 59, tzinfo=UTC),
    )
    t = datetime(2026, 5, 7, 0, 0, tzinfo=UTC)
    score = intervention_risk_score(intervention_risk_inputs([reported], t), config)
    assert 0.7 < score < 1.0  # fresh, media-confirmed intervention
    assert intervention_risk_score(intervention_risk_inputs([], t), config) == 0.0
