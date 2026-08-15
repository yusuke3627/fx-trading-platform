"""Event payload boundary: JSON-native types only, so the JSONB round-trip
preserves types exactly between live and replay."""
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.support import T0
from trading.domain.event import EventEnvelope


def envelope(payload: dict) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="macro.test",
        source="test",
        payload=payload,
        retrieved_at=T0,
        known_at=T0,
    )


def test_json_native_payload_accepted():
    event = envelope({"value": 3.4, "series": "cpi", "revised": False, "n": 2})
    assert event.payload["value"] == 3.4


def test_decimal_payload_rejected_at_the_boundary():
    with pytest.raises(ValidationError):
        envelope({"amount": Decimal("158.84")})


def test_datetime_payload_rejected_at_the_boundary():
    with pytest.raises(ValidationError):
        envelope({"at": T0})


def test_nested_tuple_rejected_not_coerced_to_list():
    # json.dumps would silently turn the tuple into a list, so the DB reload
    # would no longer equal the live payload.
    with pytest.raises(ValidationError):
        envelope({"values": (1, 2)})


def test_nested_non_string_key_rejected_not_coerced():
    with pytest.raises(ValidationError):
        envelope({"nested": {1: "a"}})


def test_non_finite_float_rejected():
    # NaN/Infinity serialize by default but are not valid JSON for JSONB.
    with pytest.raises(ValidationError):
        envelope({"value": float("nan")})
