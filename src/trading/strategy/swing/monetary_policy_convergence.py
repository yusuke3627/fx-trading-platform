"""MONETARY_POLICY_CONVERGENCE (swing, RESEARCH_ONLY).

Fundamental is a prior deciding which direction technical entries are taken
in, never an immediate SELL trigger.

The prior is read from what is actually measured — the mechanical scores of
the latest BOJ and Fed statements — not from an expectations series nobody
produces. A dovish Fed statement AND a hawkish BOJ statement AND non-low
intervention risk open the short side; the reverse pair opens the long side
(plus trend-timeframe uptrend restoration). Statement scores are a coarse,
meeting-frequency proxy for the expected-path repricing the research frames
this trade on; a market-implied path measure would replace them here.

Short: fundamental gate + technical trigger on the trigger timeframe
(lower high, support break, failed retest).
"""
from __future__ import annotations

from decimal import Decimal

from trading.domain.event import EventEnvelope
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal
from trading.indicators.market_structure import is_lower_high, rolling_low, swing_highs
from trading.intelligence import features as f
from trading.strategy.base import Strategy, StrategyContext, StrategyHorizon


class MonetaryPolicyConvergenceStrategy(Strategy):
    strategy_id = "monetary_policy_convergence"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.SWING

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
        trigger_tf = cfg.timeframes.role("trigger", "4h")
        trend_tf = cfg.timeframes.role("trend", "1d")
        left = int(cfg.param("swing_left", 2))
        right = int(cfg.param("swing_right", 2))
        support_lookback = int(cfg.param("support_lookback", 30))
        atr_period = int(cfg.param("atr_period", 14))
        stop_buffer_atr = float(cfg.param("stop_buffer_atr", 1.0))
        intervention_min_for_short = float(cfg.param("intervention_risk_min_for_short", 0.2))
        horizon_seconds = int(cfg.param("expected_horizon_seconds", 432000))

        spec = ctx.market.instrument(symbol)
        if spec is None:
            return None
        pip = float(spec.pip_size)

        atr = ctx.indicators.atr(symbol, trigger_tf, atr_period)
        if atr is None or atr <= 0:
            return None

        bars = list(ctx.market.bars(symbol, trigger_tf, support_lookback + 10))
        if len(bars) < support_lookback:
            return None
        current = float(bars[-1].close)

        if self._short_fundamental_gate(ctx, intervention_min_for_short):
            lower_high = is_lower_high(bars, left, right)
            support = rolling_low(bars[:-3] or bars, support_lookback)
            support_broken = support is not None and current < support
            failed_retest = support is not None and float(bars[-1].high) < support + atr
            if lower_high and support_broken and failed_retest:
                highs = swing_highs(bars, left, right)
                # One signal per structural setup (identified by the bar of
                # the last swing high), not one per market event.
                setup_id = bars[highs[-1]].start if highs else bars[-1].start
                if not self._new_setup(symbol, PositionDirection.SHORT, setup_id):
                    return None
                structural_high = float(bars[highs[-1]].high) if highs else max(
                    float(b.high) for b in bars[-10:]
                )
                stop_price_distance = (structural_high - current) + stop_buffer_atr * atr
                return self.make_signal(
                    ctx,
                    symbol=symbol,
                    direction=PositionDirection.SHORT,
                    conviction=0.5,
                    stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                    expected_horizon_seconds=horizon_seconds,
                    reason_codes=[
                        "POLICY_CONVERGENCE_GATE",
                        "LOWER_HIGH",
                        "SUPPORT_BREAK",
                        "FAILED_RETEST",
                    ],
                )

        if self._long_fundamental_gate(ctx) and self._trend_up(ctx, symbol, trend_tf):
            recent_low = rolling_low(bars, 10)
            if recent_low is not None and current > recent_low:
                if not self._new_setup(symbol, PositionDirection.LONG, bars[-1].start):
                    return None
                stop_price_distance = (current - recent_low) + stop_buffer_atr * atr
                return self.make_signal(
                    ctx,
                    symbol=symbol,
                    direction=PositionDirection.LONG,
                    conviction=0.4,
                    stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                    expected_horizon_seconds=horizon_seconds,
                    reason_codes=[
                        "FED_HAWKISH_REPRICING",
                        "BOJ_DOVISH_REPRICING",
                        "TREND_UPTREND_RESTORED",
                    ],
                )
        return None

    @staticmethod
    def _short_fundamental_gate(ctx: StrategyContext, intervention_min: float) -> bool:
        fed = ctx.features.get(f.FED_POLICY_SHIFT_SCORE)
        boj = ctx.features.get(f.BOJ_POLICY_SHIFT_SCORE)
        intervention = ctx.features.get(f.INTERVENTION_RISK)
        return (
            fed is not None
            and fed < 0
            and boj is not None
            and boj > 0
            and intervention is not None
            and intervention >= intervention_min
        )

    @staticmethod
    def _long_fundamental_gate(ctx: StrategyContext) -> bool:
        fed = ctx.features.get(f.FED_POLICY_SHIFT_SCORE)
        boj = ctx.features.get(f.BOJ_POLICY_SHIFT_SCORE)
        return fed is not None and fed > 0 and boj is not None and boj < 0

    def _trend_up(self, ctx: StrategyContext, symbol: str, trend_tf: str) -> bool:
        fast = ctx.indicators.ema(symbol, trend_tf, 20)
        slow = ctx.indicators.ema(symbol, trend_tf, 50)
        return fast is not None and slow is not None and fast > slow
