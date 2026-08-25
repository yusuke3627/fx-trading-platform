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
from trading.data.policy.collector import main as collector_main
from trading.data.policy.features import latest_policy_score, us2y_features
from trading.data.policy.meetings import (
    PolicyMeeting,
    load_coverage,
    load_meetings,
    load_schedule,
)
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


def test_schedule_entries_never_reach_scoring(tmp_path):
    # 予定はリスク窓専用。load_meetings に混ざると日次 collector が 0 点の
    # プレースホルダを決定的 id で永続化し、後から実結果を転記しても
    # ON CONFLICT DO NOTHING で訂正されない。
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
meetings:
  - bank: BOJ
    decision_date: 2026-07-31
    statement_published_at: 2026-07-31T06:00:00+00:00
    verified: false
    source_uri: https://example.invalid
schedule:
  - bank: FED
    decision_date: 2026-09-16
    earliest_published_at: 2026-09-16T18:00:00+00:00
    latest_published_at: 2026-09-16T18:00:00+00:00
    source_uri: https://example.invalid
"""
    )

    meetings = load_meetings(path)
    schedule = load_schedule(path)

    assert [m.bank for m in meetings] == ["BOJ"]
    assert [s.bank for s in schedule] == ["FED"]


def test_a_meeting_in_both_sections_is_rejected(tmp_path):
    # meetings: へ移した後に schedule: から消し忘れた状態を黙って通さない。
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
meetings:
  - bank: FED
    decision_date: 2026-09-16
    statement_published_at: 2026-09-16T18:00:00+00:00
    verified: false
    source_uri: https://example.invalid
schedule:
  - bank: FED
    decision_date: 2026-09-16
    earliest_published_at: 2026-09-16T18:00:00+00:00
    latest_published_at: 2026-09-16T18:00:00+00:00
    source_uri: https://example.invalid
"""
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_schedule(path)


def test_results_transcribed_onto_a_schedule_entry_fail_loudly(tmp_path):
    # 結果を schedule: に書き足して meetings: へ移し忘れると、黙って採点から
    # 漏れ続ける。未知フィールドは拒否して移行漏れをその場で検出する。
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
schedule:
  - bank: FED
    decision_date: 2026-09-16
    earliest_published_at: 2026-09-16T18:00:00+00:00
    latest_published_at: 2026-09-16T18:00:00+00:00
    rate_change_bp: 25
    verified: true
    source_uri: https://example.invalid
"""
    )
    with pytest.raises(ValueError, match="rate_change_bp"):
        load_schedule(path)


def test_a_misspelled_section_name_is_rejected(tmp_path):
    # scheudle: の誤記が空の schedule として通ると、covers の主張だけが残り、
    # 登録したはずの会合期間が NORMAL と判定される。
    path = tmp_path / "meetings.yaml"
    path.write_text("meetings: []\nscheudle: []\n")

    with pytest.raises(ValueError, match="scheudle"):
        load_schedule(path)


def test_the_collector_fails_when_a_past_meeting_was_never_transcribed(
    tmp_path, monkeypatch
):
    # 公表期限を過ぎた会合が schedule: に残ったままだと、実結果は存在するのに
    # 採点イベントが永続的に欠落する。日次実行はそこで止まって知らせる。
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
schedule:
  - bank: BOJ
    decision_date: 2026-06-16
    earliest_published_at: 2026-06-16T00:00:00+00:00
    latest_published_at: 2026-06-16T06:00:00+00:00
    source_uri: https://example.invalid
"""
    )
    monkeypatch.setattr(
        "sys.argv", ["collector", "--env", "demo", "--meetings", str(path)]
    )

    with pytest.raises(SystemExit, match="results not transcribed"):
        collector_main()


def test_the_collector_validates_the_whole_file_before_scoring(tmp_path, monkeypatch):
    # 日次 collector が meetings: しか読まないと、schedule: への書き足しは
    # 本番で一度も検証されず、その会合は正常終了の裏で採点され続けない。
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
schedule:
  - bank: FED
    decision_date: 2026-09-16
    earliest_published_at: 2026-09-16T18:00:00+00:00
    latest_published_at: 2026-09-16T18:00:00+00:00
    rate_change_bp: 25
    source_uri: https://example.invalid
"""
    )
    monkeypatch.setattr(
        "sys.argv", ["collector", "--env", "demo", "--meetings", str(path)]
    )

    with pytest.raises(ValueError, match="rate_change_bp"):
        collector_main()


def test_committed_schedule_loads():
    schedule = load_schedule("config/policy_meetings.yaml")
    assert all(s.source_uri.startswith("https://") for s in schedule)


def test_a_file_that_declares_no_coverage_claims_nothing(tmp_path):
    # Absent coverage is not "covers everything": risk falls back to its
    # configured default wherever the schedule is unstated.
    path = tmp_path / "meetings.yaml"
    path.write_text("meetings: []\n")

    assert load_coverage(path) is None


def test_coverage_bounds_must_be_ordered(tmp_path):
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
covers:
  since: 2026-08-01T00:00:00+00:00
  until: 2026-07-01T00:00:00+00:00
meetings: []
"""
    )
    with pytest.raises(ValueError, match="precede"):
        load_coverage(path)


def test_coverage_bounds_must_be_timezone_aware(tmp_path):
    path = tmp_path / "meetings.yaml"
    path.write_text(
        """
covers:
  since: 2026-07-01 00:00:00
  until: 2026-08-01T00:00:00+00:00
meetings: []
"""
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        load_coverage(path)


def test_the_committed_file_states_what_it_covers():
    # Without this the shipped calendar answers nothing anywhere, and every
    # configured halt would go unapplied while looking configured.
    coverage = load_coverage("config/policy_meetings.yaml")

    assert coverage is not None
    assert coverage.since < coverage.until


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
