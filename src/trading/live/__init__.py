"""Live trading application.

Wiring and the loops that drive it, as opposed to the replay engine in
`backtest`. The layering is the same in both: strategies read market data and
emit signals, Portfolio turns them into intents, Risk grades them.
"""
