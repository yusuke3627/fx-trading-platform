"""Dukascopy の歴史 tick で market_ticks の収集欠損を補完する。

対象期間に既存 tick がある時間帯は取得せず、MT5 由来の系列との混在を防ぐ。
時間単位で保存するため、中断後は同じコマンドを再実行すれば未取得時間から再開できる。

Usage:

    python -m trading.data.market.dukascopy --env demo --symbol USDJPY \
        --since 2022-01-01T00:00:00Z --until 2024-07-23T00:00:00Z
"""
from __future__ import annotations

import argparse
import http.client
import lzma
import struct
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.cli import aware_utc
from trading.domain.market import Tick
from trading.storage.repository import MarketTickRepository

DATAFEED_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD_FORMAT = ">IIIff"
RECORD_SIZE = 20

# Dukascopy の point 単位は通貨ペア依存なので、対応ペアの追加時はここへ追記する。
POINT_SCALES: dict[str, Decimal] = {"USDJPY": Decimal("0.001")}

SOURCE_DUKASCOPY = "DUKASCOPY"
USER_AGENT = "fx-trading-platform-collector/1.0"
FETCH_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 5.0
REQUEST_INTERVAL_SECONDS = 0.1

_ONE_HOUR = timedelta(hours=1)
_ONE_DAY = timedelta(days=1)


def hour_url(symbol: str, hour_start: datetime) -> str:
    """Dukascopy の時間単位 bi5 URL を返す。"""
    return (
        f"{DATAFEED_URL}/{symbol}/{hour_start.year:04d}/{hour_start.month - 1:02d}/"
        f"{hour_start.day:02d}/{hour_start.hour:02d}h_ticks.bi5"
    )


def decode_bi5(
    payload: bytes,
    symbol: str,
    hour_start: datetime,
    received_at: datetime,
) -> list[Tick]:
    """LZMA 圧縮された Dukascopy の時間単位 tick を復号する。"""
    if not payload:
        return []

    data = lzma.decompress(payload)
    if len(data) % RECORD_SIZE:
        raise ValueError(
            f"invalid Dukascopy payload size: {len(data)} bytes is not divisible by "
            f"{RECORD_SIZE}"
        )

    scale = POINT_SCALES[symbol]
    return [
        Tick(
            symbol=symbol,
            bid=Decimal(bid_point) * scale,
            ask=Decimal(ask_point) * scale,
            time=hour_start + timedelta(milliseconds=msec),
            received_at=received_at,
        )
        for msec, ask_point, bid_point, _ask_volume, _bid_volume in struct.iter_unpack(
            RECORD_FORMAT, data
        )
    ]


def default_fetch(url: str) -> bytes | None:
    """時間単位 bi5 を取得し、tick のない 404 は正常な欠測として返す。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


class DukascopyTickImporter:
    """Dukascopy tick を既存系列と重ならない時間帯だけ取り込む。"""

    def __init__(
        self,
        repository: MarketTickRepository,
        *,
        fetch: Callable[[str], bytes | None] = default_fetch,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._fetch = fetch
        self._clock = clock or SystemClock()
        self._sleep = sleep
        self._ingestion_run: UUID = uuid4()

    def import_range(
        self, symbol: str, since: datetime, until: datetime
    ) -> tuple[int, int]:
        """指定した半開区間を取り込み、保存件数と失敗時間帯数を返す。"""
        total_stored = 0
        total_failed = 0
        day_start = since.replace(hour=0, minute=0, second=0, microsecond=0)

        while day_start < until:
            day_end = day_start + _ONE_DAY
            day_window_start = max(day_start, since)
            day_window_end = min(day_end, until)
            check_each_hour = (
                self._repository.bounds_between(symbol, day_window_start, day_window_end)
                is not None
            )
            day_fetched = 0
            day_stored = 0
            requested_hours = 0
            hour_start = day_start

            while hour_start < day_end:
                hour_end = hour_start + _ONE_HOUR
                window_start = max(hour_start, since)
                window_end = min(hour_end, until)
                if window_start >= window_end:
                    hour_start = hour_end
                    continue
                if (
                    check_each_hour
                    and self._repository.bounds_between(symbol, window_start, window_end)
                    is not None
                ):
                    hour_start = hour_end
                    continue

                requested_hours += 1
                url = hour_url(symbol, hour_start)
                payload: bytes | None = None
                fetch_failed = False
                for attempt in range(1, FETCH_ATTEMPTS + 1):
                    try:
                        payload = self._fetch(url)
                        break
                    # ボディ受信途中の切断は IncompleteRead（HTTPException 系で
                    # OSError ではない）になるため、OSError だけでは transient を
                    # 拾い切れない。
                    except (OSError, http.client.HTTPException) as exc:
                        if attempt == FETCH_ATTEMPTS:
                            print(
                                f"{hour_start:%Y-%m-%d %H:%M}: fetch failed after "
                                f"{FETCH_ATTEMPTS} attempts: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            fetch_failed = True
                            total_failed += 1
                        else:
                            self._sleep(RETRY_WAIT_SECONDS)

                self._sleep(REQUEST_INTERVAL_SECONDS)
                if fetch_failed or payload is None:
                    hour_start = hour_end
                    continue

                ticks = decode_bi5(payload, symbol, hour_start, self._clock.now())
                day_fetched += len(ticks)
                ticks_in_range = [tick for tick in ticks if since <= tick.time < until]
                if ticks_in_range:
                    stored = self._repository.insert_many(
                        ticks_in_range,
                        source=SOURCE_DUKASCOPY,
                        ingestion_run=self._ingestion_run,
                    )
                    day_stored += stored
                    total_stored += stored
                hour_start = hour_end

            if requested_hours == 0:
                print(
                    f"{day_start:%Y-%m-%d}: skip (existing ticks)",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"{day_start:%Y-%m-%d}: {day_fetched} ticks, +{day_stored} new",
                    file=sys.stderr,
                    flush=True,
                )
            day_start = day_end

        return total_stored, total_failed


def main() -> None:
    import os

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Dukascopy historical tick importer")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--since", type=aware_utc, required=True)
    parser.add_argument("--until", type=aware_utc, required=True)
    args = parser.parse_args()

    if args.since >= args.until:
        parser.error("--since must be earlier than --until")

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    if symbol not in POINT_SCALES:
        parser.error(f"unsupported Dukascopy symbol: {symbol}")

    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    # DB extra がない環境でもデコーダーと取り込み処理をテスト可能に保つ。
    from trading.storage.postgres import PostgresMarketTickRepository, connect

    importer = DukascopyTickImporter(PostgresMarketTickRepository(connect(dsn)))
    stored, failed = importer.import_range(symbol, args.since, args.until)
    if failed:
        print(f"imported {stored} ticks; {failed} hourly downloads failed")
        raise SystemExit(1)
    print(f"imported {stored} ticks")


if __name__ == "__main__":
    main()
