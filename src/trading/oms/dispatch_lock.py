"""単一 OMS dispatcher の所有権契約（ADR-032）。

送信は 1 process に限る。process ごとに独立した RateLimiter が同時に送ると
broker 側の上限（同一 symbol 5 requests/sec、market new entry 1/sec）を超えるため、
ExecutionQueue はこの lock を保持している間だけ dispatch する。
"""
from __future__ import annotations

from typing import Protocol


class DispatcherNotHeldError(RuntimeError):
    """単一 dispatcher の所有権なしに送信しようとした。"""


class DispatchLock(Protocol):
    def held(self) -> bool: ...
