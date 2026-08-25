"""PostgreSQL-backed decision trail: signal, intent and how Risk graded it.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a throwaway strategy_id and removes its own rows.

The three tables are chained by foreign key and carry JSONB columns, so what
is exercised here — the ordering of the writes, the round-trip of the checks,
the protection fields folding into a spec and back — cannot be seen against a
fake that hands back the objects it was given.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading.domain.intent import PositionIntent, ProtectionSpec
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.risk import RiskCheck, RiskDecision
from trading.domain.signal import StrategySignal

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 13, tzinfo=UTC)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresDecisionRepository, connect

    conn = connect(DSN)
    strategy_id = f"test_{uuid4().hex[:12]}"
    account = f"test-account-{uuid4().hex[:8]}"
    other_account = f"test-account-{uuid4().hex[:8]}"
    yield PostgresDecisionRepository(conn), strategy_id, account, other_account
    # Foreign keys run signal <- intent <- decision, so removal runs the other
    # way round.
    conn.execute(
        """
        DELETE FROM risk_decisions WHERE intent_id IN (
            SELECT id FROM position_intents WHERE strategy_id = %s
        )
        """,
        (strategy_id,),
    )
    conn.execute("DELETE FROM position_intents WHERE strategy_id = %s", (strategy_id,))
    conn.execute("DELETE FROM strategy_signals WHERE strategy_id = %s", (strategy_id,))
    conn.commit()
    conn.close()


def make_trail(strategy_id: str, *, protected: bool = True, signal=None):
    signal = signal or StrategySignal(
        signal_id=uuid4(),
        strategy_id=strategy_id,
        strategy_version="0.1.0",
        symbol="USDJPY",
        desired_direction=PositionDirection.SHORT,
        conviction=0.7,
        expected_horizon_seconds=300,
        stop_distance_pips=Decimal("5.5"),
        reason_codes=["SPIKE", "FAILED_RETEST"],
        generated_at=T0,
    )
    intent = PositionIntent(
        intent_id=uuid4(),
        strategy_id=strategy_id,
        strategy_version="0.1.0",
        symbol="USDJPY",
        action=PositionAction.OPEN,
        direction=PositionDirection.SHORT,
        target_quantity=Decimal(1000),
        protection=(
            ProtectionSpec(
                stop_loss_price=Decimal("158.900"),
                take_profit_price=Decimal("158.700"),
                maximum_unprotected_seconds=30,
                source="STRATEGY",
            )
            if protected
            else None
        ),
        reason_codes=["SIZED"],
        generated_at=T0,
    )
    decision = RiskDecision(
        decision_id=uuid4(),
        intent_id=intent.intent_id,
        approved=False,
        checks=[
            RiskCheck(name="EXECUTION_ENABLED", passed=False),
            RiskCheck(name="QUOTE_FRESH", passed=True, detail="age=0.4s"),
        ],
        reject_codes=["EXECUTION_ENABLED"],
        decided_at=T0,
    )
    return signal, intent, decision


def only_ours(repo_and_id, limit: int = 50):
    r, _, account, _ = repo_and_id
    return r.recent(account, limit)


def test_a_trail_round_trips_through_the_database(repo):
    r, strategy_id, account, _ = repo
    signal, intent, decision = make_trail(strategy_id)

    r.record(account, signal, intent, decision)

    (read,) = only_ours(repo)
    assert read == (signal, intent, decision)


def test_the_risk_checks_survive_the_json_column(repo):
    # checks is JSONB; a detail string and the passed flags have to come back
    # as they went in, or a recorded rejection cannot be explained later.
    r, strategy_id, account, _ = repo
    signal, intent, decision = make_trail(strategy_id)

    r.record(account, signal, intent, decision)

    (_, _, read) = only_ours(repo)[0]
    assert [(c.name, c.passed, c.detail) for c in read.checks] == [
        ("EXECUTION_ENABLED", False, None),
        ("QUOTE_FRESH", True, "age=0.4s"),
    ]


def test_an_intent_without_protection_round_trips_as_none(repo):
    # An exit carries no protection, and the columns are nullable for it.
    r, strategy_id, account, _ = repo
    signal, intent, decision = make_trail(strategy_id, protected=False)

    r.record(account, signal, intent, decision)

    (_, read, _) = only_ours(repo)[0]
    assert read.protection is None


def test_another_accounts_trail_is_never_read(repo):
    # An intent is sized from the account's equity and a decision is graded
    # against its loss history, so a trail from another account is a different
    # judgement about a different book.
    r, strategy_id, account, other = repo
    ours = make_trail(strategy_id)
    theirs = make_trail(strategy_id)

    r.record(other, *theirs)
    r.record(account, *ours)

    trails = only_ours(repo)

    assert [t[0].signal_id for t in trails] == [ours[0].signal_id]


def test_a_signal_shared_by_two_intents_is_stored_once(repo):
    # A direction flip yields CLOSE then OPEN from one signal. Both trails name
    # it, and the primary key is what keeps it a single row.
    r, strategy_id, account, _ = repo
    signal, first, first_decision = make_trail(strategy_id)
    _, second, second_decision = make_trail(strategy_id, signal=signal)

    r.record(account, signal, first, first_decision)
    r.record(account, signal, second, second_decision)

    trails = only_ours(repo)
    assert len(trails) == 2
    assert {t[0].signal_id for t in trails} == {signal.signal_id}
    assert {t[1].intent_id for t in trails} == {first.intent_id, second.intent_id}
