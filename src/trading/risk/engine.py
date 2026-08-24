"""Pre-trade risk engine.

The initial risk configuration is a beginner's upper bound for entering micro
live, not a profit parameter, and is never a backtest optimization target.

Key invariant: if the broker minimum lot exceeds what risk allows, the trade
is rejected (MINIMUM_BROKER_SIZE_EXCEEDS_RISK) — the minimum lot never
overrides risk limits.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from trading.backtest.clock import Clock
from trading.domain.account import AccountMode, AccountSnapshot
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent
from trading.domain.market import Tick
from trading.domain.position import PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel, RiskCheck, RiskDecision
from trading.risk.limits import daily_loss_pct, hwm_drawdown_pct, rolling_24h_loss_pct


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    trading_enabled: bool = False

    max_open_positions: int = 1
    max_units_per_symbol: dict[str, int] = Field(default_factory=dict)

    max_risk_per_trade_pct: Decimal = Decimal("0.05")

    daily_loss_halt_pct: Decimal = Decimal("0.75")
    rolling_24h_loss_halt_pct: Decimal = Decimal("1.00")
    high_water_mark_drawdown_halt_pct: Decimal = Decimal("3.00")

    max_spread_pips: Decimal = Decimal("2.0")
    quote_max_age_seconds: float = 5.0
    min_margin_level_pct: Decimal = Decimal(300)

    require_broker_stop_loss: bool = True

    halt_on_unknown_order: bool = True
    halt_on_position_mismatch: bool = True
    halt_on_untracked_fill: bool = True

    event_mode_default: EventRiskMode = EventRiskMode.REDUCED


@dataclass(frozen=True)
class PreTradeContext:
    now: datetime

    execution_enabled: bool
    broker_connected: bool
    account_reconciled: bool

    quote: Tick | None
    instrument: InstrumentSpec

    account: AccountSnapshot
    snapshots: Sequence[AccountSnapshot]

    open_positions_count: int
    # SIGNED net exposure in units (+long / -short); the netting cap and the
    # position-cap exemption both depend on the sign.
    symbol_exposure_units: Decimal

    event_mode: EventRiskMode
    kill_switch: KillSwitchLevel

    unknown_commands: int
    # HEDGING is the safe default: the position cap then always applies.
    account_mode: AccountMode = AccountMode.HEDGING
    # Sum of |quantity| across the symbol's tickets. Hedging cap semantics:
    # offsetting tickets consume margin even when the net is flat.
    symbol_gross_exposure_units: Decimal = Decimal(0)
    position_mismatch: bool = False
    untracked_fills: int = 0
    duplicate: bool = False

    stop_distance_pips: Decimal | None = None
    requested_quantity: Decimal = field(default=Decimal(0))


class RiskEngine:
    def __init__(self, config: RiskConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    def evaluate(self, intent: PositionIntent, ctx: PreTradeContext) -> RiskDecision:
        cfg = self._config
        checks: list[RiskCheck] = []
        opening = intent.action in (PositionAction.OPEN, PositionAction.INCREASE)

        def check(name: str, passed: bool, detail: str | None = None) -> None:
            checks.append(RiskCheck(name=name, passed=passed, detail=detail))

        # System-state checks apply to every order, exits included.
        # execution_enabled=False signals a broken execution path (account
        # mode mismatch etc.); building even an exit there mishandles tickets.
        check("BROKER_CONNECTED", ctx.broker_connected)
        check("EXECUTION_ENABLED", ctx.execution_enabled)
        check(
            "KILL_SWITCH_ALLOWS_ORDER",
            ctx.kill_switch is not KillSwitchLevel.EMERGENCY
            if not opening
            else ctx.kill_switch is KillSwitchLevel.NONE,
            f"kill_switch={ctx.kill_switch}",
        )
        check("NOT_DUPLICATE", not ctx.duplicate)

        approved_quantity: Decimal | None = None

        if opening:
            check("TRADING_ENABLED", cfg.trading_enabled)
            check("EVENT_MODE_ALLOWS_ENTRY", ctx.event_mode is not EventRiskMode.HALT)

            # These halts stop NEW risk only. Blocking exits as well would
            # keep market risk on the book until reconciliation finishes —
            # the opposite of what an incident demands (UNKNOWN commands are
            # still never resent; that ban lives in the OMS state machine).
            check("ACCOUNT_RECONCILED", ctx.account_reconciled)
            if cfg.halt_on_unknown_order:
                check(
                    "NO_UNKNOWN_ORDERS",
                    ctx.unknown_commands == 0,
                    f"unknown={ctx.unknown_commands}",
                )
            if cfg.halt_on_position_mismatch:
                check("NO_POSITION_MISMATCH", not ctx.position_mismatch)
            if cfg.halt_on_untracked_fill:
                check("NO_UNTRACKED_FILL", ctx.untracked_fills == 0)

            # Age must be within [0, max]: a future-dated quote is not
            # "fresh", it is look-ahead (replay leak or broken broker clock).
            # Measured on OUR clock — known_time is when the quote reached us,
            # while `time` belongs to the broker's clock and the two are offset
            # (ADR-005). Aging a broker timestamp against now would shift every
            # quote by that offset; at OANDA Japan by three hours, in the
            # direction that reads as future-dated.
            quote_age = (
                (ctx.now - ctx.quote.known_time).total_seconds()
                if ctx.quote is not None
                else None
            )
            quote_fresh = (
                quote_age is not None and 0 <= quote_age <= cfg.quote_max_age_seconds
            )
            check("QUOTE_FRESH", quote_fresh)
            spread_ok = (
                ctx.quote is not None
                and ctx.quote.spread <= cfg.max_spread_pips * ctx.instrument.pip_size
            )
            check("SPREAD_ACCEPTABLE", spread_ok)

            if cfg.require_broker_stop_loss:
                check(
                    "PROTECTION_REQUIRED",
                    intent.protection is not None
                    and intent.protection.stop_loss_price is not None,
                    "broker-side SL is mandatory for new positions",
                )

            if intent.action is PositionAction.OPEN:
                # ONLY on a netting account does an opposite-direction OPEN
                # offset the existing exposure instead of adding a broker
                # position. On hedging it opens a separate ticket, so the cap
                # must always apply there.
                reduces_net = (
                    ctx.account_mode is AccountMode.NETTING
                    and ctx.symbol_exposure_units != 0
                    and (
                        (ctx.symbol_exposure_units > 0)
                        != (intent.direction is PositionDirection.LONG)
                    )
                )
                adds_broker_position = not reduces_net
            else:
                # INCREASE merges into the single net position on netting,
                # but on hedging EVERY order opens a new ticket — the cap
                # applies exactly like an OPEN there.
                adds_broker_position = ctx.account_mode is AccountMode.HEDGING
            if adds_broker_position:
                check(
                    "MAX_OPEN_POSITIONS",
                    ctx.open_positions_count < cfg.max_open_positions,
                    f"open={ctx.open_positions_count} max={cfg.max_open_positions}",
                )

            check(
                "DAILY_LOSS_WITHIN_LIMIT",
                daily_loss_pct(ctx.snapshots, ctx.account, ctx.now) < cfg.daily_loss_halt_pct,
            )
            check(
                "ROLLING_24H_LOSS_WITHIN_LIMIT",
                rolling_24h_loss_pct(ctx.snapshots, ctx.account, ctx.now)
                < cfg.rolling_24h_loss_halt_pct,
            )
            check(
                "HWM_DRAWDOWN_WITHIN_LIMIT",
                hwm_drawdown_pct(ctx.account) < cfg.high_water_mark_drawdown_halt_pct,
            )
            # With open margin, an unknown margin level is a failure, not a
            # pass: a missing snapshot value must never approve new risk.
            margin_ok = ctx.account.margin == 0 or (
                ctx.account.margin_level is not None
                and ctx.account.margin_level >= cfg.min_margin_level_pct
            )
            check("MARGIN_BUFFER", margin_ok)

            approved_quantity = self._size_check(intent, ctx, check)

        approved = all(c.passed for c in checks)
        return RiskDecision(
            decision_id=uuid4(),
            intent_id=intent.intent_id,
            approved=approved,
            approved_quantity=approved_quantity if approved else None,
            checks=checks,
            reject_codes=[c.name for c in checks if not c.passed],
            decided_at=self._clock.now(),
        )

    def _size_check(self, intent, ctx: PreTradeContext, check) -> Decimal | None:
        cfg = self._config
        spec = ctx.instrument

        if ctx.stop_distance_pips is None or ctx.stop_distance_pips <= 0:
            check("STOP_DISTANCE_KNOWN", False, "cannot size without a stop distance")
            return None

        risk_budget = ctx.account.equity * cfg.max_risk_per_trade_pct / Decimal(100)
        loss_per_unit = ctx.stop_distance_pips * spec.pip_size
        allowed = (risk_budget / loss_per_unit // spec.volume_step) * spec.volume_step

        # Instrument-independent structure means limits must fail closed: a
        # symbol without a configured cap is a config omission, not unlimited.
        max_units = cfg.max_units_per_symbol.get(intent.symbol)
        check(
            "SYMBOL_LIMIT_CONFIGURED",
            max_units is not None,
            f"max_units_per_symbol missing for {intent.symbol}",
        )
        if max_units is None:
            return None
        if ctx.account_mode is AccountMode.NETTING:
            # Netting: one net position, bound |net| <= max_units. An
            # opposite-direction quantity either shrinks |net| (always within
            # the cap) or, past zero, becomes the from-flat size of the new
            # side — the OMS never crosses zero in one order, so a reversal
            # re-opens from flat and the approved size must fit the cap by
            # itself, never max_units + |current|.
            opposes = ctx.symbol_exposure_units != 0 and (
                (ctx.symbol_exposure_units > 0)
                != (intent.direction is PositionDirection.LONG)
            )
            if opposes:
                headroom = Decimal(max_units)
            elif intent.direction is PositionDirection.LONG:
                headroom = Decimal(max_units) - ctx.symbol_exposure_units
            else:
                headroom = Decimal(max_units) + ctx.symbol_exposure_units
        else:
            # Hedging: every ticket adds GROSS exposure (and consumes margin)
            # even when the net is flat, so the cap bounds the ticket sum.
            headroom = Decimal(max_units) - ctx.symbol_gross_exposure_units
        allowed = min(allowed, max(headroom, Decimal(0)))

        if ctx.event_mode is EventRiskMode.REDUCED:
            allowed = (allowed / 2 // spec.volume_step) * spec.volume_step

        requested = ctx.requested_quantity if ctx.requested_quantity > 0 else allowed
        quantity = min(requested, allowed)

        # The broker minimum never overrides risk: if the smallest tradable
        # size already exceeds the allowance, reject instead of trading it.
        check(
            "MINIMUM_BROKER_SIZE_EXCEEDS_RISK",
            spec.volume_min <= quantity,
            f"volume_min={spec.volume_min} allowed={quantity}",
        )
        if spec.volume_min > quantity:
            return None
        return quantity
