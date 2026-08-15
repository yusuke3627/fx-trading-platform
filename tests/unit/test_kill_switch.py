import pytest

from tests.support import FixedClock
from trading.domain.risk import KillSwitchLevel
from trading.risk.kill_switch import (
    EMERGENCY_POLICY,
    KillSwitch,
    KillSwitchDeescalationError,
)


def test_escalation_path_and_history():
    switch = KillSwitch(FixedClock())
    switch.trip(KillSwitchLevel.HALT_NEW_ORDER, "unknown order detected")
    switch.trip(KillSwitchLevel.CLOSE_ONLY, "reconciliation degraded")
    switch.trip(KillSwitchLevel.EMERGENCY, "position mismatch")
    assert switch.level is KillSwitchLevel.EMERGENCY
    assert [t.level for t in switch.history] == [
        KillSwitchLevel.HALT_NEW_ORDER,
        KillSwitchLevel.CLOSE_ONLY,
        KillSwitchLevel.EMERGENCY,
    ]


def test_trip_cannot_deescalate():
    switch = KillSwitch(FixedClock())
    switch.trip(KillSwitchLevel.CLOSE_ONLY, "incident")
    with pytest.raises(KillSwitchDeescalationError):
        switch.trip(KillSwitchLevel.HALT_NEW_ORDER, "feeling better")


def test_reset_requires_healthy_reconciliation():
    switch = KillSwitch(FixedClock())
    switch.trip(KillSwitchLevel.HALT_NEW_ORDER, "incident")
    with pytest.raises(KillSwitchDeescalationError):
        switch.reset(reconciliation_healthy=False, by="operator")
    switch.reset(reconciliation_healthy=True, by="operator")
    assert switch.level is KillSwitchLevel.NONE


def test_permissions_per_level():
    switch = KillSwitch(FixedClock())
    assert switch.allows_new_risk() and switch.allows_exit_orders()

    switch.trip(KillSwitchLevel.HALT_NEW_ORDER, "incident")
    assert not switch.allows_new_risk()
    assert switch.allows_exit_orders()

    switch.trip(KillSwitchLevel.EMERGENCY, "incident")
    assert not switch.allows_exit_orders()


def test_emergency_is_not_blanket_market_close():
    assert EMERGENCY_POLICY == (
        "FREEZE_NEW_RISK",
        "RECONCILE",
        "EVALUATE_EXECUTABLE_EXIT",
    )
