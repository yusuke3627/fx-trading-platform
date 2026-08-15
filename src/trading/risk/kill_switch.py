"""Kill switch.

Three escalation levels. EMERGENCY is NOT "market-close everything": closing
into a broken market can be worse, so the emergency policy is freeze new risk
+ reconcile + evaluate executable exits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.backtest.clock import Clock
from trading.domain.risk import KillSwitchLevel

_ESCALATION_ORDER = [
    KillSwitchLevel.NONE,
    KillSwitchLevel.HALT_NEW_ORDER,
    KillSwitchLevel.CLOSE_ONLY,
    KillSwitchLevel.EMERGENCY,
]

EMERGENCY_POLICY: tuple[str, ...] = (
    "FREEZE_NEW_RISK",
    "RECONCILE",
    "EVALUATE_EXECUTABLE_EXIT",
)


@dataclass(frozen=True)
class KillSwitchTransition:
    level: KillSwitchLevel
    reason: str
    at: datetime


class KillSwitchDeescalationError(RuntimeError):
    pass


class KillSwitch:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._level = KillSwitchLevel.NONE
        self.history: list[KillSwitchTransition] = []

    @property
    def level(self) -> KillSwitchLevel:
        return self._level

    def trip(self, level: KillSwitchLevel, reason: str) -> None:
        if _ESCALATION_ORDER.index(level) < _ESCALATION_ORDER.index(self._level):
            raise KillSwitchDeescalationError(
                f"cannot trip from {self._level} down to {level}; use reset()"
            )
        self._level = level
        self.history.append(KillSwitchTransition(level, reason, self._clock.now()))

    def reset(self, *, reconciliation_healthy: bool, by: str) -> None:
        """De-escalation is manual and requires a healthy reconciliation."""
        if not reconciliation_healthy:
            raise KillSwitchDeescalationError("reset requires a healthy reconciliation")
        self._level = KillSwitchLevel.NONE
        self.history.append(
            KillSwitchTransition(KillSwitchLevel.NONE, f"reset by {by}", self._clock.now())
        )

    def allows_new_risk(self) -> bool:
        return self._level is KillSwitchLevel.NONE

    def allows_exit_orders(self) -> bool:
        return self._level is not KillSwitchLevel.EMERGENCY
