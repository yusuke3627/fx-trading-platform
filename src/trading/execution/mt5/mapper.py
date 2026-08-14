"""Domain <-> MT5 mapping.

MT5 integer constants are duplicated here (stable published values) so pure
mapping logic stays testable off-Windows; the preflight verifies them against
the live module on demo connection day.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.domain.account import AccountMode, AccountTradeMode
from trading.domain.fill import BrokerDeal, ProtectionReason
from trading.domain.instrument import InstrumentSpec
from trading.domain.order import ExecutionSide
from trading.domain.position import BrokerPosition, PositionDirection

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6

ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# Only these deal types are trade executions; balance/credit/commission etc.
# carry no (or an empty) symbol and must never reach deal_from_raw.
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

DEAL_REASON_CLIENT = 0
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010

_PROTECTION_REASONS = {
    DEAL_REASON_SL: ProtectionReason.STOP_LOSS,
    DEAL_REASON_TP: ProtectionReason.TAKE_PROFIT,
    DEAL_REASON_SO: ProtectionReason.STOP_OUT,
}

_MARGIN_MODES = {
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING: AccountMode.NETTING,
    ACCOUNT_MARGIN_MODE_EXCHANGE: AccountMode.EXCHANGE,
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: AccountMode.HEDGING,
}

_TRADE_MODES = {
    ACCOUNT_TRADE_MODE_DEMO: AccountTradeMode.DEMO,
    ACCOUNT_TRADE_MODE_CONTEST: AccountTradeMode.CONTEST,
    ACCOUNT_TRADE_MODE_REAL: AccountTradeMode.REAL,
}


def account_mode_from(margin_mode: int) -> AccountMode:
    if margin_mode not in _MARGIN_MODES:
        raise ValueError(f"unknown ACCOUNT_MARGIN_MODE: {margin_mode}")
    return _MARGIN_MODES[margin_mode]


def trade_mode_from(trade_mode: int) -> AccountTradeMode:
    if trade_mode not in _TRADE_MODES:
        raise ValueError(f"unknown ACCOUNT_TRADE_MODE: {trade_mode}")
    return _TRADE_MODES[trade_mode]


def protection_reason_from(reason_code: int | None) -> ProtectionReason | None:
    if reason_code is None:
        return None
    return _PROTECTION_REASONS.get(reason_code)


def side_to_order_type(side: ExecutionSide) -> int:
    return ORDER_TYPE_BUY if side is ExecutionSide.BUY else ORDER_TYPE_SELL


def units_to_lots(units: Decimal, contract_size: Decimal) -> float:
    return float(units / contract_size)


def lots_to_units(lots: float, contract_size: Decimal) -> Decimal:
    return Decimal(str(lots)) * contract_size


def pip_size_for_digits(digits: int) -> Decimal:
    """Conventional pip size: 0.01 for JPY-style 2/3-digit quotes, 0.0001 for
    4/5-digit quotes. Point-based formulas break on 4/2-digit feeds (a pip is
    10 points only on 3/5-digit quotes), so digits are mapped explicitly."""
    if digits in (2, 3):
        return Decimal("0.01")
    if digits in (4, 5):
        return Decimal("0.0001")
    raise ValueError(f"unsupported quote digits for pip derivation: {digits}")


def instrument_spec_from_symbol_info(info: Any) -> InstrumentSpec:
    """Build an InstrumentSpec from mt5.symbol_info() output.

    MT5 volumes are lots; the spec carries units.
    """
    digits = int(info.digits)
    contract = Decimal(str(info.trade_contract_size))
    return InstrumentSpec(
        symbol=str(info.name),
        digits=digits,
        pip_size=pip_size_for_digits(digits),
        contract_size=contract,
        volume_min=lots_to_units(info.volume_min, contract),
        volume_step=lots_to_units(info.volume_step, contract),
        volume_max=lots_to_units(info.volume_max, contract),
        stop_level_points=int(getattr(info, "trade_stops_level", 0)),
    )


def market_order_request(
    *,
    symbol: str,
    side: ExecutionSide,
    units: Decimal,
    spec: InstrumentSpec,
    stop_loss: Decimal | None = None,
    take_profit: Decimal | None = None,
    position_ticket: str | None = None,
    deviation_points: int = 10,
    magic: int = 0,
    comment: str = "",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "action": TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": units_to_lots(units, spec.contract_size),
        "type": side_to_order_type(side),
        "deviation": deviation_points,
        "magic": magic,
        "comment": comment,
    }
    if stop_loss is not None:
        request["sl"] = float(stop_loss)
    if take_profit is not None:
        request["tp"] = float(take_profit)
    if position_ticket is not None:
        request["position"] = int(position_ticket)
    return request


def sltp_modify_request(
    *,
    symbol: str,
    position_ticket: str,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "action": TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": int(position_ticket),
    }
    if stop_loss is not None:
        request["sl"] = float(stop_loss)
    if take_profit is not None:
        request["tp"] = float(take_profit)
    return request


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def position_from_raw(raw: Any, contract_size: Decimal) -> BrokerPosition:
    direction = (
        PositionDirection.LONG
        if int(raw.type) == POSITION_TYPE_BUY
        else PositionDirection.SHORT
    )
    return BrokerPosition(
        broker_position_ticket=str(raw.ticket),
        broker_position_identifier=str(getattr(raw, "identifier", raw.ticket)),
        symbol=str(raw.symbol),
        direction=direction,
        quantity=lots_to_units(raw.volume, contract_size),
        entry_price=Decimal(str(raw.price_open)),
        stop_loss=Decimal(str(raw.sl)) if raw.sl else None,
        take_profit=Decimal(str(raw.tp)) if raw.tp else None,
        observed_at=_utc(raw.time_update) if getattr(raw, "time_update", 0) else _utc(raw.time),
    )


def is_trade_deal(raw: Any) -> bool:
    return int(getattr(raw, "type", -1)) in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)


def deal_from_raw(raw: Any, contract_size: Decimal) -> BrokerDeal:
    reason = int(getattr(raw, "reason", DEAL_REASON_CLIENT))
    side = ExecutionSide.BUY if int(raw.type) == DEAL_TYPE_BUY else ExecutionSide.SELL
    return BrokerDeal(
        broker_deal_id=str(raw.ticket),
        broker_order_id=str(raw.order) if getattr(raw, "order", 0) else None,
        # A deal carries only the position lifecycle identifier; the current
        # position ticket is not derivable from it (netting can change the
        # ticket), so it is never copied into the ticket field.
        broker_position_ticket=None,
        broker_position_identifier=(
            str(raw.position_id) if getattr(raw, "position_id", 0) else None
        ),
        reason_code=reason,
        protection_reason=protection_reason_from(reason),
        side=side,
        quantity=lots_to_units(raw.volume, contract_size),
        price=Decimal(str(raw.price)),
        broker_time=_utc(raw.time),
    )
