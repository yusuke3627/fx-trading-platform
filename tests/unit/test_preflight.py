from decimal import Decimal
from types import SimpleNamespace

from trading.domain.account import AccountMode
from trading.execution.mt5.adapter import MT5ExecutionAdapter
from trading.execution.mt5.preflight import run_preflight


class FakeMT5:
    """Minimal MetaTrader5 module stand-in (margin_mode 2 = hedging)."""

    def __init__(self, margin_mode: int = 2, trade_mode: int = 0) -> None:
        self._margin_mode = margin_mode
        self._trade_mode = trade_mode

    def initialize(self, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(
            login=990001,
            margin_mode=self._margin_mode,
            trade_mode=self._trade_mode,
        )

    def symbol_select(self, symbol, enable) -> bool:
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol,
            digits=3,
            trade_contract_size=1000.0,
            volume_min=1.0,
            volume_step=1.0,
            volume_max=100.0,
            trade_stops_level=0,
        )

    def order_check(self, request):
        return SimpleNamespace(retcode=0)


def run(fake: FakeMT5, **kwargs):
    adapter = MT5ExecutionAdapter(mt5_module=fake)
    return run_preflight(
        adapter,
        expected_mode=AccountMode.HEDGING,
        symbol="USDJPY",
        **kwargs,
    )


def step(report, name):
    return next(s for s in report.steps if s.name == name)


def test_preflight_passes_on_matching_hedging_demo():
    report = run(FakeMT5(margin_mode=2, trade_mode=0))
    assert report.execution_disabled is False
    assert report.passed, [s.name for s in report.steps if not s.passed]
    spec_step = step(report, "instrument_spec")
    assert Decimal(spec_step.measured["volume_min_units"]) == 1000
    assert Decimal(spec_step.measured["pip_size"]) == Decimal("0.01")
    # Unperformed verifications are reported as pending, never as passed steps.
    pending = [m["name"] for m in report.manual_pending]
    assert "trade_cycle" in pending
    assert "restart_reconciliation" in pending


def test_account_mode_mismatch_disables_execution():
    report = run(FakeMT5(margin_mode=0))  # netting account, hedging expected
    assert report.execution_disabled is True
    assert report.passed is False
    mode_step = step(report, "account_margin_mode")
    assert mode_step.passed is False
    assert mode_step.measured["actual"] == AccountMode.NETTING


def test_trade_cycle_refuses_non_demo_account():
    report = run(FakeMT5(margin_mode=2, trade_mode=2), allow_trade_cycle=True)
    cycle_step = step(report, "trade_cycle")
    assert cycle_step.passed is False
    assert "not a demo account" in (cycle_step.detail or "")
    assert report.passed is False


def test_trade_cycle_skipped_by_default():
    report = run(FakeMT5())
    assert "trade_cycle" not in [s.name for s in report.steps]
    assert "trade_cycle" in [m["name"] for m in report.manual_pending]


def test_trade_cycle_requires_nonzero_magic():
    # Manual terminal orders carry magic 0: without our own magic the cycle
    # could never safely re-identify its positions.
    report = run(FakeMT5(margin_mode=2, trade_mode=0), allow_trade_cycle=True)
    open_step = step(report, "trade_cycle_open")
    assert open_step.passed is False
    assert "magic" in (open_step.detail or "")
