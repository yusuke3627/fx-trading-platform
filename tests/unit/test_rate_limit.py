from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.support import T0
from trading.oms.rate_limit import RateLimitConfig, RateLimiter


def test_per_symbol_limit_is_five_requests_in_a_sliding_second():
    limiter = RateLimiter(RateLimitConfig())

    for _ in range(5):
        assert limiter.allows("USDJPY", market_entry=False, now=T0)
        limiter.record("USDJPY", market_entry=False, now=T0)

    assert not limiter.allows("USDJPY", market_entry=False, now=T0)
    assert limiter.allows("EURUSD", market_entry=False, now=T0)
    assert not limiter.allows(
        "USDJPY", market_entry=False, now=T0 + timedelta(milliseconds=999)
    )
    assert limiter.allows(
        "USDJPY", market_entry=False, now=T0 + timedelta(seconds=1)
    )


def test_market_entry_limit_is_shared_across_symbols_but_does_not_block_exits():
    limiter = RateLimiter(RateLimitConfig())
    limiter.record("USDJPY", market_entry=True, now=T0)

    assert not limiter.allows("EURUSD", market_entry=True, now=T0)
    assert limiter.allows("EURUSD", market_entry=False, now=T0)
    assert limiter.allows(
        "EURUSD", market_entry=True, now=T0 + timedelta(seconds=1)
    )


def test_symbol_window_discards_only_records_at_least_one_second_old():
    limiter = RateLimiter(RateLimitConfig())
    for _ in range(3):
        limiter.record("USDJPY", market_entry=False, now=T0)
    for _ in range(2):
        limiter.record(
            "USDJPY",
            market_entry=False,
            now=T0 + timedelta(milliseconds=500),
        )

    at_one_second = T0 + timedelta(seconds=1)
    assert limiter.allows("USDJPY", market_entry=False, now=at_one_second)
    for _ in range(3):
        limiter.record("USDJPY", market_entry=False, now=at_one_second)
    assert not limiter.allows("USDJPY", market_entry=False, now=at_one_second)
    assert limiter.allows(
        "USDJPY", market_entry=False, now=T0 + timedelta(seconds=1.5)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("per_symbol_requests_per_second", 0),
        ("per_symbol_requests_per_second", -1),
        ("market_entries_per_second", 0),
        ("market_entries_per_second", -1),
    ],
)
def test_rate_limits_must_be_positive(field, value):
    with pytest.raises(ValidationError):
        RateLimitConfig(**{field: value})
