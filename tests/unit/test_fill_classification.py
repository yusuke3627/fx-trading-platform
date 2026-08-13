from decimal import Decimal

from trading.domain.fill import BrokerDeal, ProtectionReason
from trading.domain.order import ExecutionSide
from trading.oms.reconciliation import (
    FillClassification,
    classify_deal,
    reconcile_deals,
)

from tests.support import T0


def make_deal(
    deal_id: str = "deal-1",
    order_id: str | None = "order-1",
    position: str | None = "pos-1",
    protection: ProtectionReason | None = None,
) -> BrokerDeal:
    return BrokerDeal(
        broker_deal_id=deal_id,
        broker_order_id=order_id,
        broker_position_ticket=position,
        broker_position_identifier=position,
        reason_code=4 if protection is ProtectionReason.STOP_LOSS else 0,
        protection_reason=protection,
        side=ExecutionSide.BUY,
        quantity=Decimal("1000"),
        price=Decimal("158.840"),
        broker_time=T0,
    )


def test_known_command_is_command_fill():
    deal = make_deal()
    assert (
        classify_deal(deal, known_command=True, owned_position=True)
        is FillClassification.COMMAND_FILL
    )


def test_protection_on_owned_position_is_protection_fill():
    deal = make_deal(order_id=None, protection=ProtectionReason.STOP_LOSS)
    assert (
        classify_deal(deal, known_command=False, owned_position=True)
        is FillClassification.PROTECTION_FILL
    )


def test_unowned_position_is_untracked_and_critical():
    deal = make_deal(order_id=None, protection=ProtectionReason.STOP_LOSS)
    assert (
        classify_deal(deal, known_command=False, owned_position=False)
        is FillClassification.UNTRACKED_FILL
    )


def test_owned_but_unexplained_requires_reconciliation():
    deal = make_deal(order_id=None, protection=None)
    assert (
        classify_deal(deal, known_command=False, owned_position=True)
        is FillClassification.RECONCILIATION_REQUIRED
    )


def test_reconcile_deals_reports_untracked_and_health():
    tracked = make_deal(deal_id="d1", order_id="known-order")
    protection = make_deal(
        deal_id="d2", order_id=None, protection=ProtectionReason.TAKE_PROFIT
    )
    untracked = make_deal(deal_id="d3", order_id=None, position="foreign-pos")

    report = reconcile_deals(
        [tracked, protection, untracked],
        command_order_ids={"known-order"},
        owned_position_ids={"pos-1"},
        started_at=T0,
    )
    assert report.classifications["d1"] is FillClassification.COMMAND_FILL
    assert report.classifications["d2"] is FillClassification.PROTECTION_FILL
    assert report.classifications["d3"] is FillClassification.UNTRACKED_FILL
    assert report.untracked_deal_ids == ["d3"]
    assert report.healthy is False
