"""POST_EVENT_FAILED_BREAKOUT + macro confirmation (intraday, RESEARCH_ONLY).

Macro confirmation reads what the platform actually measures. "US rate
expectations" is the US2Y policy proxy at two horizons — the week's drift
(5d) standing for the repricing trend, the day's move (1d) for the immediate
reaction — and "BOJ hawkish" is the latest statement's mechanical score. A
downside data surprise stays in the short gate under its own name, but no
consensus source produces it yet, so that arm of the OR is inert until one
does.

Short: macro confirmation (US2Y weekly drift down OR US data downside
surprise OR hawkish BOJ statement) + a failed breakout above setup-timeframe
resistance on the entry timeframe.

Long is NOT symmetric: under the current policy-convergence / intervention-tail
regime it requires multiple confirmations (US2Y weekly drift up AND day move
up AND no intervention-risk escalation) and carries lower conviction.

Timeframes come from configuration (regime/setup/entry), not from code.
"""
from __future__ import annotations

from decimal import Decimal

from trading.domain.event import EventEnvelope
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal
from trading.indicators.market_structure import detect_failed_breakout, rolling_high
from trading.intelligence import features as f
from trading.strategy.base import Strategy, StrategyContext, StrategyHorizon


class PostEventFailedBreakoutStrategy(Strategy):
    strategy_id = "post_event_failed_breakout"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY

    async def on_event(
        self,
        event: EventEnvelope,
        context: StrategyContext,
    ) -> list[StrategySignal]:
        if not event.event_type.startswith("market."):
            return []
        signals = []
        for symbol in context.config.instruments:
            signal = self._evaluate(symbol, context)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, ctx: StrategyContext) -> StrategySignal | None:
        cfg = ctx.config
        setup_tf = cfg.timeframes.role("setup", "15m")
        entry_tf = cfg.timeframes.role("entry", "5m")
        lookback = int(cfg.param("resistance_lookback", 20))
        atr_period = int(cfg.param("atr_period", 14))
        stop_buffer_atr = float(cfg.param("stop_buffer_atr", 0.5))
        gate_eps = float(cfg.param("macro_gate_threshold", 0.0))
        intervention_max_for_long = float(cfg.param("intervention_risk_max_for_long", 0.5))
        horizon_seconds = int(cfg.param("expected_horizon_seconds", 21600))

        spec = ctx.market.instrument(symbol)
        if spec is None:
            return None
        pip = float(spec.pip_size)

        atr = ctx.indicators.atr(symbol, entry_tf, atr_period)
        if atr is None or atr <= 0:
            return None

        entry_bars = list(ctx.market.bars(symbol, entry_tf, lookback + 5))
        setup_bars = list(ctx.market.bars(symbol, setup_tf, lookback + 5))
        if len(entry_bars) < 3 or len(setup_bars) < lookback:
            return None
        current = float(entry_bars[-1].close)

        # Resistance from the setup timeframe, excluding the bars where the
        # breakout attempt itself happened.
        resistance = rolling_high(setup_bars[:-1], lookback)
        if resistance is None:
            return None

        if self._short_macro_gate(ctx, gate_eps) and detect_failed_breakout(
            entry_bars, resistance, side="UP"
        ):
            # One signal per failed-breakout attempt (identified by the
            # attempt bar), not one per market event while the setup holds.
            if not self._new_setup(symbol, PositionDirection.SHORT, entry_bars[-2].start):
                return None
            failed_high = max(float(b.high) for b in entry_bars[-2:])
            stop_price_distance = (failed_high - current) + stop_buffer_atr * atr
            return self.make_signal(
                ctx,
                symbol=symbol,
                direction=PositionDirection.SHORT,
                conviction=0.6,
                stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                expected_horizon_seconds=horizon_seconds,
                reason_codes=["MACRO_CONFIRMATION_SHORT", "FAILED_UPSIDE_BREAKOUT"],
            )

        support = None
        if len(setup_bars) >= lookback:
            support = min(float(b.low) for b in setup_bars[-lookback:-1] or setup_bars)
        if (
            support is not None
            and self._long_macro_gate(ctx, gate_eps, intervention_max_for_long)
            and detect_failed_breakout(entry_bars, support, side="DOWN")
        ):
            if not self._new_setup(symbol, PositionDirection.LONG, entry_bars[-2].start):
                return None
            failed_low = min(float(b.low) for b in entry_bars[-2:])
            stop_price_distance = (current - failed_low) + stop_buffer_atr * atr
            return self.make_signal(
                ctx,
                symbol=symbol,
                direction=PositionDirection.LONG,
                conviction=0.4,
                stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                expected_horizon_seconds=horizon_seconds,
                reason_codes=["MACRO_CONFIRMATION_LONG_STRICT", "FAILED_DOWNSIDE_BREAKOUT"],
            )
        return None

    @staticmethod
    def _short_macro_gate(ctx: StrategyContext, eps: float) -> bool:
        drift = ctx.features.get(f.US2Y_CHANGE_5D)
        surprise = ctx.features.get(f.US_DATA_SURPRISE)
        boj = ctx.features.get(f.BOJ_POLICY_SHIFT_SCORE)
        return (
            (drift is not None and drift < -eps)
            or (surprise is not None and surprise < -eps)
            or (boj is not None and boj > eps)
        )

    @staticmethod
    def _long_macro_gate(ctx: StrategyContext, eps: float, intervention_max: float) -> bool:
        drift = ctx.features.get(f.US2Y_CHANGE_5D)
        day_move = ctx.features.get(f.US2Y_CHANGE_1D)
        intervention = ctx.features.get(f.INTERVENTION_RISK)
        return (
            drift is not None
            and drift > eps
            and day_move is not None
            and day_move > eps
            and intervention is not None
            and intervention < intervention_max
        )
