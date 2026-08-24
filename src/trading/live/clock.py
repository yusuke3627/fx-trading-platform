"""The clock a live evaluation runs on."""
from __future__ import annotations

from datetime import datetime

from trading.backtest.clock import Clock, SystemClock


class CycleClock:
    """Holds still for the length of one evaluation.

    One decision is made of many reads: a strategy asks for bars, then ticks,
    then an indicator over more bars; Risk then measures the JST day and the
    rolling window against the same intent. Read from a wall clock, those land
    at different instants, so a bar closing partway through is visible to some
    of them and not to others and the decision rests on a book that never
    existed at any single moment.

    Replay does not have the problem — ReplayClock only moves when the engine
    moves it — which is exactly the property this restores for live. The runner
    opens a cycle and every read inside it answers as of that instant.
    """

    def __init__(self, source: Clock | None = None) -> None:
        self._source = source or SystemClock()
        self._frozen: datetime | None = None

    def now(self) -> datetime:
        return self._frozen if self._frozen is not None else self._source.now()

    def begin_cycle(self) -> datetime:
        """Freeze at the current instant and return it."""
        self._frozen = self._source.now()
        return self._frozen
