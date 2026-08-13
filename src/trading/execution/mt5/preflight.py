"""MT5 demo preflight.

Machine-verifies broker behavior on demo connection day: account mode,
symbol/contract specs, order check, and (opt-in) a full trade cycle measuring
broker-side SL persistence, position ticket/identifier behavior, partial
reduction and protection-fill reason codes.

An account-mode mismatch against configuration sets execution_disabled — the
platform never trades on a "probably hedging" assumption. The trade cycle
refuses to run on a non-demo account.

Usage (Windows host with MT5 terminal):

    python -m trading.execution.mt5.preflight --env demo --symbol USDJPY
    python -m trading.execution.mt5.preflight --env demo --symbol USDJPY --trade-cycle
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading.backtest.clock import Clock, SystemClock
from trading.domain.account import AccountMode, AccountTradeMode
from trading.domain.order import ExecutionSide
from trading.execution.mt5 import mapper
from trading.execution.mt5.adapter import MT5ExecutionAdapter


@dataclass(frozen=True)
class StepResult:
    name: str
    passed: bool
    measured: dict[str, Any] = field(default_factory=dict)
    detail: str | None = None


@dataclass
class PreflightReport:
    started_at: datetime
    steps: list[StepResult] = field(default_factory=list)
    execution_disabled: bool = False
    finished_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return not self.execution_disabled and all(s.passed for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "passed": self.passed,
            "execution_disabled": self.execution_disabled,
            "steps": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "measured": s.measured,
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


def run_preflight(
    adapter: MT5ExecutionAdapter,
    *,
    expected_mode: AccountMode,
    symbol: str,
    allow_trade_cycle: bool = False,
    clock: Clock | None = None,
    magic: int = 0,
) -> PreflightReport:
    clock = clock or SystemClock()
    report = PreflightReport(started_at=clock.now())

    def step(name: str, passed: bool, measured: dict | None = None, detail: str | None = None):
        report.steps.append(StepResult(name, passed, measured or {}, detail))
        return passed

    # 1. Terminal connection + account info
    try:
        adapter.initialize()
        info = adapter.account_info()
        step("terminal_connection", True, {"login": getattr(info, "login", None)})
    except Exception as exc:  # noqa: BLE001 - boundary: report, don't crash
        step("terminal_connection", False, detail=str(exc))
        report.execution_disabled = True
        report.finished_at = clock.now()
        return report

    # 2. Account margin mode vs expected
    actual_mode = adapter.account_mode()
    mode_ok = actual_mode == expected_mode
    step(
        "account_margin_mode",
        mode_ok,
        {"expected": expected_mode, "actual": actual_mode},
        None if mode_ok else "EXECUTION_DISABLED: account mode mismatch",
    )
    if not mode_ok:
        report.execution_disabled = True

    # 3. Trade mode (demo gate for the trade cycle)
    trade_mode = adapter.account_trade_mode()
    step("account_trade_mode", True, {"trade_mode": trade_mode})

    # 4. Symbol visibility + contract spec
    visible = adapter.select_symbol(symbol)
    step("symbol_visibility", visible, {"symbol": symbol})
    if not visible:
        report.finished_at = clock.now()
        return report

    spec = adapter.instrument(symbol)
    step(
        "instrument_spec",
        spec.volume_min > 0 and spec.volume_step > 0,
        {
            "contract_size": str(spec.contract_size),
            "volume_min_units": str(spec.volume_min),
            "volume_step_units": str(spec.volume_step),
            "stop_level_points": spec.stop_level_points,
            "digits": spec.digits,
            "pip_size": str(spec.pip_size),
        },
    )

    # 5. Order check with minimum size (no side effect)
    check_request = mapper.market_order_request(
        symbol=symbol,
        side=ExecutionSide.BUY,
        units=spec.volume_min,
        spec=spec,
        magic=magic,
        comment="preflight-check",
    )
    check_result = adapter.order_check(check_request)
    check_ok = check_result is not None and getattr(check_result, "retcode", -1) == 0
    step(
        "order_check",
        check_ok,
        {"retcode": getattr(check_result, "retcode", None)},
    )

    # 6-13. Trade cycle (opt-in, demo only, requires matching account mode)
    if allow_trade_cycle:
        if trade_mode is not AccountTradeMode.DEMO:
            step("trade_cycle", False, detail="refused: account is not a demo account")
        elif report.execution_disabled:
            step("trade_cycle", False, detail="skipped: execution disabled")
        else:
            _trade_cycle(adapter, spec, symbol, magic, clock, step)
    else:
        step("trade_cycle", True, detail="skipped (pass --trade-cycle to run)")

    step(
        "restart_reconciliation",
        True,
        detail=(
            "operational step: restart terminal + application, then verify "
            "startup reconciliation leaves trading disabled until healthy"
        ),
    )

    report.finished_at = clock.now()
    return report


def _trade_cycle(adapter, spec, symbol: str, magic: int, clock: Clock, step) -> None:
    """OPEN min size with SL/TP -> verify protection -> modify SL/TP ->
    partial REDUCE -> full CLOSE -> history check."""
    tick = adapter._mt5.symbol_info_tick(symbol)  # noqa: SLF001 - preflight-only raw access
    if tick is None:
        step("trade_cycle_open", False, detail="no tick available")
        return
    price = Decimal(str(tick.ask))
    sl = price - 100 * spec.pip_size
    tp = price + 100 * spec.pip_size

    open_request = mapper.market_order_request(
        symbol=symbol,
        side=ExecutionSide.BUY,
        units=spec.volume_min,
        spec=spec,
        stop_loss=sl,
        take_profit=tp,
        magic=magic,
        comment="preflight-cycle",
    )
    result = adapter.order_send(open_request)
    opened = result is not None and result.retcode == mapper.TRADE_RETCODE_DONE
    step("trade_cycle_open", opened, {"retcode": getattr(result, "retcode", None)})
    if not opened:
        return

    positions = adapter.positions(symbol)
    ours = [p for p in positions if p.symbol == symbol]
    if not ours:
        step("trade_cycle_protection_verify", False, detail="opened position not found")
        return
    position = ours[-1]
    step(
        "trade_cycle_protection_verify",
        position.protected,
        {
            "ticket": position.broker_position_ticket,
            "identifier": position.broker_position_identifier,
            "sl": str(position.stop_loss) if position.stop_loss else None,
            "tp": str(position.take_profit) if position.take_profit else None,
        },
        None if position.protected else "OPEN_UNPROTECTED: broker-side SL missing",
    )

    modify = mapper.sltp_modify_request(
        symbol=symbol,
        position_ticket=position.broker_position_ticket,
        stop_loss=sl - 10 * spec.pip_size,
        take_profit=tp,
    )
    modify_result = adapter.order_send(modify)
    step(
        "trade_cycle_sltp_modify",
        modify_result is not None
        and modify_result.retcode == mapper.TRADE_RETCODE_DONE,
        {"retcode": getattr(modify_result, "retcode", None)},
    )

    # Partial reduce (half, if the step allows), then full close.
    half = (spec.volume_min / 2 // spec.volume_step) * spec.volume_step
    remaining = position.quantity
    if half >= spec.volume_step:
        reduce_request = mapper.market_order_request(
            symbol=symbol,
            side=ExecutionSide.SELL,
            units=half,
            spec=spec,
            position_ticket=position.broker_position_ticket,
            magic=magic,
            comment="preflight-reduce",
        )
        reduce_result = adapter.order_send(reduce_request)
        reduced = (
            reduce_result is not None
            and reduce_result.retcode == mapper.TRADE_RETCODE_DONE
        )
        step("trade_cycle_partial_reduce", reduced, {"retcode": getattr(reduce_result, "retcode", None)})
        if reduced:
            remaining = remaining - half
    else:
        step("trade_cycle_partial_reduce", True, detail="skipped: volume_min == volume_step")

    fresh = adapter.position(position.broker_position_ticket)
    if fresh is None:
        step("trade_cycle_close", False, detail="position vanished before close")
        return
    close_request = mapper.market_order_request(
        symbol=symbol,
        side=ExecutionSide.SELL,
        units=fresh.quantity,
        spec=spec,
        position_ticket=fresh.broker_position_ticket,
        magic=magic,
        comment="preflight-close",
    )
    close_result = adapter.order_send(close_request)
    step(
        "trade_cycle_close",
        close_result is not None and close_result.retcode == mapper.TRADE_RETCODE_DONE,
        {"retcode": getattr(close_result, "retcode", None), "closed_units": str(remaining)},
    )

    deals = adapter.history_deals(
        clock.now() - timedelta(hours=1), clock.now() + timedelta(minutes=1)
    )
    cycle_deals = [d for d in deals if d.broker_position_ticket == position.broker_position_ticket]
    step(
        "trade_cycle_history_check",
        len(cycle_deals) >= 2,
        {
            "deals": len(cycle_deals),
            "reason_codes": sorted({d.reason_code for d in cycle_deals if d.reason_code is not None}),
        },
    )


def main() -> None:
    import argparse

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="MT5 demo preflight")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--trade-cycle", action="store_true")
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]

    adapter = MT5ExecutionAdapter()
    report = run_preflight(
        adapter,
        expected_mode=AccountMode(config.broker.expected_account_mode),
        symbol=symbol,
        allow_trade_cycle=args.trade_cycle,
        magic=config.broker.magic_number,
    )
    print(json.dumps(report.to_dict(), indent=2, default=str))
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
