"""RANGE_EDGE_REVERSAL (intraday, RESEARCH_ONLY).

Research hypothesis: a failed break of a flat range returns to its midpoint.
Midpoint take profit and a holding deadline aim to change the protection-fill
dominated exits seen in H4/H5. Timeframes and thresholds come from configuration.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from trading.domain.event import EventEnvelope
from trading.domain.market import TIMEFRAME_SECONDS
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal
from trading.indicators import DEFAULT_BAR_COUNT
from trading.indicators.ema import ema_series
from trading.indicators.session import Session, session_end, session_start, sessions_at
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    market_span_to_calendar,
)
from trading.strategy.parameters import ResolvedStrategyParameters
from trading.strategy.spread_gate import SpreadGate


class _Range(NamedTuple):
    session: Session
    start: datetime
    built_at: datetime
    high: Decimal
    low: Decimal
    eligible: bool
    invalidated: bool


def _regime_count(params: ResolvedStrategyParameters) -> int:
    return max(
        int(params.param("range_lookback_bars", 24)),
        int(params.param("ema_period", 20)) + int(params.param("range_slope_lookback", 6)),
        int(params.param("atr_period", 14)) + 1,
    )


def _latest_session_end(open_sessions: frozenset[Session], now: datetime) -> datetime:
    return max(session_end(session, now) for session in open_sessions)


class RangeEdgeReversalStrategy(Strategy):
    strategy_id = "range_edge_reversal"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY

    @classmethod
    def warmup(cls, config: StrategyConfig) -> timedelta:
        regime_tf = config.timeframes.role("regime", "1h")
        entry_tf = config.timeframes.role("entry", "5m")
        params = [config.params_for(symbol) for symbol in config.instruments or [""]]
        span = max(
            max(
                _regime_count(item) * TIMEFRAME_SECONDS[regime_tf],
                (int(item.param("atr_period", 14)) + 1 + int(item.param("reentry_max_bars", 6)) + 1)
                * TIMEFRAME_SECONDS[entry_tf],
            )
            for item in params
        )
        return market_span_to_calendar(span)

    @classmethod
    def bar_window(cls, config: StrategyConfig) -> int:
        params = [config.params_for(symbol) for symbol in config.instruments or [""]]
        return max(
            DEFAULT_BAR_COUNT,
            max(_regime_count(item) for item in params),
            max(int(item.param("reentry_max_bars", 6)) + 1 for item in params),
        )

    async def on_event(
        self,
        event: EventEnvelope,
        context: StrategyContext,
    ) -> list[StrategySignal]:
        if not event.event_type.startswith("market."):
            return []
        signals = []
        for symbol in context.config.instruments:
            signal = self._horizon_exit(context, symbol, default_horizon_seconds=3600)
            if signal is not None:
                signals.append(signal)
                continue
            if not self._session_permits_evaluation(context, symbol):
                continue
            signal = self._evaluate(symbol, context)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, ctx: StrategyContext) -> StrategySignal | None:
        params = ctx.config.params_for(symbol)
        regime_tf = ctx.config.timeframes.role("regime", "1h")
        entry_tf = ctx.config.timeframes.role("entry", "5m")
        regime_count = _regime_count(params)
        lookback = int(params.param("range_lookback_bars", 24))
        ema_period = int(params.param("ema_period", 20))
        slope_lookback = int(params.param("range_slope_lookback", 6))
        atr_period = int(params.param("atr_period", 14))
        reentry_max_bars = int(params.param("reentry_max_bars", 6))
        entry_count = reentry_max_bars + 1
        entry_band_fraction = Decimal(str(params.param("entry_band_fraction", 0.2)))
        stop_buffer_atr = Decimal(str(params.param("stop_buffer_atr", 0.25)))
        min_reward_to_risk = Decimal(str(params.param("min_reward_to_risk", 1.5)))
        take_profit_enabled = bool(params.param("take_profit_enabled", True))
        horizon_seconds = int(params.param("expected_horizon_seconds", 3600))

        spec = ctx.market.instrument(symbol)
        if spec is None:
            return None
        pip = spec.pip_size
        now = ctx.clock.now()
        memo: dict[str, _Range] = self.__dict__.setdefault("_ranges", {})
        open_sessions = sessions_at(now)
        if not open_sessions:
            memo.pop(symbol, None)
            return None
        session, start = max(
            ((session, session_start(session, now)) for session in open_sessions),
            key=lambda item: (item[1], item[0].value),
        )
        rng = memo.get(symbol)
        if rng is None or (rng.session, rng.start) != (session, start):
            regime_bars = list(ctx.market.bars(symbol, regime_tf, regime_count))
            high = low = Decimal(0)
            eligible = False
            if len(regime_bars) >= regime_count:
                range_bars = regime_bars[-lookback:]
                stale_after = timedelta(hours=float(params.param("range_stale_hours", 72)))
                if start - range_bars[0].known_at <= stale_after:
                    high = max(bar.high for bar in range_bars)
                    low = min(bar.low for bar in range_bars)
                    atr_regime = ctx.indicators.atr(symbol, regime_tf, atr_period)
                    if atr_regime is not None and atr_regime > 0:
                        closes = [
                            float(bar.close)
                            for bar in regime_bars[-(ema_period + slope_lookback) :]
                        ]
                        series = ema_series(closes, ema_period)
                        slope = abs(series[-1] - series[-1 - slope_lookback])
                        eligible = (
                            slope <= float(params.param("range_slope_max_atr", 0.5)) * atr_regime
                            and high - low
                            >= Decimal(str(params.param("range_width_min_atr", 1.5)))
                            * Decimal(str(atr_regime))
                        )
            rng = memo[symbol] = _Range(session, start, now, high, low, eligible, False)
        if not rng.eligible:
            return None
        regime_bars = ctx.market.bars(symbol, regime_tf, regime_count)
        if any(
            bar.known_at > rng.built_at and not (rng.low <= bar.close <= rng.high)
            for bar in regime_bars
        ):
            rng = memo[symbol] = rng._replace(invalidated=True)
        if rng.invalidated:
            return None

        if _latest_session_end(open_sessions, now) - now < timedelta(
            seconds=int(params.param("session_end_buffer_seconds", 3600))
        ):
            return None
        atr_entry = ctx.indicators.atr(symbol, entry_tf, atr_period)
        if atr_entry is None or atr_entry <= 0:
            return None
        tick = ctx.market.latest_tick(symbol)
        if tick is None or not SpreadGate.from_params(params).allows(
            spread=tick.spread,
            atr=atr_entry,
            pip_size=spec.pip_size,
        ):
            return None
        entry_bars = list(ctx.market.bars(symbol, entry_tf, entry_count))
        if len(entry_bars) < entry_count:
            return None
        last = entry_bars[-1]
        if not rng.low <= last.close <= rng.high:
            return None

        for direction in (PositionDirection.LONG, PositionDirection.SHORT):
            if not self._session_permits_setup(ctx, symbol, direction):
                continue
            is_long = direction is PositionDirection.LONG
            run = 0
            for bar in reversed(entry_bars[:-1]):
                outside = bar.close < rng.low if is_long else bar.close > rng.high
                if not outside:
                    break
                run += 1
            if run == 0 and not (
                last.low < rng.low if is_long else last.high > rng.high
            ):
                continue
            if run + 1 > reentry_max_bars:
                continue
            attempt = entry_bars[-(run + 1) :]
            if attempt[0].known_at <= rng.built_at:
                continue
            mid = (rng.high + rng.low) / 2
            band = entry_band_fraction * (rng.high - rng.low)
            if is_long:
                price = tick.ask
                if price > rng.low + band:
                    continue
                extreme = min(bar.low for bar in attempt)
                stop = extreme - stop_buffer_atr * Decimal(str(atr_entry))
                risk, reward = price - stop, mid - price
                reason = "RANGE_LOWER_EDGE_REENTRY"
            else:
                price = tick.bid
                if price < rng.high - band:
                    continue
                extreme = max(bar.high for bar in attempt)
                stop = extreme + stop_buffer_atr * Decimal(str(atr_entry))
                risk, reward = stop - price, price - mid
                reason = "RANGE_UPPER_EDGE_REENTRY"
            if risk <= 0 or reward < min_reward_to_risk * risk:
                continue
            stop_distance = round(risk / pip, 1)
            take_profit_distance = (
                round(reward / pip, 1) if take_profit_enabled else None
            )
            if stop_distance <= 0 or (
                take_profit_distance is not None and take_profit_distance <= 0
            ):
                continue
            return self._setup_signal(
                ctx,
                symbol=symbol,
                direction=direction,
                setup_id=(rng.session, rng.start),
                conviction=0.5,
                stop_distance_pips=stop_distance,
                take_profit_distance_pips=take_profit_distance,
                expected_horizon_seconds=horizon_seconds,
                reason_codes=["RANGE_REGIME_FLAT", reason],
            )
        return None
