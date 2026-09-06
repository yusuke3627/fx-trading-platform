"""One setup emits exactly one signal even though every market event
re-evaluates the same closed bars."""
from tests.support import at
from trading.domain.position import PositionDirection
from trading.strategy.base import Strategy, StrategyHorizon


class DedupeProbe(Strategy):
    strategy_id = "dedupe_probe"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.INTRADAY

    async def on_event(self, event, context):
        return []


def test_same_setup_signals_once():
    probe = DedupeProbe()
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is True
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is False


def test_new_setup_rearms():
    probe = DedupeProbe()
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is True
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=2)) is True


def test_slots_are_per_symbol_and_direction():
    probe = DedupeProbe()
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is True
    assert probe._new_setup("USDJPY", PositionDirection.LONG, at(hours=1)) is True
    assert probe._new_setup("EURUSD", PositionDirection.SHORT, at(hours=1)) is True
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is False


def test_instances_do_not_share_memo():
    first, second = DedupeProbe(), DedupeProbe()
    assert first._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is True
    assert second._new_setup("USDJPY", PositionDirection.SHORT, at(hours=1)) is True


def test_exit_only_setups_dedupe_in_their_own_slot():
    probe = DedupeProbe()
    setup_id = at(hours=1)

    assert probe._new_setup(
        "USDJPY", PositionDirection.SHORT, setup_id, exit_only=True
    ) is True
    assert probe._new_setup(
        "USDJPY", PositionDirection.SHORT, setup_id, exit_only=True
    ) is False
    assert probe._new_setup("USDJPY", PositionDirection.SHORT, setup_id) is True
