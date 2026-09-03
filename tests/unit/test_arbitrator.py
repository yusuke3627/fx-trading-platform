"""同時 signal の portfolio 裁定。"""
import random
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from tests.support import T0, at, eurusd_spec, gbpjpy_spec, gbpusd_spec, usdjpy_spec
from trading.domain.arbitration import (
    REJECTED_EXPIRED,
    REJECTED_REDUNDANT_FACTOR_EXPOSURE,
    REJECTED_TRADING_DISABLED,
    REJECTED_TRIANGLE_CAP,
    ArbitrationCandidate,
    CandidateSignal,
)
from trading.domain.exposure import OpenPositionExposure
from trading.domain.instrument import InstrumentSpec
from trading.domain.position import PositionDirection
from trading.portfolio.arbitrator import ArbitratorConfig, PortfolioArbitrator


def candidate(
    spec: InstrumentSpec,
    direction: PositionDirection,
    confidence: str,
    *,
    strategy_id: str = "test_strategy",
    units: str = "1000",
    price: str,
    stop: str | None = None,
    generated_at=T0,
    expected_horizon_seconds: int = 300,
    trading_enabled: bool = True,
    signal_id: UUID | None = None,
) -> ArbitrationCandidate:
    quantity = Decimal(units)
    return ArbitrationCandidate(
        signal=CandidateSignal(
            signal_id=signal_id or uuid4(),
            strategy_id=strategy_id,
            symbol=spec.symbol,
            position_direction=direction,
            expected_edge_r=Decimal(1),
            confidence=Decimal(confidence),
            stop_distance_pips=Decimal(5),
            generated_at=generated_at,
            expires_at=generated_at + timedelta(seconds=expected_horizon_seconds),
        ),
        exposure=OpenPositionExposure(
            spec=spec,
            signed_units=(
                quantity if direction is PositionDirection.LONG else -quantity
            ),
            mark_price=Decimal(price),
            stop_loss_price=Decimal(stop) if stop is not None else None,
        ),
        trading_enabled=trading_enabled,
    )


def book_position(
    spec: InstrumentSpec,
    direction: PositionDirection,
    *,
    units: str = "1000",
    price: str,
    stop: str | None = None,
) -> OpenPositionExposure:
    quantity = Decimal(units)
    return OpenPositionExposure(
        spec=spec,
        signed_units=quantity if direction is PositionDirection.LONG else -quantity,
        mark_price=Decimal(price),
        stop_loss_price=Decimal(stop) if stop is not None else None,
    )


def test_selection_is_independent_of_input_order():
    candidates = [
        candidate(eurusd_spec(), PositionDirection.SHORT, "0.8", price="1.08"),
        candidate(gbpusd_spec(), PositionDirection.SHORT, "0.6", price="1.27"),
        candidate(usdjpy_spec(), PositionDirection.LONG, "0.7", price="158.84"),
        candidate(gbpjpy_spec(), PositionDirection.SHORT, "0.5", price="201.70"),
        candidate(eurusd_spec(), PositionDirection.LONG, "0.4", price="1.08"),
    ]
    arbitrator = PortfolioArbitrator(ArbitratorConfig())
    selections = []
    for seed in range(3):
        shuffled = list(candidates)
        random.Random(seed).shuffle(shuffled)
        result = arbitrator.select(shuffled, [], T0)
        selections.append(
            [
                (decision.signal_id, decision.rank, decision.reason_code)
                for decision in (*result.accepted, *result.rejected)
            ]
        )

    assert selections[0] == selections[1] == selections[2]


def test_duplicate_usd_factor_selects_the_strongest_signal():
    strongest = candidate(
        eurusd_spec(), PositionDirection.SHORT, "0.8", price="1.08"
    )
    result = PortfolioArbitrator(ArbitratorConfig()).select(
        [
            candidate(gbpusd_spec(), PositionDirection.SHORT, "0.6", price="1.27"),
            candidate(usdjpy_spec(), PositionDirection.LONG, "0.7", price="158.84"),
            strongest,
        ],
        [],
        T0,
    )

    assert [(decision.signal_id, decision.rank) for decision in result.accepted] == [
        (strongest.signal.signal_id, 1)
    ]
    assert [decision.rank for decision in result.rejected] == [2, 3]
    for decision in result.rejected:
        assert decision.reason_code == REJECTED_REDUNDANT_FACTOR_EXPOSURE
        assert "USD LONG" in decision.detail
        assert "test_strategy/EURUSD" in decision.detail


def test_candidates_without_a_shared_leg_are_all_accepted():
    eurusd = candidate(eurusd_spec(), PositionDirection.LONG, "0.8", price="1.08")
    usdjpy = candidate(usdjpy_spec(), PositionDirection.LONG, "0.7", price="158.84")

    result = PortfolioArbitrator(ArbitratorConfig()).select(
        [usdjpy, eurusd], [], T0
    )

    assert [decision.rank for decision in result.accepted] == [1, 2]
    assert result.accepted[0].book_before == ()
    assert result.accepted[1].book_before == (eurusd.exposure,)


