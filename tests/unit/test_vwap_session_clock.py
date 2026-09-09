"""session VWAP は broker のラベルを実時刻へ戻してから窓を選ぶ。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.support import make_bar
from trading.data.market.dukascopy import known_to_broker_label
from trading.indicators import IndicatorService
from trading.indicators.session import Session


@pytest.mark.parametrize("month,utc_hour", [(1, 8), (7, 7), (3, 8)])
@pytest.mark.parametrize("anchor", [7.0, 9.0])
def test_london_session_includes_only_bars_after_real_open(month, utc_hour, anchor):
    # 3月20日は米国のみ夏時間。英国の切替日を米国と同一視しない。
    opening = datetime(2026, month, 20, utc_hour, tzinfo=UTC)
    bars = [
        make_bar(
            str(price), str(price), str(price), str(price),
            start=known_to_broker_label(instant, timedelta(hours=anchor)),
            known_at=instant + timedelta(minutes=1),
            tick_volume=1,
        )
        for instant, price in [
            (opening - timedelta(minutes=1), 90),
            (opening, 100),
            (opening + timedelta(minutes=1), 102),
        ]
    ]
    market = SimpleNamespace(bars=lambda *_: bars)
    service = IndicatorService(market, broker_server_ahead_of_ny_hours=anchor)
    assert service.vwap("USDJPY", session=Session.LONDON) == 101.0
    assert service.vwap("USDJPY") == pytest.approx(292 / 3)


def test_delayed_publication_does_not_move_the_vwap_session():
    opening = datetime(2026, 7, 20, 7, tzinfo=UTC)
    bars = [
        make_bar(
            str(price), str(price), str(price), str(price),
            start=known_to_broker_label(instant, timedelta(hours=7)),
            known_at=opening + timedelta(days=1), tick_volume=1,
        )
        for instant, price in [(opening - timedelta(minutes=1), 90), (opening, 100)]
    ]
    service = IndicatorService(SimpleNamespace(bars=lambda *_: bars))
    assert service.vwap("USDJPY", session=Session.LONDON) == 100


def test_live_wiring_uses_the_configured_broker_anchor():
    from tests.unit.test_live_wiring import config_with, services
    from trading.live.wiring import build_runner

    config = config_with("post_event_failed_breakout")
    config.market = config.market.model_copy(update={"broker_server_ahead_of_ny_hours": 9})
    inputs = services()
    opening = datetime(2026, 7, 20, 7, tzinfo=UTC)
    bars = [
        make_bar(
            str(price), str(price), str(price), str(price), tick_volume=1,
            start=known_to_broker_label(instant, timedelta(hours=9)),
            known_at=instant + timedelta(minutes=1),
        )
        for instant, price in [(opening - timedelta(minutes=1), 90), (opening, 100)]
    ]
    inputs["market"] = SimpleNamespace(bars=lambda *_: bars)
    runner = build_runner(config, **inputs)
    assert runner.bindings[0].context.indicators.vwap("USDJPY", session=Session.LONDON) == 100
