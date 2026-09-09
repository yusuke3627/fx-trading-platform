"""broker ラベルが繰り返されても、別の確定バーの setup は失われない。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.support import (
    evaluation_context,
    long_failed_breakout_bars,
    make_bar,
    short_failed_breakout_bars,
)
from trading.intelligence import features as f
from trading.strategy.base import StrategyConfig
from trading.strategy.intraday.post_event_failed_breakout import PostEventFailedBreakoutStrategy
from trading.strategy.parameters import StrategyParameters
from trading.strategy.swing import monetary_policy_convergence as swing


@pytest.mark.parametrize("side", ["short", "long"])
def test_intraday_repeated_label_rearms_but_same_publication_does_not(side):
    entry, setup = (
        short_failed_breakout_bars() if side == "short" else long_failed_breakout_bars()
    )
    ctx = evaluation_context(
        entry, setup, macro_confirmation_enabled=False,
        features={f.INTERVENTION_RISK: 0.1},
    )
    strategy = PostEventFailedBreakoutStrategy()
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._evaluate("USDJPY", ctx) is None
    entry[-2] = entry[-2].model_copy(
        update={"known_at": entry[-2].known_at + timedelta(hours=1)}
    )
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._evaluate("USDJPY", ctx) is None


@pytest.mark.parametrize("side", ["short", "long"])
def test_swing_repeated_label_rearms_but_same_publication_does_not(monkeypatch, side):
    bars = [make_bar("150", "151", "149", "150") for _ in range(4)]
    ctx = evaluation_context(bars, bars, macro_confirmation_enabled=False)
    ctx.config = StrategyConfig(
        strategy_id="monetary_policy_convergence", instruments=["USDJPY"],
        parameters=StrategyParameters(defaults={"support_lookback": 3}),
    )
    ctx.market = SimpleNamespace(instrument=ctx.market.instrument, bars=lambda *_: bars)
    strategy = swing.MonetaryPolicyConvergenceStrategy()
    monkeypatch.setattr(strategy, "_short_fundamental_gate", lambda *_: side == "short")
    monkeypatch.setattr(strategy, "_long_fundamental_gate", lambda *_: side == "long")
    monkeypatch.setattr(strategy, "_trend_up", lambda *_: True)
    monkeypatch.setattr(swing, "is_lower_high", lambda *_: True)
    monkeypatch.setattr(swing, "swing_highs", lambda *_: [1])
    monkeypatch.setattr(swing, "rolling_low", lambda *_: 152 if side == "short" else 149)
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._evaluate("USDJPY", ctx) is None
    index = 1 if side == "short" else -1
    bars[index] = bars[index].model_copy(
        update={"known_at": bars[index].known_at + timedelta(hours=1)}
    )
    assert strategy._evaluate("USDJPY", ctx) is not None
    assert strategy._evaluate("USDJPY", ctx) is None
