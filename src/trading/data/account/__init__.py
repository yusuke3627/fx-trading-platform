"""Account state observed from the broker.

Equity, balance and margin as the terminal reports them, appended to the
point-in-time store. Risk reads that series to measure loss; nothing here
interprets it.
"""
