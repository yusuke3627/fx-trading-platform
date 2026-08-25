"""The fundamental gates read the features the platform actually produces.

The gates once read expectation-series names nothing computed, so they could
never open (docs/research/2026-08-15, principle 6). These pin them to the
proxy names StoredFeatureSource fills, and pin the doctrine that a missing
feature closes a gate rather than defaulting.
"""
from types import SimpleNamespace

from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RegimeLabel, RuleBasedRegimeService
from trading.strategy.intraday.post_event_failed_breakout import (
    PostEventFailedBreakoutStrategy,
)
from trading.strategy.swing.monetary_policy_convergence import (
    MonetaryPolicyConvergenceStrategy,
)


def ctx_with(values: dict[str, float]) -> SimpleNamespace:
    store = InMemoryFeatureStore()
    for name, value in values.items():
        store.set(name, value)
    return SimpleNamespace(features=store)


def test_swing_short_gate_opens_on_dovish_fed_hawkish_boj_and_intervention():
    ctx = ctx_with(
        {
            f.FED_POLICY_SHIFT_SCORE: -1.0,
            f.BOJ_POLICY_SHIFT_SCORE: 0.5,
            f.INTERVENTION_RISK: 0.4,
        }
    )
    assert MonetaryPolicyConvergenceStrategy._short_fundamental_gate(ctx, 0.2)

    # Any missing leg closes the gate.
    for missing in (f.FED_POLICY_SHIFT_SCORE, f.BOJ_POLICY_SHIFT_SCORE, f.INTERVENTION_RISK):
        values = {
            f.FED_POLICY_SHIFT_SCORE: -1.0,
            f.BOJ_POLICY_SHIFT_SCORE: 0.5,
            f.INTERVENTION_RISK: 0.4,
        }
        del values[missing]
        assert not MonetaryPolicyConvergenceStrategy._short_fundamental_gate(
            ctx_with(values), 0.2
        )


def test_swing_long_gate_opens_on_hawkish_fed_dovish_boj():
    ctx = ctx_with({f.FED_POLICY_SHIFT_SCORE: 1.0, f.BOJ_POLICY_SHIFT_SCORE: -0.5})
    assert MonetaryPolicyConvergenceStrategy._long_fundamental_gate(ctx)
    assert not MonetaryPolicyConvergenceStrategy._long_fundamental_gate(ctx_with({}))


def test_intraday_short_gate_is_an_or_over_the_produced_features():
    gate = PostEventFailedBreakoutStrategy._short_macro_gate

    assert gate(ctx_with({f.US2Y_CHANGE_5D: -0.10}), eps=0.0)
    assert gate(ctx_with({f.BOJ_POLICY_SHIFT_SCORE: 0.5}), eps=0.0)
    # US_DATA_SURPRISE has no producer yet; when someone fills it, it counts.
    assert gate(ctx_with({f.US_DATA_SURPRISE: -0.5}), eps=0.0)
    # All absent: nothing to confirm on, so nothing fires.
    assert not gate(ctx_with({}), eps=0.0)


def test_intraday_long_gate_requires_both_us2y_horizons_and_calm_intervention():
    gate = PostEventFailedBreakoutStrategy._long_macro_gate
    open_values = {
        f.US2Y_CHANGE_5D: 0.10,
        f.US2Y_CHANGE_1D: 0.04,
        f.INTERVENTION_RISK: 0.1,
    }

    assert gate(ctx_with(open_values), eps=0.0, intervention_max=0.5)

    for missing in open_values:
        values = dict(open_values)
        del values[missing]
        assert not gate(ctx_with(values), eps=0.0, intervention_max=0.5)

    elevated = {**open_values, f.INTERVENTION_RISK: 0.9}
    assert not gate(ctx_with(elevated), eps=0.0, intervention_max=0.5)


def test_regime_labels_follow_the_statement_scores():
    store = InMemoryFeatureStore()
    store.set(f.FED_POLICY_SHIFT_SCORE, 1.0)
    store.set(f.BOJ_POLICY_SHIFT_SCORE, -1.0)

    active = RuleBasedRegimeService(store).active()

    assert RegimeLabel.USD_POLICY_HAWKISH in active
    assert RegimeLabel.JPY_POLICY_HAWKISH not in active
