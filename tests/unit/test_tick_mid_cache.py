from datetime import timedelta
from decimal import Decimal
from unittest.mock import PropertyMock, patch

import pytest

from tests.support import T0, make_tick
from trading.domain.market import Tick


def test_mid_float_caches_the_existing_decimal_mid_conversion():
    tick = make_tick("150.001", "150.002")
    expected = float(tick.mid)
    with patch.object(Tick, "mid", new_callable=PropertyMock, return_value=tick.mid) as mid:
        assert tick.mid_float.hex() == expected.hex()
        assert tick.mid_float.hex() == expected.hex()
        assert mid.call_count == 1


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("update", [
    None,
    {"bid": Decimal("149.999")},
    {"ask": Decimal("150.100")},
    {"bid": Decimal("151.000"), "ask": Decimal("151.002")},
    {"received_at": T0 + timedelta(seconds=30)},
])
def test_model_copy_does_not_reuse_a_cached_mid(update, deep):
    tick = make_tick("150.001", "150.002")
    original = tick.mid_float
    copied = tick.model_copy(update=update, deep=deep)

    assert "mid_float" not in copied.__dict__
    assert copied.mid_float.hex() == float(copied.mid).hex()
    assert tick.mid_float == original
    assert tick.bid == Decimal("150.001")
    assert tick.ask == Decimal("150.002")


def test_mid_cache_does_not_change_equality_hash_or_serialization():
    tick = make_tick("150.001", "150.002")
    other = make_tick("150.001", "150.002")
    before = tick.model_dump_json()
    original_hash = hash(tick)

    assert tick.mid_float == float(tick.mid)
    assert tick == other
    assert hash(tick) == hash(other) == original_hash
    assert tick.model_dump_json() == before
    assert isinstance(tick.mid, Decimal)
