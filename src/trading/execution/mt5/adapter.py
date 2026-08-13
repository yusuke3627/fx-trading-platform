"""MT5 execution adapter.

Thin, typed wrapper over the MetaTrader5 Python module. The module is
injectable so preflight and OMS logic can be exercised with a fake off
Windows; on the trading host the real package is imported lazily.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading.domain.account import AccountMode, AccountTradeMode
from trading.domain.fill import BrokerDeal
from trading.domain.instrument import InstrumentSpec
from trading.domain.position import BrokerPosition
from trading.execution.mt5 import mapper


class MT5NotAvailable(RuntimeError):
    pass


class MT5ConnectionError(RuntimeError):
    pass


def _load_mt5_module() -> Any:
    try:
        import MetaTrader5
    except ImportError as exc:
        raise MT5NotAvailable(
            "MetaTrader5 package is not installed (Windows-only). "
            "Install with: pip install 'fx-trading-platform[mt5]'"
        ) from exc
    return MetaTrader5


class MT5ExecutionAdapter:
    def __init__(self, mt5_module: Any | None = None) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _load_mt5_module()
        self._contract_sizes: dict[str, Decimal] = {}

    def initialize(self, **kwargs: Any) -> None:
        if not self._mt5.initialize(**kwargs):
            raise MT5ConnectionError(f"mt5.initialize failed: {self._mt5.last_error()}")

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def account_info(self) -> Any:
        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info failed: {self._mt5.last_error()}")
        return info

    def account_mode(self) -> AccountMode:
        return mapper.account_mode_from(int(self.account_info().margin_mode))

    def account_trade_mode(self) -> AccountTradeMode:
        return mapper.trade_mode_from(int(self.account_info().trade_mode))

    def select_symbol(self, symbol: str) -> bool:
        return bool(self._mt5.symbol_select(symbol, True))

    def instrument(self, symbol: str) -> InstrumentSpec:
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise MT5ConnectionError(f"symbol_info({symbol}) failed")
        spec = mapper.instrument_spec_from_symbol_info(info)
        self._contract_sizes[symbol] = spec.contract_size
        return spec

    def order_check(self, request: dict[str, Any]) -> Any:
        return self._mt5.order_check(request)

    def order_send(self, request: dict[str, Any]) -> Any:
        return self._mt5.order_send(request)

    def positions(self, symbol: str | None = None) -> list[BrokerPosition]:
        raw = (
            self._mt5.positions_get(symbol=symbol)
            if symbol
            else self._mt5.positions_get()
        )
        return [self._map_position(p) for p in (raw or [])]

    def position(self, ticket: str) -> BrokerPosition | None:
        """Fresh single-position select; the mandatory step before any exit."""
        raw = self._mt5.positions_get(ticket=int(ticket))
        if not raw:
            return None
        return self._map_position(raw[0])

    def orders(self) -> list[Any]:
        return list(self._mt5.orders_get() or [])

    def history_orders(self, start: datetime, end: datetime) -> list[Any]:
        return list(self._mt5.history_orders_get(start, end) or [])

    def history_deals(self, start: datetime, end: datetime) -> list[BrokerDeal]:
        raw = self._mt5.history_deals_get(start, end) or []
        return [
            mapper.deal_from_raw(d, self._contract_size(str(d.symbol))) for d in raw
        ]

    def _map_position(self, raw: Any) -> BrokerPosition:
        return mapper.position_from_raw(raw, self._contract_size(str(raw.symbol)))

    def _contract_size(self, symbol: str) -> Decimal:
        if symbol not in self._contract_sizes:
            self.instrument(symbol)
        return self._contract_sizes[symbol]
