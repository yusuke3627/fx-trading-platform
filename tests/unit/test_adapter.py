"""MT5 fetch failure (None) vs confirmed absence (empty tuple) must never be
conflated: an exit that mistakes an outage for "position closed" silently
skips a live position."""
from types import SimpleNamespace

import pytest

from trading.execution.mt5.adapter import MT5ConnectionError, MT5ExecutionAdapter


def raw_short_position():
    return SimpleNamespace(
        ticket=1001,
        identifier=1001,
        symbol="USDJPY",
        type=1,  # POSITION_TYPE_SELL
        volume=1.0,
        price_open=158.84,
        sl=159.5,
        tp=0.0,
        time=1755000000,
        time_update=1755000000,
    )


class FakeMT5Positions:
    def __init__(self, positions_result) -> None:
        self._result = positions_result

    def positions_get(self, **kwargs):
        return self._result

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

    def last_error(self):
        return (-10004, "no connection")


def test_api_error_raises_instead_of_reporting_closed():
    adapter = MT5ExecutionAdapter(mt5_module=FakeMT5Positions(None))
    with pytest.raises(MT5ConnectionError):
        adapter.position("1001")


def test_confirmed_empty_result_means_position_closed():
    adapter = MT5ExecutionAdapter(mt5_module=FakeMT5Positions(()))
    assert adapter.position("1001") is None


def test_existing_position_is_mapped():
    adapter = MT5ExecutionAdapter(mt5_module=FakeMT5Positions((raw_short_position(),)))
    position = adapter.position("1001")
    assert position is not None
    assert position.broker_position_ticket == "1001"
    assert position.quantity == 1000
