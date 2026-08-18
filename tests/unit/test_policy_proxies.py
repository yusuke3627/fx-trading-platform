"""Policy proxies: mechanical scoring, meeting loading, US2Y features.

All meeting data here is fictional test data.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import FixedClock
from trading.data.macro.registry import (
    INDICATORS,
    US_TREASURY_2Y_YIELD,
    period_from_date,
)
from trading.data.policy.features import latest_policy_score, us2y_features
from trading.data.policy.meetings import PolicyMeeting, load_meetings
from trading.data.policy.scoring import (
    SCORING_VERSION,
    event_from_meeting,
    score_meeting,
)
from trading.domain.economic import EconomicObservation

T0 = datetime(2026, 8, 18, 22, 0, tzinfo=UTC)


def meeting(**overrides) -> PolicyMeeting:
    values = {
        "bank": "BOJ",
        "decision_date": date(2026, 7, 31),
        "statement_published_at": datetime(2026, 7, 31, 6, 0, tzinfo=UTC),
        "verified": True,
        "source_uri": "https://example.invalid/statement",
    }
    values.update(overrides)
    return PolicyMeeting(**values)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_hold_scores_zero():
    assert score_meeting(meeting()) == 0.0


def test_hike_and_cut_direction():
    assert score_meeting(meeting(rate_change_bp=15)) == 2.0
    assert score_meeting(meeting(rate_change_bp=-25)) == -2.0


def test_dissents_and_language_accumulate():
    scored = score_meeting(
        meeting(hawkish_dissents=2, explicit_future_hike_language=True)
    )
    assert scored == 1.5
    assert score_meeting(meeting(dovish_dissents=1, inflation_forecast_change=-1)) == -1.0


def test_score_is_clipped_to_declared_scale():
    # Hike + dissents + forecast + language would sum to 3.5 without the clip.
    scored = score_meeting(
        meeting(
            rate_change_bp=25,
            hawkish_dissents=1,
            inflation_forecast_change=1,
            explicit_future_hike_language=True,
        )
    )
    assert scored == 2.0
    assert score_meeting(meeting(rate_change_bp=-25, dovish_dissents=4)) == -2.0


def test_event_id_is_deterministic_and_known_at_is_publication():
    clock = FixedClock(T0)
    first = event_from_meeting(meeting(), clock)
    second = event_from_meeting(meeting(), clock)
    assert first.event_id == second.event_id
    assert first.event_type == "BOJ_POLICY_SHIFT_SCORE"
    assert first.known_at == datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    assert first.payload["scoring_version"] == SCORING_VERSION
    assert first.payload["score"] == 0.0


def test_fed_meeting_uses_fed_event_type():
    event = event_from_meeting(
        meeting(bank="FED", decision_date=date(2026, 7, 29), hawkish_dissents=3),
        FixedClock(T0),
    )
    assert event.event_type == "FED_POLICY_SHIFT_SCORE"
    assert event.payload["score"] == 1.5


# ---------------------------------------------------------------------------
# Meetings yaml
# ---------------------------------------------------------------------------


def test_load_meetings_rejects_naive_datetime(tmp_path):
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
meetings:
  - bank: BOJ
    decision_date: 2026-07-31
    statement_published_at: 2026-07-31 06:00:00
    verified: false
    source_uri: https://example.invalid
"""
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        load_meetings(path)


def test_load_meetings_rejects_duplicates(tmp_path):
    path = tmp_path / "meetings.yaml"
    entry = """
  - bank: BOJ
    decision_date: 2026-07-31
    statement_published_at: 2026-07-31T06:00:00+00:00
    verified: false
    source_uri: https://example.invalid
"""
    path.write_text(f"meetings:{entry}{entry}")
    with pytest.raises(ValueError, match="duplicate"):
        load_meetings(path)


def test_committed_seed_file_loads():
    meetings = load_meetings("config/policy_meetings.yaml")
    assert meetings, "seed file must contain at least one meeting"
    assert all(m.source_uri.startswith("https://") for m in meetings)


# ---------------------------------------------------------------------------
# US2Y features
# ---------------------------------------------------------------------------


def yield_observation(day: date, value: str, revised: bool = False) -> EconomicObservation:
    known = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=23)
    if revised:
        known += timedelta(days=1)
    return EconomicObservation(
        observation_id=uuid4(),
        series=US_TREASURY_2Y_YIELD,
        observation_period=day.isoformat(),
        value=Decimal(value),
        unit="percent",
        source="ALFRED",
        retrieved_at=known,
        known_at=known,
    )


def test_us2y_changes_use_business_day_offsets():
    days = [date(2026, 8, 3) + timedelta(days=i) for i in range(7)]
    values = ["3.50", "3.52", "3.55", "3.53", "3.60", "3.58", "3.66"]
    features = us2y_features(
        [yield_observation(d, v) for d, v in zip(days, values, strict=True)]
    )
    assert features["us2y_level"] == pytest.approx(3.66)
    assert features["us2y_change_1d"] == pytest.approx(0.08)
    assert features["us2y_change_5d"] == pytest.approx(0.14)  # 3.66 - 3.52
    assert features["us2y_zscore_20d"] is None  # window not filled


def test_us2y_missing_data_is_none_not_zero():
    features = us2y_features([])
    assert features == {
        "us2y_level": None,
        "us2y_change_1d": None,
        "us2y_change_5d": None,
        "us2y_zscore_20d": None,
    }
    single = us2y_features([yield_observation(date(2026, 8, 3), "3.50")])
    assert single["us2y_level"] == pytest.approx(3.50)
    assert single["us2y_change_1d"] is None


def test_us2y_revision_supersedes_earlier_vintage():
    day = date(2026, 8, 3)
    observations = [
        yield_observation(day, "3.50"),
        yield_observation(day, "3.51", revised=True),
        yield_observation(date(2026, 8, 4), "3.60"),
    ]
    features = us2y_features(observations)
    assert features["us2y_change_1d"] == pytest.approx(0.09)  # 3.60 - 3.51


def test_us2y_zscore_with_full_window():
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(20)]
    observations = [yield_observation(d, "3.50") for d in days[:-1]]
    observations.append(yield_observation(days[-1], "3.70"))
    zscore = us2y_features(observations)["us2y_zscore_20d"]
    assert zscore is not None
    assert zscore > 4  # single spike against a flat window

    flat = us2y_features([yield_observation(d, "3.50") for d in days])
    assert flat["us2y_zscore_20d"] is None  # no scale in a flat window


def test_latest_policy_score_picks_most_recent_visible():
    clock = FixedClock(T0)
    older = event_from_meeting(
        meeting(decision_date=date(2026, 6, 17),
                statement_published_at=datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
                rate_change_bp=25),
        clock,
    )
    newer = event_from_meeting(meeting(hawkish_dissents=1), clock)
    assert latest_policy_score([older, newer]) == 0.5
    assert latest_policy_score([older]) == 2.0
    assert latest_policy_score([]) is None


# ---------------------------------------------------------------------------
# Daily period support
# ---------------------------------------------------------------------------


def test_daily_period_format():
    assert period_from_date(date(2026, 8, 18), "daily") == "2026-08-18"
    spec = INDICATORS[US_TREASURY_2Y_YIELD]
    assert spec.frequency == "daily"
    # 18:00 ET conservative release time: 22:00Z in summer.
    assert spec.release_instant(date(2026, 8, 18)) == datetime(
        2026, 8, 18, 22, 0, tzinfo=UTC
    )
