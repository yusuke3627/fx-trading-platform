from decimal import Decimal
from types import SimpleNamespace

from tests.support import T0, FixedClock, usdjpy_spec
from trading.domain.account import AccountMode
from trading.domain.fill import BrokerDeal
from trading.domain.order import ExecutionSide
from trading.domain.position import BrokerPosition, PositionDirection
from trading.execution.mt5 import mapper
from trading.execution.mt5.adapter import MT5ExecutionAdapter
from trading.execution.mt5.preflight import _protection_fill_probe, run_preflight


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


class MissingOrderIdProbeAdapter:
    """Probe path where the OPEN fills (DONE) but the broker result carries no
    order id: cleanup must re-identify the position via magic and flatten."""

    def __init__(self, magic: int) -> None:
        self._mt5 = SimpleNamespace(
            symbol_info_tick=lambda symbol: SimpleNamespace(bid=158.840, ask=158.844)
        )
        self._positions = {
            "777": BrokerPosition(
                broker_position_ticket="777",
                broker_position_identifier="777",
                symbol="USDJPY",
                direction=PositionDirection.LONG,
                quantity=Decimal(1000),
                entry_price=Decimal("158.840"),
                observed_at=T0,
            )
        }
        self._deals = [
            BrokerDeal(
                broker_deal_id="d1",
                broker_position_identifier="777",
                magic=magic,
                side=ExecutionSide.BUY,
                quantity=Decimal(1000),
                price=Decimal("158.840"),
                broker_time=T0,
            )
        ]

    def order_send(self, request):
        if "position" in request:
            self._positions.pop(str(request["position"]), None)
            return SimpleNamespace(retcode=mapper.TRADE_RETCODE_DONE, order=888)
        return SimpleNamespace(retcode=mapper.TRADE_RETCODE_DONE, order=0)

    def position(self, ticket):
        return self._positions.get(ticket)

    def history_deals(self, from_time, to_time):
        return self._deals


def test_protection_probe_cleans_up_when_order_id_missing():
    adapter = MissingOrderIdProbeAdapter(magic=42)
    steps = []

    def record(name, passed, measured=None, detail=None):
        steps.append((name, passed, measured, detail))

    _protection_fill_probe(
        adapter, usdjpy_spec(), "USDJPY", 42, FixedClock(), record
    )

    names = [s[0] for s in steps]
    assert "trade_cycle_protection_fill" in names
    cleanup = next(s for s in steps if s[0] == "trade_cycle_protection_cleanup")
    assert cleanup[1] is True
    assert cleanup[2] == {"leftover_units": "0"}
    # The position opened by the probe really is gone from the account.
    assert adapter.position("777") is None
