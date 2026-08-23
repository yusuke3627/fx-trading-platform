"""Instrument specification.

Broker-specific values (pip size, contract size, volume limits, stop level,
sessions) come from the broker / market-data layer. They must never be
hard-coded inside strategy implementations.

Quantities are expressed in base-currency units throughout the system; the MT5
mapper converts units to broker lots via contract_size.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FillingMode(StrEnum):
    """How a broker may fill the requested volume."""

    FILL_OR_KILL = "FILL_OR_KILL"
    IMMEDIATE_OR_CANCEL = "IMMEDIATE_OR_CANCEL"


class InstrumentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    digits: int
    pip_size: Decimal
    contract_size: Decimal

    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal | None = None

    stop_level_points: int = 0

    # Brokers differ in which filling modes they accept per symbol (OANDA Japan
    # accepts IOC only for USD/JPY) and reject an order asking for one they do
    # not, so the accepted set is carried from the broker rather than assumed.
    accepted_filling_modes: frozenset[FillingMode]
