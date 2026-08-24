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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading.backtest.clock import Clock, SystemClock
from trading.domain.account import AccountMode, AccountTradeMode
from trading.domain.order import ExecutionSide
from trading.execution.mt5 import mapper
from trading.execution.mt5.adapter import MT5ExecutionAdapter

# How far back a deal may sit and still be treated as one this run opened.
# Wide enough to cover a slow broker round trip, short enough that positions
# the account already held are never mistaken for the preflight's own.
OWN_DEAL_LOOKBACK = timedelta(minutes=10)


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
    # Verifications that were NOT performed in this run (skipped trade cycle,
    # restart reconciliation). Listed separately so they can never read as
    # "verified": `passed` covers executed checks only.
    manual_pending: list[dict[str, str]] = field(default_factory=list)
    execution_disabled: bool = False
    finished_at: datetime | None = None

    @property
    def passed(self) -> bool:
        """All *executed* checks passed. Items in manual_pending remain
        unverified regardless of this flag."""
        return not self.execution_disabled and all(s.passed for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "passed": self.passed,
            "execution_disabled": self.execution_disabled,
            "manual_pending": self.manual_pending,
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
        adapter.account_info()
        step("terminal_connection", True)
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
        report.manual_pending.append(
            {
                "name": "trade_cycle",
                "detail": "not executed (run with --trade-cycle on a demo account)",
            }
        )

    report.manual_pending.append(
        {
            "name": "restart_reconciliation",
            "detail": (
                "operational step not performed: restart terminal + application, "
                "then verify startup reconciliation keeps trading disabled until "
                "healthy"
            ),
        }
    )

    report.finished_at = clock.now()
    return report


def _trade_cycle(adapter, spec, symbol: str, magic: int, clock: Clock, step) -> None:
    """OPEN volume_min + one step with SL/TP -> verify protection -> modify
    SL/TP -> partial REDUCE -> full CLOSE -> history check."""
    # Manual terminal orders carry magic 0: without our own nonzero magic the
    # cycle could never safely re-identify its positions from deal history.
    if magic == 0:
        step(
            "trade_cycle_open",
            False,
            detail=(
                "refused: a nonzero magic number is required so the cycle can "
                "always re-identify its own positions"
            ),
        )
        return

    # On a netting account an OPEN merges into any existing position on the
    # symbol: the cycle could then neither track its own position nor restore
    # the prior exposure. Require a flat symbol before touching it.
    if (
        adapter.account_mode() is AccountMode.NETTING
        and adapter.net_exposure(symbol) != 0
    ):
        step(
            "trade_cycle_open",
            False,
            detail=(
                "refused: netting account already has exposure on the symbol; "
                "flatten it before running the trade cycle"
            ),
        )
        return

    tick = adapter._mt5.symbol_info_tick(symbol)
    if tick is None:
        step("trade_cycle_open", False, detail="no tick available")
        return
    price = Decimal(str(tick.ask))
    sl = price - 100 * spec.pip_size
    tp = price + 100 * spec.pip_size

    # Open volume_min + one step so a real partial REDUCE is always executed
    # while the remainder stays at the broker minimum. With volume_min ==
    # volume_step (e.g. OANDA 1,000-unit accounts) a half-of-minimum reduce
    # would be skipped — and a skipped step must never count as verifying the
    # Production Gate item "Partial REDUCE verified".
    cycle_units = spec.volume_min + spec.volume_step
    open_request = mapper.market_order_request(
        symbol=symbol,
        side=ExecutionSide.BUY,
        units=cycle_units,
        spec=spec,
        stop_loss=sl,
        take_profit=tp,
        magic=magic,
        comment="preflight-cycle",
    )
    result = adapter.order_send(open_request)
    retcode = getattr(result, "retcode", None)
    # DONE_PARTIAL still leaves a live position; it must flow through the
    # normal verify/close path, never be dropped as a failure.
    opened = retcode in (mapper.TRADE_RETCODE_DONE, mapper.TRADE_RETCODE_DONE_PARTIAL)
    step("trade_cycle_open", opened, {"retcode": retcode})
    if not opened:
        _cleanup_leftover_position(
            adapter, spec, symbol, magic, result, clock, step, "trade_cycle_cleanup"
        )
        return

    # An MT5 market order's position ticket equals the opening order ticket.
    # Select exactly that position: guessing (e.g. "latest position on the
    # symbol") could touch a pre-existing position the cycle did not open.
    order_ticket = str(getattr(result, "order", 0) or 0)
    position = None
    if order_ticket != "0":
        # The position can be transiently invisible right after the fill.
        for _ in range(3):
            position = adapter.position(order_ticket)
            if position is not None:
                break
            time.sleep(1)
    if position is None:
        # The order DID succeed: whatever it created must not stay open just
        # because identification failed.
        _cleanup_leftover_ticket(
            adapter, spec, symbol, magic, order_ticket, clock, step, "trade_cycle_cleanup"
        )
        step(
            "trade_cycle_protection_verify",
            False,
            detail=(
                "opened position not identified from order ticket; cleanup "
                "attempted — verify manually that no position remains"
            ),
        )
        return
    # Both SL and TP must come back as stored values from a fresh select —
    # "SL present" alone would let a dropped TP pass as verified.
    tolerance = spec.pip_size
    protection_ok = (
        position.stop_loss is not None
        and position.take_profit is not None
        and abs(position.stop_loss - sl) < tolerance
        and abs(position.take_profit - tp) < tolerance
    )
    step(
        "trade_cycle_protection_verify",
        protection_ok,
        {
            "ticket": position.broker_position_ticket,
            "identifier": position.broker_position_identifier,
            "requested_sl": str(sl),
            "stored_sl": str(position.stop_loss) if position.stop_loss else None,
            "requested_tp": str(tp),
            "stored_tp": str(position.take_profit) if position.take_profit else None,
        },
        None if protection_ok else "broker did not persist the requested SL/TP",
    )

    new_sl = sl - 10 * spec.pip_size
    modify_result = adapter.order_send(
        mapper.sltp_modify_request(
            symbol=symbol,
            position_ticket=position.broker_position_ticket,
            stop_loss=new_sl,
            take_profit=tp,
        )
    )
    # retcode alone does not prove persistence; re-select and compare BOTH
    # values — a dropped TP must fail the step just like a dropped SL.
    modified = adapter.position(position.broker_position_ticket)
    modify_ok = (
        modify_result is not None
        and modify_result.retcode == mapper.TRADE_RETCODE_DONE
        and modified is not None
        and modified.stop_loss is not None
        and abs(modified.stop_loss - new_sl) < tolerance
        and modified.take_profit is not None
        and abs(modified.take_profit - tp) < tolerance
    )
    step(
        "trade_cycle_sltp_modify",
        modify_ok,
        {
            "retcode": getattr(modify_result, "retcode", None),
            "requested_sl": str(new_sl),
            "stored_sl": (
                str(modified.stop_loss)
                if modified is not None and modified.stop_loss is not None
                else None
            ),
            "requested_tp": str(tp),
            "stored_tp": (
                str(modified.take_profit)
                if modified is not None and modified.take_profit is not None
                else None
            ),
        },
    )

    reduce_units = spec.volume_step
    reduce_request = mapper.market_order_request(
        symbol=symbol,
        side=ExecutionSide.SELL,
        units=reduce_units,
        spec=spec,
        position_ticket=position.broker_position_ticket,
        magic=magic,
        comment="preflight-reduce",
    )
    reduce_result = adapter.order_send(reduce_request)
    step(
        "trade_cycle_partial_reduce",
        reduce_result is not None
        and reduce_result.retcode == mapper.TRADE_RETCODE_DONE,
        {
            "retcode": getattr(reduce_result, "retcode", None),
            "reduced_units": str(reduce_units),
        },
    )

    fresh = adapter.position(position.broker_position_ticket)
    if fresh is None:
        step("trade_cycle_close", False, detail="position vanished before close")
        return
    # A CLOSE can itself fill partially (DONE_PARTIAL): keep closing until
    # the fresh select confirms the position is gone, so the preflight never
    # leaves exposure on the demo account.
    last_retcode = None
    for _ in range(4):
        if fresh is None:
            break
        close_result = adapter.order_send(
            mapper.market_order_request(
                symbol=symbol,
                side=ExecutionSide.SELL,
                units=fresh.quantity,
                spec=spec,
                position_ticket=fresh.broker_position_ticket,
                magic=magic,
                comment="preflight-close",
            )
        )
        last_retcode = getattr(close_result, "retcode", None)
        if last_retcode not in (
            mapper.TRADE_RETCODE_DONE,
            mapper.TRADE_RETCODE_DONE_PARTIAL,
        ):
            break
        fresh = adapter.position(position.broker_position_ticket)
    flat = fresh is None
    step(
        "trade_cycle_close",
        flat,
        {
            "retcode": last_retcode,
            "leftover_units": "0" if flat else str(fresh.quantity),
        },
        None if flat else "position not fully closed; leftover remains on the demo account",
    )

    # Deals carry the lifecycle identifier, not the current ticket.
    cycle_deals = adapter.history_deals_for_position(
        position.broker_position_identifier
    )
    step(
        "trade_cycle_history_check",
        len(cycle_deals) >= 2,
        {
            "deals": len(cycle_deals),
            "reason_codes": sorted({d.reason_code for d in cycle_deals if d.reason_code is not None}),
        },
    )

    _protection_fill_probe(adapter, spec, symbol, magic, clock, step)


def _cleanup_leftover_position(
    adapter, spec, symbol: str, magic: int, result, clock: Clock, step, step_name: str
) -> None:
    ticket = str(getattr(result, "order", 0) or 0) if result is not None else "0"
    _cleanup_leftover_ticket(adapter, spec, symbol, magic, ticket, clock, step, step_name)


def _own_position_tickets_from_history(
    adapter, symbol: str, magic: int, clock: Clock
) -> list[str]:
    """Re-identify positions this preflight created via its own magic number
    — the safe lookup when the broker result lacks an order id (a foreign
    position can never match our magic).

    Magic alone is not enough to identify them: the account's own trading uses
    the same magic, and the adapter widens the window past the broker's
    server-time offset, so older deals come back too. Recency is therefore
    judged against the broker's clock, the only reading in the same time zone
    as the deals. Cutting the candidates down here matters because whatever
    survives gets a close order sent to it.
    """
    deals = adapter.history_deals(
        clock.now() - OWN_DEAL_LOOKBACK, clock.now() + timedelta(minutes=1)
    )
    cutoff = adapter.broker_time(symbol) - OWN_DEAL_LOOKBACK
    return sorted(
        {
            d.broker_position_identifier
            for d in deals
            if d.magic == magic
            and d.broker_position_identifier
            and d.broker_time >= cutoff
        }
    )


def _close_until_flat(adapter, spec, symbol: str, magic: int, ticket: str):
    """Close one ticket until a fresh select confirms it is gone. Returns
    (leftover, found): leftover is the still-open position on failure."""
    leftover = adapter.position(ticket)
    if leftover is None or leftover.symbol != symbol:
        return None, False
    for _ in range(4):
        if leftover is None:
            return None, True
        # All preflight opens are BUY, so cleanup always sells.
        close_result = adapter.order_send(
            mapper.market_order_request(
                symbol=symbol,
                side=ExecutionSide.SELL,
                units=leftover.quantity,
                spec=spec,
                position_ticket=ticket,
                magic=magic,
                comment="preflight-cleanup",
            )
        )
        retcode = getattr(close_result, "retcode", None)
        if retcode not in (
            mapper.TRADE_RETCODE_DONE,
            mapper.TRADE_RETCODE_DONE_PARTIAL,
        ):
            break
        leftover = adapter.position(ticket)
    return leftover, True


def _cleanup_leftover_ticket(
    adapter, spec, symbol: str, magic: int, ticket: str, clock: Clock, step, step_name: str
) -> None:
    """A failed or partial order may still have left a position on the demo
    account; keep closing until a fresh select confirms it is gone. A cleanup
    that leaves exposure is itself a failure, never a silent success. With no
    order id in the result, own positions are re-identified from recent deals
    (magic) instead of being abandoned."""
    if ticket != "0":
        tickets = [ticket]
    else:
        if magic == 0:
            # Manual terminal orders also carry magic 0: re-identification
            # would risk closing a position the preflight never opened.
            step(
                step_name,
                False,
                detail=(
                    "cannot re-identify without a nonzero magic; verify "
                    "manually that no position remains"
                ),
            )
            return
        tickets = _own_position_tickets_from_history(adapter, symbol, magic, clock)
        if not tickets:
            step(
                step_name,
                True,
                detail="no own position found via magic re-identification",
            )
            return
    leftovers = []
    found_any = False
    for candidate in tickets:
        leftover, found = _close_until_flat(adapter, spec, symbol, magic, candidate)
        found_any = found_any or found
        if leftover is not None:
            leftovers.append(leftover)
    if not found_any:
        return
    flat = not leftovers
    step(
        step_name,
        flat,
        {
            "leftover_units": (
                "0" if flat else str(sum((p.quantity for p in leftovers), Decimal(0)))
            )
        },
        None if flat else "cleanup failed: leftover position remains on the demo account",
    )


def _protection_fill_probe(
    adapter, spec, symbol: str, magic: int, clock: Clock, step, wait_seconds: int = 90
) -> None:
    """Verify PROTECTION_FILL with a real broker-side SL execution.

    The normal trade cycle produces only client-origin deals, so it can never
    prove protection classification. This probe opens a minimum position with
    the tightest legal SL and waits for the broker to fire it. If no SL/TP/SO
    deal is observed within the window, the step FAILS as unverified — a
    skipped protection check must never read as a verified Production Gate
    item.
    """
    tick = adapter._mt5.symbol_info_tick(symbol)
    if tick is None:
        step("trade_cycle_protection_fill", False, detail="no tick available")
        return
    point = Decimal(10) ** -spec.digits
    distance = max(spec.stop_level_points, 20) * point
    stop_loss = Decimal(str(tick.bid)) - distance

    result = adapter.order_send(
        mapper.market_order_request(
            symbol=symbol,
            side=ExecutionSide.BUY,
            units=spec.volume_min,
            spec=spec,
            stop_loss=stop_loss,
            magic=magic,
            comment="preflight-protection",
        )
    )
    probe_retcode = getattr(result, "retcode", None)
    if probe_retcode not in (
        mapper.TRADE_RETCODE_DONE,
        mapper.TRADE_RETCODE_DONE_PARTIAL,
    ):
        step("trade_cycle_protection_fill", False, {"retcode": probe_retcode})
        _cleanup_leftover_position(
            adapter, spec, symbol, magic, result, clock, step, "trade_cycle_protection_cleanup"
        )
        return
    ticket = str(getattr(result, "order", 0) or 0)
    if ticket == "0":
        step("trade_cycle_protection_fill", False, detail="opened position not identified")
        # The order DID fill: without an order id the position must still be
        # re-identified via magic and flattened, exactly like the trade cycle.
        _cleanup_leftover_ticket(
            adapter, spec, symbol, magic, ticket, clock, step, "trade_cycle_protection_cleanup"
        )
        return

    # Deals reference the lifecycle identifier, so capture it while the
    # position still exists (identifier == opening order ticket as fallback).
    # Retry: an empty select right after the fill can be transient, and
    # mistaking it for "SL fired" would skip the wait loop.
    probed = None
    for _ in range(3):
        probed = adapter.position(ticket)
        if probed is not None:
            break
        time.sleep(1)
    identifier = probed.broker_position_identifier if probed is not None else ticket

    deadline = time.monotonic() + wait_seconds
    fired = probed is None
    while not fired and time.monotonic() < deadline:
        if adapter.position(ticket) is None:
            fired = True
            break
        time.sleep(2)

    if not fired:
        _cleanup_leftover_ticket(
            adapter, spec, symbol, magic, ticket, clock, step, "trade_cycle_protection_cleanup"
        )
        step(
            "trade_cycle_protection_fill",
            False,
            detail=(
                f"unverified: broker-side SL did not fire within {wait_seconds}s; "
                "PROTECTION_FILL remains unverified"
            ),
        )
        return

    protection_deals = [
        d
        for d in adapter.history_deals_for_position(identifier)
        if d.protection_reason is not None
    ]
    step(
        "trade_cycle_protection_fill",
        bool(protection_deals),
        {
            "reason_codes": sorted(
                {d.reason_code for d in protection_deals if d.reason_code is not None}
            )
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
