"""FAILED_SPIKE_REVERSAL (scalp, RESEARCH_ONLY).

Research hypothesis: a sharp spike that fails to continue mean-reverts.
SELL sequence: spike up beyond k x short-term ATR -> spread normalizes ->
no new high -> price loses the pre-spike high -> tick momentum turns down.
The long side is the mirror image and is disabled by default (asymmetric
prior under current intervention-tail regime).

The base edge is tested WITHOUT an intervention gate first; gating is added
in ablation (models A/B/C), never baked in here.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading.domain.event import EventEnvelope
from trading.domain.market import TIMEFRAME_SECONDS
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal
from trading.indicators import DEFAULT_BAR_COUNT
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    market_span_to_calendar,
)
from trading.strategy.spread_gate import SpreadGate


class FailedSpikeReversalStrategy(Strategy):
    strategy_id = "failed_spike_reversal"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.SCALP

    @classmethod
    def warmup(cls, config: StrategyConfig) -> timedelta:
        # The slowest window is the entry-timeframe ATR; the tick window the
        # spike detection reads (window_seconds x 3) is added on top.
        entry_tf = config.timeframes.role("entry", "1m")
        params = [config.params_for(symbol) for symbol in config.instruments or [""]]
        atr_period = max(int(item.param("atr_period", 14)) for item in params)
        window_seconds = max(
            float(item.param("spike_window_seconds", 60)) for item in params
        )
        span = (atr_period + 1) * TIMEFRAME_SECONDS[entry_tf] + window_seconds * 3
        return market_span_to_calendar(span)

    @classmethod
    def bar_window(cls, config: StrategyConfig) -> int:
        # Only the entry-timeframe ATR reads bars, through IndicatorService's
        # max(default window, period + 1) fetch.
        params = [config.params_for(symbol) for symbol in config.instruments or [""]]
        atr_period = max(int(item.param("atr_period", 14)) for item in params)
        return max(DEFAULT_BAR_COUNT, atr_period + 1)

    @classmethod
    def tick_window_seconds(cls, config: StrategyConfig) -> float:
        # _evaluate reads spike_window x 3 of raw ticks; the momentum window
        # (spike_window / 2) sits inside it.
        params = [config.params_for(symbol) for symbol in config.instruments or [""]]
        return max(float(item.param("spike_window_seconds", 60)) for item in params) * 3

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
        params = cfg.params_for(symbol)
        entry_tf = cfg.timeframes.role("entry", "1m")
        window_seconds = float(params.param("spike_window_seconds", 60))
        k = float(params.param("spike_atr_multiple", 3.0))
        atr_period = int(params.param("atr_period", 14))
        stop_buffer_atr = float(params.param("stop_buffer_atr", 0.5))
        long_side_enabled = bool(params.param("long_side_enabled", False))
        horizon_seconds = int(params.param("expected_horizon_seconds", 300))
        spread_gate = SpreadGate.from_params(params)

        spec = ctx.market.instrument(symbol)
        if spec is None:
            return None
        pip = float(spec.pip_size)

        atr = ctx.indicators.atr(symbol, entry_tf, atr_period)
        if atr is None or atr <= 0:
            return None

        ticks = list(ctx.market.ticks(symbol, window_seconds * 3))
        if len(ticks) < 10:
            return None
        last = ticks[-1]
        if not spread_gate.allows(
            spread=last.spread,
            atr=atr,
            pip_size=spec.pip_size,
        ):
            return None

        mids = [float(t.mid) for t in ticks]
        momentum = ctx.indicators.tick_momentum(symbol, window_seconds / 2)
        if momentum is None:
            return None

        base = mids[0]
        spike_high = max(mids)
        spike_low = min(mids)
        current = mids[-1]

        # Upward spike that failed: SELL setup.
        spike_up = spike_high - base
        if spike_up > k * atr:
            no_new_high = current < spike_high
            lost_base_high = current < max(mids[: mids.index(spike_high)] or [base])
            if no_new_high and lost_base_high and momentum < 0:
                # One signal per spike (identified by its extreme tick).
                spike_time = ticks[mids.index(spike_high)].time
                if not self._new_setup(symbol, PositionDirection.SHORT, spike_time):
                    return None
                stop_price_distance = (spike_high - current) + stop_buffer_atr * atr
                return self.make_signal(
                    ctx,
                    symbol=symbol,
                    direction=PositionDirection.SHORT,
                    conviction=min(1.0, spike_up / (k * atr) - 1.0 + 0.5),
                    stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                    expected_horizon_seconds=horizon_seconds,
                    reason_codes=[
                        "SPIKE_UP_EXCEEDS_ATR",
                        "NO_NEW_HIGH",
                        "LOST_PRE_SPIKE_HIGH",
                        "TICK_MOMENTUM_DOWN",
                    ],
                )

        # Downward spike that failed: BUY setup (off by default).
        spike_down = base - spike_low
        if long_side_enabled and spike_down > k * atr:
            no_new_low = current > spike_low
            reclaimed = current > min(mids[: mids.index(spike_low)] or [base])
            if no_new_low and reclaimed and momentum > 0:
                spike_time = ticks[mids.index(spike_low)].time
                if not self._new_setup(symbol, PositionDirection.LONG, spike_time):
                    return None
                stop_price_distance = (current - spike_low) + stop_buffer_atr * atr
                return self.make_signal(
                    ctx,
                    symbol=symbol,
                    direction=PositionDirection.LONG,
                    conviction=min(1.0, spike_down / (k * atr) - 1.0 + 0.5),
                    stop_distance_pips=Decimal(str(round(stop_price_distance / pip, 1))),
                    expected_horizon_seconds=horizon_seconds,
                    reason_codes=[
                        "SPIKE_DOWN_EXCEEDS_ATR",
                        "NO_NEW_LOW",
                        "RECLAIMED_PRE_SPIKE_LOW",
                        "TICK_MOMENTUM_UP",
                    ],
                )
        return None
