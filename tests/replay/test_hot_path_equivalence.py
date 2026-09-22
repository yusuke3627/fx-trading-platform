"""origin/main 4b4b09d の全結果・CSV・入力 hash を固定した退行テスト。"""
import hashlib
import json
from contextlib import ExitStack
from datetime import timedelta

import pytest

from tests.replay.hot_path_inputs import (
    ANCHOR,
    canonical,
    input_ticks,
    make_engine,
    strategy_config,
)
from trading.backtest.data import TickDigest
from trading.backtest.research import (
    BAR_CSV_HEADER,
    broker_label_to_known,
    reconstructed_tick,
    write_bar,
)


# 本体変更前に main のコードで各 2 回算出して一致を確認した値。
# Decimal の桁や float の bit を丸めず、BacktestResult の全フィールドを含める。
@pytest.mark.parametrize("dataset,strategy,expected,fill_count,trade_count", [
    ("synthetic", "failed_spike_reversal",
     "f0bae01d5386d8e754f7e53a1e6fc0ec0127507379334d409b852b2ca7dcb110", 47, 23),
    ("synthetic", "range_edge_reversal",
     "c0ce6ab5b0b5a5cb83b3a30f89015b223a3147043043f1467057ec32e0d5dfdc", 0, 0),
    ("bi5", "failed_spike_reversal",
     "e830f854e5a96bf3fe26ddcccd1ca4008b553448e952d7cb25a3e3544979e82a", 146, 73),
    ("bi5", "range_edge_reversal",
     "95cbeb28b43360d2be6388f4c4eef497d11392728a3596d6fc8e58844e8aebc2", 2, 1),
])
def test_complete_result_and_bar_csv_match_main(
    tmp_path, dataset, strategy, expected, fill_count, trade_count,
):
    ticks = input_ticks(dataset)
    digest = TickDigest()

    def stream():
        for tick in ticks:
            item = reconstructed_tick(tick, ANCHOR)
            digest.update(item)
            yield item

    engine = make_engine(
        strategy, broker_label_to_known(ticks[0].time, ANCHOR) + timedelta(minutes=30),
    )
    with ExitStack() as stack:
        files = {
            tf: stack.enter_context(
                (tmp_path / f"bars_{tf}.csv").open("w", encoding="utf-8", newline="")
            )
            for tf in strategy_config(strategy).timeframes.all()
        }
        for out in files.values():
            out.write(BAR_CSV_HEADER)
        result = engine.run_stream(stream(), on_bar=lambda bar: write_bar(bar, files[bar.timeframe]))

    payload = {
        "result": canonical(result),
        "bars": {
            path.name: path.read_bytes().decode("utf-8")
            for path in sorted(tmp_path.glob("bars_*.csv"))
        },
        "ticks": digest.hexdigest(),
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    assert digest.count == (12_000 if dataset == "synthetic" else 33_054)
    assert len(result.fills) == fill_count
    assert len(result.trades) == trade_count
    assert hashlib.sha256(encoded).hexdigest() == expected
