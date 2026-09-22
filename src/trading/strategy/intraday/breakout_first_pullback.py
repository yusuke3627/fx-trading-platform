"""確定ブレイク後の押し目・戻りを比較する研究専用戦略。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from trading.domain.event import EventEnvelope
from trading.domain.market import TIMEFRAME_SECONDS, Bar
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal
from trading.indicators import DEFAULT_BAR_COUNT
from trading.indicators.ema import ema_series
from trading.indicators.session import Session, session_end
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
    market_span_to_calendar,
)
from trading.strategy.parameters import ResolvedStrategyParameters
from trading.strategy.spread_gate import SpreadGate


def _regime_count(params: ResolvedStrategyParameters) -> int:
    return max(
        int(params.param("breakout_lookback_bars", 20)) + 1,
        int(params.param("ema_period", 20)) + 1,
    )


def _entry_count(params: ResolvedStrategyParameters, timeframe: str) -> int:
    return max(
        DEFAULT_BAR_COUNT,
        int(params.param("atr_period", 14)) + 1,
        int(params.param("pullback_wait_seconds", 3600)) // TIMEFRAME_SECONDS[timeframe] + 1,
    )


def _continuous(bars: list[Bar]) -> bool:
    return all(left.close_time == right.start for left, right in pairwise(bars))


@dataclass
class _Breakout:
    direction: PositionDirection
    level: Decimal
    atr: Decimal
    next_start: datetime
    expires_at: datetime
    consumed: bool = False


class BreakoutFirstPullbackStrategy(Strategy):
    strategy_id = "breakout_first_pullback"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}
        self._breakouts: dict[str, _Breakout] = {}

    @classmethod
    def warmup(cls, config: StrategyConfig) -> timedelta:
        regime_tf = config.timeframes.role("regime", "1h")
        entry_tf = config.timeframes.role("entry", "5m")
        return market_span_to_calendar(max(
            max(
                _regime_count(config.params_for(symbol)) * TIMEFRAME_SECONDS[regime_tf],
                _entry_count(config.params_for(symbol), entry_tf) * TIMEFRAME_SECONDS[entry_tf],
            )
            for symbol in config.instruments or [""]
        ))

    @classmethod
    def bar_window(cls, config: StrategyConfig) -> int:
        entry_tf = config.timeframes.role("entry", "5m")
        return max(
            max(_regime_count(config.params_for(symbol)),
                _entry_count(config.params_for(symbol), entry_tf))
            for symbol in config.instruments or [""]
        )

    async def on_event(
        self, event: EventEnvelope, context: StrategyContext,
    ) -> list[StrategySignal]:
        if not event.event_type.startswith("market."):
            return []
        signals = []
        for symbol in context.config.instruments:
            signal = self._horizon_exit(context, symbol, default_horizon_seconds=14400)
            if signal is None:
                signal = self._evaluate(symbol, context)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, ctx: StrategyContext) -> StrategySignal | None:
        params = ctx.config.params_for(symbol)
        entry_tf = ctx.config.timeframes.role("entry", "5m")
        bars = list(ctx.market.bars(symbol, entry_tf, _entry_count(params, entry_tf)))
        setup = self._breakouts.get(symbol)
        signal = None
        # 60分境界で新しいH1が確定しても、既存候補の最後のM5を先に評価する。
        if setup is not None and not setup.consumed:
            signal = self._advance(symbol, ctx, setup, bars)
        self._arm(symbol, ctx, bars)
        return signal

    def _arm(self, symbol: str, ctx: StrategyContext, entry_bars: list[Bar]) -> None:
        params = ctx.config.params_for(symbol)
        regime_tf = ctx.config.timeframes.role("regime", "1h")
        bars = list(ctx.market.bars(symbol, regime_tf, _regime_count(params)))
        if not bars or self._seen.get(symbol) == bars[-1].start:
            return
        last = bars[-1]
        self._seen[symbol] = last.start
        self._breakouts.pop(symbol, None)
        age = ctx.clock.now() - last.known_at
        if not timedelta(0) <= age <= timedelta(
            seconds=int(params.param("confirmation_max_age_seconds", 5))
        ):
            return
        if len(bars) < _regime_count(params) or not _continuous(bars):
            return
        lookback = int(params.param("breakout_lookback_bars", 20))
        previous = bars[-lookback - 1:-1]
        high, low = max(bar.high for bar in previous), min(bar.low for bar in previous)
        ema_period = int(params.param("ema_period", 20))
        series = ema_series([float(bar.close) for bar in bars[-(ema_period + 1):]], ema_period)
        if last.close > high and series[-1] > series[-2]:
            direction, level = PositionDirection.LONG, high
        elif last.close < low and series[-1] < series[-2]:
            direction, level = PositionDirection.SHORT, low
        else:
            return
        atr_period = int(params.param("atr_period", 14))
        recent = entry_bars[-(atr_period + 1):]
        if (
            len(recent) < atr_period + 1 or not _continuous(recent)
            or recent[-1].close_time != last.close_time
        ):
            return
        atr = ctx.indicators.atr(symbol, ctx.config.timeframes.role("entry", "5m"), atr_period)
        if atr is None or atr <= 0:
            return
        self._breakouts[symbol] = _Breakout(
            direction=direction,
            level=level,
            atr=Decimal(str(atr)),
            next_start=last.close_time,
            expires_at=last.known_at + timedelta(seconds=int(params.param("pullback_wait_seconds", 3600))),
        )

    def _advance(
        self, symbol: str, ctx: StrategyContext, setup: _Breakout, bars: list[Bar],
    ) -> StrategySignal | None:
        if ctx.clock.now() > setup.expires_at:
            setup.consumed = True
            return None
        params = ctx.config.params_for(symbol)
        band = setup.atr * Decimal(str(params.param("retest_tolerance_atr", "0.25")))
        first_only = bool(params.param("first_pullback_only", True))
        for bar in bars:
            if bar.start < setup.next_start:
                continue
            if bar.start != setup.next_start:
                setup.consumed = True
                return None
            setup.next_start = bar.close_time
            if not bar.low <= setup.level + band or not bar.high >= setup.level - band:
                continue
            if first_only:
                setup.consumed = True
            is_long = setup.direction is PositionDirection.LONG
            confirmed = bar.close > setup.level if is_long else bar.close < setup.level
            if confirmed:
                signal = self._entry(symbol, ctx, setup, bar, band)
                if signal is not None:
                    setup.consumed = True
                    return signal
            if setup.consumed:
                return None
        return None

    def _entry(
        self, symbol: str, ctx: StrategyContext, setup: _Breakout, bar: Bar, band: Decimal,
    ) -> StrategySignal | None:
        params = ctx.config.params_for(symbol)
        now = ctx.clock.now()
        max_age = timedelta(seconds=int(params.param("confirmation_max_age_seconds", 5)))
        if not timedelta(0) <= now - bar.known_at <= max_age:
            return None
        if self._held_position(ctx, symbol) is not None or not self._session_permits_entry(ctx, symbol):
            return None
        next_close = session_end(Session.NEW_YORK, now)
        if next_close <= now:
            next_close = session_end(Session.NEW_YORK, now + timedelta(days=1))
        if next_close - now <= timedelta(
            seconds=int(params.param("expected_horizon_seconds", 14400))
            + int(params.param("rollover_entry_buffer_seconds", 60))
        ):
            return None
        spec, tick = ctx.market.instrument(symbol), ctx.market.latest_tick(symbol)
        if spec is None or tick is None:
            return None
        # 受信時刻は実UTC、quote/barの順序はbroker時計で比較する。
        if not (
            timedelta(0) <= now - tick.known_time <= max_age
            and timedelta(0) <= tick.time - bar.close_time <= max_age
        ):
            return None
        if not SpreadGate.from_params(params).allows(
            spread=tick.spread, atr=float(setup.atr), pip_size=spec.pip_size,
        ):
            return None
        buffer = setup.atr * Decimal(str(params.param("stop_buffer_atr", "0.25")))
        if setup.direction is PositionDirection.LONG:
            if not setup.level < tick.ask <= setup.level + band:
                return None
            risk = tick.ask - (bar.low - buffer)
        else:
            if not setup.level - band <= tick.bid < setup.level:
                return None
            risk = bar.high + buffer - tick.bid
        stop_pips = round(risk / spec.pip_size, 1)
        if stop_pips <= 0:
            return None
        return self.make_signal(
            ctx, symbol=symbol, direction=setup.direction, conviction=0.5,
            stop_distance_pips=stop_pips,
            take_profit_distance_pips=stop_pips * Decimal(str(params.param("take_profit_r", "2"))),
            expected_horizon_seconds=int(params.param("expected_horizon_seconds", 14400)),
            reason_codes=["H1_EMA_BREAKOUT", "BREAKOUT_LEVEL_RETEST"],
        )
