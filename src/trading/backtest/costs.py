"""Execution cost and stress model.

The simulator never uses a fixed spread: spread always comes from observed
bid/ask, optionally stressed by a multiplier. Stress scenarios cover the
abnormal conditions around interventions and event bursts: spread x2/x5/x10,
fat-tail slippage, quote gaps, reject bursts and stop-through.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    # Multiplies the *observed* spread; there is deliberately no absolute
    # spread field in this model.
    spread_multiplier: float = 1.0

    latency_ms: float = 150.0

    # Slippage in pips: |gauss(0, sigma)| plus a fat tail that occurs with
    # `tail_probability` and adds up to `tail_max_pips`.
    slippage_sigma_pips: float = 0.3
    tail_probability: float = 0.0
    tail_max_pips: float = 0.0

    reject_probability: float = 0.0
    partial_fill_probability: float = 0.0

    # Extra adverse pips applied when a stop level is traded through.
    stop_through_pips: float = 0.0


STRESS_SCENARIOS: dict[str, CostModel] = {
    "normal": CostModel(),
    "spread_x2": CostModel(spread_multiplier=2.0, slippage_sigma_pips=0.6),
    "spread_x5": CostModel(
        spread_multiplier=5.0,
        slippage_sigma_pips=1.5,
        tail_probability=0.05,
        tail_max_pips=5.0,
        reject_probability=0.05,
        stop_through_pips=2.0,
    ),
    "spread_x10": CostModel(
        spread_multiplier=10.0,
        slippage_sigma_pips=3.0,
        tail_probability=0.15,
        tail_max_pips=20.0,
        reject_probability=0.20,
        partial_fill_probability=0.20,
        stop_through_pips=10.0,
    ),
}
