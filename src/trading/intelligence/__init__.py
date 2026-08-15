"""Intelligence layer: Indicator -> Feature -> Regime.

Indicators are deterministic functions of market prices; Features are inputs
directly usable by strategy decisions; Regimes are market-environment labels
composed from features. Strategies combine all three to produce signals.
"""