def test_expired_candidate_is_rejected_without_a_rank():
    expired = candidate(
        usdjpy_spec(),
        PositionDirection.LONG,
        "0.7",
        price="158.84",
        expected_horizon_seconds=300,
    )

    result = PortfolioArbitrator(ArbitratorConfig()).select(
        [expired], [], at(minutes=6)
    )

    (decision,) = result.rejected
    assert decision.reason_code == REJECTED_EXPIRED
    assert decision.rank is None
    assert decision.priority is None


def test_trading_disabled_candidate_does_not_claim_its_factor():
    disabled = candidate(
        eurusd_spec(),
        PositionDirection.SHORT,
        "0.9",
        price="1.08",
        trading_enabled=False,
    )
    enabled = candidate(
        usdjpy_spec(), PositionDirection.LONG, "0.5", price="158.84"
    )

    result = PortfolioArbitrator(ArbitratorConfig()).select(
        [disabled, enabled], [], T0
    )

    assert [(decision.signal_id, decision.rank) for decision in result.accepted] == [
        (enabled.signal.signal_id, 1)
    ]
    assert result.rejected[0].reason_code == REJECTED_TRADING_DISABLED
    assert result.rejected[0].rank is None


def test_existing_portfolio_overlap_changes_ranking():
    book = [
        book_position(
            usdjpy_spec(), PositionDirection.LONG, price="158.84"
        )
    ]
    eurusd = candidate(eurusd_spec(), PositionDirection.SHORT, "0.7", price="1.08")
    gbpjpy = candidate(gbpjpy_spec(), PositionDirection.SHORT, "0.65", price="201.70")

    result = PortfolioArbitrator(ArbitratorConfig()).select(
        [eurusd, gbpjpy], book, T0
    )

    assert [decision.signal_id for decision in result.accepted] == [
        gbpjpy.signal.signal_id,
        eurusd.signal.signal_id,
    ]
    assert [decision.priority for decision in result.accepted] == [
        Decimal("0.65"),
        Decimal("0.60"),
    ]


def test_existing_portfolio_penalty_only_applies_to_same_direction_legs():
    book = [
        book_position(
            usdjpy_spec(), PositionDirection.LONG, price="158.84"
        )
    ]
    opposite = candidate(
        eurusd_spec(), PositionDirection.LONG, "0.7", price="1.08"
    )

    result = PortfolioArbitrator(ArbitratorConfig()).select([opposite], book, T0)

    assert result.accepted[0].priority == Decimal("0.7")


def test_triangle_hard_cap_rejects_the_third_distinct_symbol():
    book = [
        book_position(gbpusd_spec(), PositionDirection.LONG, price="1.27"),
        book_position(usdjpy_spec(), PositionDirection.SHORT, price="158.84"),
    ]
    candidate_to_reject = candidate(
        gbpjpy_spec(), PositionDirection.LONG, "0.7", price="201.70"
    )

    rejected = PortfolioArbitrator(ArbitratorConfig()).select(
        [candidate_to_reject], book, T0
    )
    accepted = PortfolioArbitrator(
        ArbitratorConfig(max_pairs_per_triangle=3)
    ).select([candidate_to_reject], book, T0)

    assert rejected.rejected[0].reason_code == REJECTED_TRIANGLE_CAP
    assert "GBPJPY/GBPUSD/USDJPY" in rejected.rejected[0].detail
    assert accepted.accepted[0].signal_id == candidate_to_reject.signal.signal_id


def test_triangle_cap_counts_candidates_accepted_in_the_same_cycle():
    candidates = [
        candidate(gbpusd_spec(), PositionDirection.LONG, "0.9", price="1.27"),
        candidate(usdjpy_spec(), PositionDirection.LONG, "0.8", price="158.84"),
        candidate(gbpjpy_spec(), PositionDirection.SHORT, "0.7", price="201.70"),
    ]

    result = PortfolioArbitrator(ArbitratorConfig()).select(candidates, [], T0)

    assert [decision.rank for decision in result.accepted] == [1, 2]
    assert result.rejected[0].rank == 3
    assert result.rejected[0].reason_code == REJECTED_TRIANGLE_CAP


def test_equal_priority_uses_strategy_symbol_and_signal_id_as_tiebreakers():
    later_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    earlier_id = UUID("00000000-0000-0000-0000-000000000000")
    candidates = [
        candidate(
            usdjpy_spec(),
            PositionDirection.LONG,
            "0.7",
            strategy_id="strategy_b",
            price="158.84",
        ),
        candidate(
            eurusd_spec(),
            PositionDirection.LONG,
            "0.7",
            strategy_id="strategy_a",
            price="1.08",
            signal_id=later_id,
        ),
        candidate(
            eurusd_spec(),
            PositionDirection.LONG,
            "0.7",
            strategy_id="strategy_a",
            price="1.08",
            signal_id=earlier_id,
        ),
    ]

    result = PortfolioArbitrator(ArbitratorConfig()).select(candidates, [], T0)
    ranks = {
        decision.signal_id: decision.rank
        for decision in (*result.accepted, *result.rejected)
    }

    assert ranks[earlier_id] == 1
    assert ranks[later_id] == 2
    assert ranks[candidates[0].signal.signal_id] == 3
