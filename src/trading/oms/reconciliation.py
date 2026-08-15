"""Reconciliation: broker truth vs internal state.

Fill classification:

    Broker deal
      -> known execution command?      COMMAND_FILL
      -> not an owned position?        UNTRACKED_FILL (CRITICAL, halts new risk)
      -> reason is SL/TP/stop-out?     PROTECTION_FILL
      -> otherwise                     RECONCILIATION_REQUIRED

"untracked_fill = 0" counts command-origin AND protection-origin fills as
tracked. UNKNOWN commands are resolved here from broker history, never by
resending.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading.domain.fill import BrokerDeal
from trading.domain.order import CommandState, ExecutionCommand
from trading.oms.state_machine import transition


class FillClassification(StrEnum):
    COMMAND_FILL = "COMMAND_FILL"
    PROTECTION_FILL = "PROTECTION_FILL"
    UNTRACKED_FILL = "UNTRACKED_FILL"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


def classify_deal(
    deal: BrokerDeal,
    *,
    known_command: bool,
    owned_position: bool,
) -> FillClassification:
    if known_command:
        return FillClassification.COMMAND_FILL
    if not owned_position:
        return FillClassification.UNTRACKED_FILL
    if deal.protection_reason is not None:
        return FillClassification.PROTECTION_FILL
    return FillClassification.RECONCILIATION_REQUIRED


def resolve_unknown(
    command: ExecutionCommand,
    *,
    now: datetime,
    broker_order_found: bool,
    broker_order_live: bool,
    filled_quantity: Decimal,
) -> ExecutionCommand | None:
    """Resolve an UNKNOWN command from broker history evidence.

    Returns None while the broker order is still live (working on the book):
    any resolution then could be contradicted by a later fill, so the command
    stays UNKNOWN — and keeps blocking new risk — until the broker order
    reaches a terminal state. A fully observed fill is terminal evidence on
    its own.
    """
    if command.state is not CommandState.UNKNOWN:
        raise ValueError(f"command is {command.state}, not UNKNOWN")
    if filled_quantity >= command.quantity:
        new_state = CommandState.FILLED
    elif broker_order_live:
        return None
    elif filled_quantity > 0:
        new_state = CommandState.PARTIAL_FILL
    elif broker_order_found:
        new_state = CommandState.CANCELLED
    else:
        # No broker side effect ever materialized.
        new_state = CommandState.REJECTED
    return transition(command, new_state, now=now, via_reconciliation=True)


@dataclass
class ReconciliationReport:
    started_at: datetime
    finished_at: datetime | None = None
    trigger: str = "manual"

    commands_checked: int = 0
    deals_checked: int = 0

    classifications: dict[str, FillClassification] = field(default_factory=dict)
    untracked_deal_ids: list[str] = field(default_factory=list)
    # Owned-position deals with no command and no protection reason: still
    # unexplained, so they block health exactly like untracked fills.
    unexplained_deal_ids: list[str] = field(default_factory=list)
    unresolved_command_ids: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return (
            not self.untracked_deal_ids
            and not self.unexplained_deal_ids
            and not self.unresolved_command_ids
        )


def reconcile_deals(
    deals: list[BrokerDeal],
    *,
    command_order_ids: set[str],
    owned_position_ids: set[str],
    started_at: datetime,
    trigger: str = "manual",
) -> ReconciliationReport:
    """Classify a batch of broker deals against known commands and owned
    positions (by ticket or lifecycle identifier)."""
    report = ReconciliationReport(started_at=started_at, trigger=trigger)
    for deal in deals:
        known_command = (
            deal.broker_order_id is not None and deal.broker_order_id in command_order_ids
        )
        owned = (
            (deal.broker_position_ticket or "") in owned_position_ids
            or (deal.broker_position_identifier or "") in owned_position_ids
        )
        classification = classify_deal(
            deal, known_command=known_command, owned_position=owned
        )
        report.classifications[deal.broker_deal_id] = classification
        if classification is FillClassification.UNTRACKED_FILL:
            report.untracked_deal_ids.append(deal.broker_deal_id)
        elif classification is FillClassification.RECONCILIATION_REQUIRED:
            report.unexplained_deal_ids.append(deal.broker_deal_id)
        report.deals_checked += 1
    return report
