from decimal import Decimal

import pytest

from trading.portfolio.allocation import AllocationRequest, allocate_pro_rata


def test_pro_rata_matches_spec_example():
    # A requested 30k, B requested 10k, fill 20k -> A 15k, B 5k.
    result = allocate_pro_rata(
        [
            AllocationRequest("strategy_a", Decimal(30000)),
            AllocationRequest("strategy_b", Decimal(10000)),
        ],
        Decimal(20000),
        volume_step=Decimal(1000),
    )
    assert result.filled == {
        "strategy_a": Decimal(15000),
        "strategy_b": Decimal(5000),
    }
    assert result.pending == {
        "strategy_a": Decimal(15000),
        "strategy_b": Decimal(5000),
    }


def test_full_fill_leaves_no_pending():
    result = allocate_pro_rata(
        [
            AllocationRequest("strategy_a", Decimal(3000)),
            AllocationRequest("strategy_b", Decimal(1000)),
        ],
        Decimal(4000),
        volume_step=Decimal(1000),
    )
    assert result.filled == {
        "strategy_a": Decimal(3000),
        "strategy_b": Decimal(1000),
    }
    assert result.pending == {}


def test_remainder_assignment_is_deterministic():
    # Three equal requests, one step to distribute: ties break by strategy_id.
    result = allocate_pro_rata(
        [
            AllocationRequest("c", Decimal(1000)),
            AllocationRequest("a", Decimal(1000)),
            AllocationRequest("b", Decimal(1000)),
        ],
        Decimal(1000),
        volume_step=Decimal(1000),
    )
    assert result.filled == {
        "a": Decimal(1000),
        "b": Decimal(0),
        "c": Decimal(0),
    }


def test_overfill_rejected():
    with pytest.raises(ValueError):
        allocate_pro_rata(
            [AllocationRequest("a", Decimal(1000))],
            Decimal(2000),
        )
