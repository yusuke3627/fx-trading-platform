"""MOF 国債金利情報 CSV の読み取りと公表 bound の known_at。

配布ファイルを模した架空データ（Shift_JIS・和暦短縮形）を読ませる。
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.support import FakeTransport, FixedClock
from trading.data.macro.jgb import (
    CURRENT_URL,
    HISTORY_URL,
    JGBYieldCollector,
    parse_wareki_short,
)
from trading.data.macro.registry import INDICATORS, JP_JGB_2Y_YIELD, PIT_UNVERIFIED

RETRIEVED = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)

HEADER = "基準日,1年,2年,3年"


def jgb_csv(rows: list[str], header: str = HEADER) -> bytes:
    lines = [
        "国債金利情報,,,(単位 : %)",
        header,
        *rows,
        ",,,",
        "※最新のcsvデータがダウンロードできない場合の注記,,,",
    ]
    return "\r\n".join(lines).encode("shift_jis")


def collect(history: bytes, current: bytes):
    transport = FakeTransport([history, current])
    batch = JGBYieldCollector(transport, clock=FixedClock(RETRIEVED)).collect()
    return batch, transport


def jst_1500_as_utc(day: date) -> datetime:
    # 15:00 JST = 06:00 UTC（日本に夏時間はない）。
    return datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)


def test_reads_the_two_year_column_wherever_the_header_puts_it() -> None:
    history = jgb_csv(
        ["R8.8.3,9.9,9.9,9.9", "R8.8.4,9.9,9.9,9.9"]
    )
    current = jgb_csv(
        ["R8.8.28,1.1,1.743,1.9", "R8.8.31,1.1,1.750,1.9"],
        header="基準日,1年,1.5年,2年",
    )

    batch, transport = collect(history, current)

    by_period = {o.observation_period: o.value for o in batch.observations}
    assert by_period["2026-08-28"] == Decimal("1.9")
    assert transport.byte_calls == [HISTORY_URL, CURRENT_URL]


def test_wareki_short_form_covers_every_era_the_file_contains() -> None:
    assert parse_wareki_short("S49.9.24") == date(1974, 9, 24)
    assert parse_wareki_short("H1.1.9") == date(1989, 1, 9)
    assert parse_wareki_short("R1.5.7") == date(2019, 5, 7)
    assert parse_wareki_short("R8.8.31") == date(2026, 8, 31)


def test_an_unknown_era_letter_fails_instead_of_guessing() -> None:
    history = jgb_csv(["X1.1.6,1.0,1.0,1.0", "X1.1.7,1.0,1.0,1.0"])
    with pytest.raises(ValueError, match="unknown era"):
        collect(history, jgb_csv(["R8.8.31,1.1,1.7,1.9"]))


def test_known_at_is_the_next_trading_day_at_1500_jst() -> None:
    # 金曜（R8.8.28）の値は週末を跨いだ月曜（R8.8.31）の 15:00 JST に known。
    history = jgb_csv(["R8.8.27,1.1,1.72,1.9"])
    current = jgb_csv(["R8.8.28,1.1,1.743,1.9", "R8.8.31,1.1,1.750,1.9"])

    batch, _ = collect(history, current)

    known = {o.observation_period: o.known_at for o in batch.observations}
    assert known["2026-08-27"] == jst_1500_as_utc(date(2026, 8, 28))
    assert known["2026-08-28"] == jst_1500_as_utc(date(2026, 8, 31))


def test_the_newest_row_waits_for_its_successor() -> None:
    # R8.8.31 の公表日はまだ CSV に現れていないので、この行は emit されない。
    history = jgb_csv(["R8.8.27,1.1,1.72,1.9"])
    current = jgb_csv(["R8.8.28,1.1,1.743,1.9", "R8.8.31,1.1,1.750,1.9"])

    batch, _ = collect(history, current)

    assert [o.observation_period for o in batch.observations] == [
        "2026-08-27",
        "2026-08-28",
    ]


def test_a_missing_value_is_skipped_but_its_date_still_bounds_the_predecessor() -> None:
    history = jgb_csv(
        ["S49.9.24,10.3,9.362,8.8", "S49.9.25,10.3,-,8.8", "S49.9.26,10.3,9.366,8.8"]
    )
    current = jgb_csv(["R8.8.31,1.1,1.750,1.9"])

    batch, _ = collect(history, current)

    emitted = {o.observation_period: o for o in batch.observations}
    # 9/25 は値なしなので emit されないが、9/24 の公表 bound にはなる。
    assert "1974-09-25" not in emitted
    assert emitted["1974-09-24"].known_at == jst_1500_as_utc(date(1974, 9, 25))
    assert emitted["1974-09-26"].known_at == jst_1500_as_utc(date(2026, 8, 31))


def test_the_current_month_file_wins_where_the_two_overlap() -> None:
    history = jgb_csv(["R8.8.27,1.1,9.999,1.9", "R8.8.28,1.1,9.999,1.9"])
    current = jgb_csv(["R8.8.27,1.1,1.720,1.9", "R8.8.28,1.1,1.743,1.9"])

    batch, _ = collect(history, current)

    overlap = next(o for o in batch.observations if o.observation_period == "2026-08-27")
    assert overlap.value == Decimal("1.720")
    assert overlap.source_uri == CURRENT_URL


def test_the_same_days_produce_the_same_vintages_whichever_file_carries_them() -> None:
    # 当月分にしか無い状態と、翌月に全期間ファイルへ同値が現れた状態とで、
    # (period, value, known_at) が一致する — 再収集が新 vintage を作らない前提。
    rows = ["R8.8.27,1.1,1.720,1.9", "R8.8.28,1.1,1.743,1.9", "R8.8.31,1.1,1.750,1.9"]
    early, _ = collect(jgb_csv(["R8.7.31,1.2,1.507,1.6"]), jgb_csv(rows))
    late, _ = collect(jgb_csv(["R8.7.31,1.2,1.507,1.6", *rows]), jgb_csv(["R9.9.1,1.0,1.0,1.0"]))

    def keyed(batch):
        return {
            o.observation_period: (o.value, o.known_at)
            for o in batch.observations
            if o.observation_period.startswith("2026-08-2")
        }

    assert keyed(early) == keyed(late)


def test_a_file_without_the_header_or_without_data_fails_loud() -> None:
    with pytest.raises(ValueError, match="header"):
        collect(b"broken", jgb_csv(["R8.8.31,1.1,1.7,1.9"]))
    with pytest.raises(ValueError, match="no data rows"):
        collect(jgb_csv([]), jgb_csv(["R8.8.31,1.1,1.7,1.9"]))


def test_an_unparseable_value_fails_loud() -> None:
    history = jgb_csv(["R8.8.27,1.1,abc,1.9", "R8.8.28,1.1,1.7,1.9"])
    with pytest.raises(ValueError, match="unparseable"):
        collect(history, jgb_csv(["R8.8.31,1.1,1.7,1.9"]))


def test_every_response_is_archived_and_the_spec_matches_the_source() -> None:
    history = jgb_csv(["R8.8.27,1.1,1.72,1.9", "R8.8.28,1.1,1.743,1.9"])
    batch, _ = collect(history, jgb_csv(["R8.8.31,1.1,1.750,1.9"]))

    assert [event.source_uri for event in batch.raw_events] == [HISTORY_URL, CURRENT_URL]

    spec = INDICATORS[JP_JGB_2Y_YIELD]
    assert spec.frequency == "daily"
    assert spec.unit == "percent"
    assert spec.pit_classification == PIT_UNVERIFIED
    observation = batch.observations[0]
    assert observation.series == JP_JGB_2Y_YIELD
    assert observation.unit == "percent"
    assert observation.source == "MOF"
