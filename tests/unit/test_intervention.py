from uuid import uuid4

import pytest

from trading.intelligence.intervention import (
    InterventionRiskConfig,
    VerificationRegressionError,
    advance_verification,
    intervention_risk_score,
    verification_state_level,
)

from tests.support import T0


def test_forward_transition_creates_new_event():
    subject = uuid4()
    event = advance_verification(subject, "RUMOR", "MARKET_SUSPECTED", T0)
    assert event.subject_event_id == subject
    assert event.from_status == "RUMOR"
    assert event.to_status == "MARKET_SUSPECTED"
    assert event.known_at == T0


def test_skipping_forward_is_allowed():
    event = advance_verification(uuid4(), "RUMOR", "OFFICIAL_ACTION_CONFIRMED", T0)
    assert event.to_status == "OFFICIAL_ACTION_CONFIRMED"


def test_regression_is_rejected():
    with pytest.raises(VerificationRegressionError):
        advance_verification(uuid4(), "MEDIA_CONFIRMED", "RUMOR", T0)
    with pytest.raises(VerificationRegressionError):
        advance_verification(uuid4(), "RUMOR", "RUMOR", T0)


def test_initial_status_needs_no_predecessor():
    event = advance_verification(uuid4(), None, "RUMOR", T0)
    assert event.from_status is None


def test_risk_score_renormalizes_over_available_inputs():
    config = InterventionRiskConfig(
        version="test",
        weights={"realized_volatility": 0.5, "rate_of_change": 0.5},
    )
    assert intervention_risk_score(
        {"realized_volatility": 1.0, "rate_of_change": 1.0}, config
    ) == 1.0
    # Missing input reduces coverage but does not count as zero risk.
    assert intervention_risk_score({"realized_volatility": 1.0}, config) == 1.0
    assert intervention_risk_score({}, config) == 0.0


def test_risk_score_clips_inputs():
    config = InterventionRiskConfig(version="test", weights={"realized_volatility": 1.0})
    assert intervention_risk_score({"realized_volatility": 5.0}, config) == 1.0


def test_verification_state_level_is_monotonic():
    assert verification_state_level(None) == 0.0
    assert verification_state_level("RUMOR") == pytest.approx(0.2)
    assert verification_state_level("OFFICIAL_AMOUNT_CONFIRMED") == 1.0
