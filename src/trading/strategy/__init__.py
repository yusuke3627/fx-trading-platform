"""Strategy layer.

Directories are classified by trading horizon (scalp / intraday / swing),
not by candle timeframe. One file = one strategy definition with one
canonical strategy_id. A strategy may consume multiple timeframes; the
timeframe selection lives in strategy configuration.
"""
