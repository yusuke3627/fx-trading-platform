"""Broker tests: run on the Windows host against an OANDA demo account.

These measure real broker behavior (account mode, SL persistence, ticket and
identifier behavior, partial reduction, protection fill reasons) and feed the
Production Gate checklist.
"""
import importlib.util

import pytest

mt5_available = importlib.util.find_spec("MetaTrader5") is not None

pytestmark = pytest.mark.skipif(
    not mt5_available, reason="MetaTrader5 package not installed (Windows-only)"
)


def test_preflight_against_demo():
    from trading.config import load_config
    from trading.domain.account import AccountMode
    from trading.execution.mt5.adapter import MT5ExecutionAdapter
    from trading.execution.mt5.preflight import run_preflight

    config = load_config("demo")
    report = run_preflight(
        MT5ExecutionAdapter(),
        expected_mode=AccountMode(config.broker.expected_account_mode),
        symbol=config.market.primary_instruments[0],
        allow_trade_cycle=False,
    )
    assert report.passed, [s for s in report.steps if not s.passed]
