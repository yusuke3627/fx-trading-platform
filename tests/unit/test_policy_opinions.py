"""架空の声明・主な意見で、取得・保存境界と公表日時をオフライン検証する。"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

import pytest

from tests.support import FakeEventRepository, FixedClock
from trading.data.policy import opinions_collector as collector
from trading.data.policy.meetings import PolicyMeeting
from trading.data.policy.opinions import (
    BOJ_SUMMARY_OF_OPINIONS_RAW,
    JST,
    event_from_opinions,
    extract_pdf_text,
    japanese_statement_pdf_url,
    opinions_url,
    publication_from_statement,
)

DECISION = date(2026, 7, 30)
PUBLICATION = datetime(2026, 8, 7, 8, 50, tzinfo=JST)
RETRIEVED = datetime(2026, 9, 19, 4, 0, tzinfo=UTC)
STATEMENT = """・公表日時
当面の金融政策運営について――7 月 30 日（木）12:11
主 な 意 見 ―― 8 月 7 日 （金） 8 : 50 予 定
議事要旨――9月18日（金）8:50予定
"""
RAW_STATEMENT = STATEMENT.encode()
RAW_OPINIONS = "主な意見\n物価見通しには上下両方向の不確実性がある。".encode()


@pytest.fixture
def meeting() -> PolicyMeeting:
    return PolicyMeeting(
        bank="BOJ",
        decision_date=DECISION,
        statement_published_at=datetime(2026, 7, 30, 12, 11, tzinfo=JST),
        verified=True,
        source_uri="https://example.invalid/statement.pdf",
    )


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("unit テストからネットワークへ接続しました")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.create_connection", fail)


@pytest.mark.parametrize("day, expected", [
    (date(2026, 7, 30), "opinion_2026/opi260730.pdf"),
    (date(2025, 1, 23), "opinion_2025/opi250123.pdf"),
])
def test_url_uses_the_meeting_date(day, expected):
    assert opinions_url(day) == f"https://www.boj.or.jp/mopo/mpmsche_minu/{expected}"


@pytest.mark.parametrize("day, expected", [
    (date(2024, 3, 19), "mpr_2024/k240319a.pdf"),
    (date(2026, 7, 30), "mpr_2026/k260730a.pdf"),
])
def test_japanese_statement_pdf_url_uses_the_meeting_date(day, expected):
    assert japanese_statement_pdf_url(day) == f"https://www.boj.or.jp/mopo/mpmdeci/{expected}"


@pytest.mark.parametrize("day, announced, publication", [
    (date(2024, 3, 19), "3月28日（木）8:50予定", datetime(2024, 3, 28, 8, 50, tzinfo=JST)),
    (date(2024, 4, 26), "5月8日（水）8:50予定", datetime(2024, 5, 8, 8, 50, tzinfo=JST)),
    (date(2024, 6, 14), "6月24日（月）8:50予定", datetime(2024, 6, 24, 8, 50, tzinfo=JST)),
])
def test_english_html_source_uses_japanese_pdf_for_publication(meeting, day, announced, publication):
    english_uri = f"https://example.invalid/en/mopo/mpmdeci/state_{day:%Y}/k{day:%y%m%d}a.htm"
    meeting = meeting.model_copy(update={
        "decision_date": day,
        "statement_published_at": datetime(day.year, day.month, day.day, 12, tzinfo=JST),
        "source_uri": english_uri,
    })
    statement_uri = f"https://www.boj.or.jp/mopo/mpmdeci/mpr_{day:%Y}/k{day:%y%m%d}a.pdf"
    responses = {
        statement_uri: f"・公表日時\n主な意見――{announced}".encode(),
        opinions_url(day): RAW_OPINIONS,
    }
    fetch = Mock(side_effect=responses.__getitem__)
    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=fetch,
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(1, 1, 0, 0, ())
    assert [call.args[0] for call in fetch.call_args_list] == [statement_uri, opinions_url(day)]
    assert meeting.source_uri == english_uri
    event = repository.events[0]
    assert event.known_at == event.published_at == publication
    assert event.payload["announced_published_at"] == publication.isoformat()
    assert event.payload["statement_source_uri"] == statement_uri


@pytest.mark.parametrize("text, expected", [
    (STATEMENT, PUBLICATION),
    ("主な意見――８月７日（金）９：１５予定", PUBLICATION.replace(hour=9, minute=15)),
    ("主な意見—8月7日(金)\n8:50予定", PUBLICATION),
])
def test_publication_comes_from_statement_not_a_fixed_time(text, expected):
    assert publication_from_statement(text, DECISION) == expected


def test_publication_rolls_over_the_year():
    assert publication_from_statement(
        "主な意見――1月6日（水）9:05予定", date(2026, 12, 24)
    ) == datetime(2027, 1, 6, 9, 5, tzinfo=JST)


@pytest.mark.parametrize("text", [
    "公表予定の記載がありません",
    "議事要旨――8月7日（金）8:50予定",
    "主な意見――8月7日（金）予定",
    "主な意見――8月7日（金）25:00予定",
    "主な意見――8月32日（金）8:50予定",
    "主な意見――7月29日（水）8:50予定",
    STATEMENT + "主な意見――8月10日（月）8:50予定",
])
def test_missing_ambiguous_or_invalid_publication_fails(text):
    with pytest.raises(ValueError):
        publication_from_statement(text, DECISION)


def test_event_archives_original_bytes_and_text_with_publication_known_at(meeting):
    event = event_from_opinions(
        meeting, raw=RAW_OPINIONS, text=RAW_OPINIONS.decode(),
        published_at=PUBLICATION, retrieved_at=RETRIEVED,
    )
    assert event.event_type == BOJ_SUMMARY_OF_OPINIONS_RAW
    assert event.source == "BOJ_OFFICIAL"
    assert event.source_uri == opinions_url(DECISION)
    assert event.published_at == event.known_at == PUBLICATION
    assert event.retrieved_at == RETRIEVED
    assert event.known_at != RETRIEVED
    assert event.payload_hash == hashlib.sha256(RAW_OPINIONS).hexdigest()
    assert event.payload["decision_date"] == DECISION.isoformat()
    assert event.payload["statement_source_uri"] == japanese_statement_pdf_url(DECISION)
    assert event.payload["announced_published_at"] == PUBLICATION.isoformat()
    assert event.payload["text"] == RAW_OPINIONS.decode()
    assert base64.b64decode(event.payload["pdf_base64"]) == RAW_OPINIONS
    assert json.loads(json.dumps(event.payload)) == event.payload
    assert event.raw_uri is None
    repository = FakeEventRepository([event])
    assert repository.known_before(PUBLICATION - timedelta(microseconds=1)) == []
    assert repository.known_before(PUBLICATION) == [event]


def test_future_publication_skips_even_when_pdf_was_uploaded_early(meeting):
    fetch = Mock(return_value=RAW_STATEMENT)
    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=fetch,
        clock=FixedClock(PUBLICATION - timedelta(microseconds=1)), extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(0, 0, 0, 1, ())
    assert repository.events == []
    fetch.assert_called_once_with(japanese_statement_pdf_url(DECISION))


def test_publication_boundary_and_retrieval_timestamp_are_separate(meeting):
    clock = FixedClock(PUBLICATION)

    def fetch(url):
        if url == japanese_statement_pdf_url(DECISION):
            return RAW_STATEMENT
        clock.advance(seconds=3)
        return RAW_OPINIONS

    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=fetch, clock=clock, extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(1, 1, 0, 0, ())
    assert repository.events[0].known_at == PUBLICATION
    assert repository.events[0].retrieved_at == PUBLICATION + timedelta(seconds=3)


def test_http_last_modified_is_not_used(meeting, monkeypatch):
    # 本番の transport を通すが、HTTP 応答はその場で差し替える。
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = [RAW_STATEMENT, RAW_OPINIONS]
    response.headers = {"Last-Modified": "Thu, 06 Aug 2026 08:00:00 GMT"}
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=collector.HttpTransport().get_bytes,
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(1, 1, 0, 0, ())
    assert repository.events[0].known_at == PUBLICATION
    assert repository.events[0].retrieved_at == RETRIEVED
    assert [call.args[0].full_url for call in urlopen.call_args_list] == [
        japanese_statement_pdf_url(DECISION), opinions_url(DECISION),
    ]


@pytest.mark.parametrize("failure", [
    HTTPError("https://example.invalid/opinions.pdf", 404, "Not Found", {}, None),
    HTTPError("https://example.invalid/opinions.pdf", 503, "Unavailable", {}, None),
    URLError("offline"),
    TimeoutError("timed out"),
], ids=["404", "503", "offline", "timeout"])
def test_overdue_fetch_failure_is_reported_and_later_meetings_continue(meeting, failure):
    later = meeting.model_copy(update={"decision_date": date(2026, 9, 17)})
    fetch = Mock(side_effect=[
        RAW_STATEMENT, failure,
        "主な意見――9月18日（金）8:50予定".encode(), RAW_OPINIONS,
    ])
    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting, later], repository, fetch=fetch,
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert (result.fetched, result.stored, result.unpublished) == (1, 1, 0)
    assert len(result.failures) == 1
    assert f"BOJ {DECISION} 主な意見の取得" in result.failures[0]
    assert type(failure).__name__ in result.failures[0]
    assert len(repository.events) == 1


@pytest.mark.parametrize("response", [b"no publication schedule", URLError("offline")])
def test_statement_failure_is_not_counted_as_unpublished(meeting, response):
    fetch = Mock(side_effect=[response])
    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=fetch,
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert (result.fetched, result.stored, result.unpublished) == (0, 0, 0)
    assert len(result.failures) == 1
    assert "声明の取得・公表日時の解析" in result.failures[0]
    assert repository.events == []
    fetch.assert_called_once_with(japanese_statement_pdf_url(DECISION))


def test_rerun_deduplicates_but_changed_and_restored_bytes_are_saved(meeting):
    repository = FakeEventRepository()
    clock = FixedClock(RETRIEVED)
    revised = RAW_OPINIONS + "\n追記".encode()
    for raw, expected_stored, expected_revised in [
        (RAW_OPINIONS, 1, 0), (RAW_OPINIONS, 0, 0), (revised, 1, 1),
        (RAW_OPINIONS, 1, 1), (RAW_OPINIONS, 0, 0),
    ]:
        result = collector.collect_opinions(
            [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, raw]),
            clock=clock, extract_text=bytes.decode,
        )
        assert result == collector.CollectionResult(1, expected_stored, expected_revised, 0, ())
        if expected_stored:
            event = repository.events[-1]
            assert event.known_at == (clock.now() if expected_revised else PUBLICATION)
            assert event.published_at == (None if expected_revised else PUBLICATION)
            assert event.payload["announced_published_at"] == PUBLICATION.isoformat()
        clock.advance(hours=1)
    assert len(repository.events) == 3
    assert len({event.event_id for event in repository.events}) == 3
    assert repository.known_before(RETRIEVED + timedelta(hours=2, microseconds=-1)) == [
        repository.events[0],
    ]
    assert repository.known_before(RETRIEVED + timedelta(hours=2)) == repository.events[:2]


def test_same_hash_skips_pdf_extraction_and_insert(meeting):
    repository = Mock()
    repository.latest_raw_hash.return_value = hashlib.sha256(RAW_OPINIONS).hexdigest()
    extract = Mock(return_value=STATEMENT)
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(RETRIEVED), extract_text=extract,
    )
    assert result == collector.CollectionResult(1, 0, 0, 0, ())
    repository.latest_raw_hash.assert_called_once_with(
        BOJ_SUMMARY_OF_OPINIONS_RAW, opinions_url(DECISION)
    )
    repository.insert_raw_archive.assert_not_called()
    extract.assert_called_once_with(RAW_STATEMENT)


def test_concurrent_duplicate_is_not_counted_as_a_saved_revision(meeting):
    repository = Mock()
    repository.latest_raw_hash.return_value = "previous-hash"
    repository.insert_raw_archive.return_value = False
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(1, 0, 0, 0, ())
    event = repository.insert_raw_archive.call_args.args[0]
    assert event.known_at == RETRIEVED
    assert event.published_at is None


def test_concurrent_first_revision_is_rejected_without_backdating(meeting):
    repository = FakeEventRepository()
    original_insert = repository.insert_raw_archive
    competing = event_from_opinions(
        meeting, raw=b"another version", text="別の本文",
        published_at=PUBLICATION, retrieved_at=RETRIEVED - timedelta(seconds=1),
    )

    def insert_after_competitor(event, *, require_initial=False):
        original_insert(competing)
        return original_insert(event, require_initial=require_initial)

    repository.insert_raw_archive = insert_after_competitor
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert (result.stored, result.revised, len(result.failures)) == (0, 0, 1)
    assert "初回判定後" in result.failures[0]
    assert repository.events == [competing]

    # 再取得した時刻を使い、初回の公表日時へ遡らせず訂正版として保存する。
    repository.insert_raw_archive = original_insert
    next_retrieval = RETRIEVED + timedelta(seconds=30)
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(next_retrieval), extract_text=bytes.decode,
    )
    assert result == collector.CollectionResult(1, 1, 1, 0, ())
    assert repository.events[-1].known_at == next_retrieval
    assert repository.events[-1].published_at is None


def test_other_banks_are_not_fetched(meeting):
    fetch = Mock()
    result = collector.collect_opinions(
        [meeting.model_copy(update={"bank": "FED"})], FakeEventRepository(),
        fetch=fetch, clock=FixedClock(RETRIEVED),
    )
    assert result == collector.CollectionResult(0, 0, 0, 0, ())
    fetch.assert_not_called()


def test_storage_error_is_a_reported_failure(meeting):
    repository = Mock()
    repository.latest_raw_hash.return_value = None
    repository.insert_raw_archive.side_effect = RuntimeError("storage unavailable")
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert (result.fetched, result.stored, result.unpublished) == (1, 0, 0)
    assert len(result.failures) == 1
    assert "raw archive の保存: RuntimeError: storage unavailable" in result.failures[0]


def test_hash_lookup_error_is_reported_without_an_initial_insert(meeting):
    repository = Mock()
    repository.latest_raw_hash.side_effect = RuntimeError("lookup unavailable")
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, RAW_OPINIONS]),
        clock=FixedClock(RETRIEVED), extract_text=bytes.decode,
    )
    assert (result.stored, result.revised, len(result.failures)) == (0, 0, 1)
    assert "最新 raw ハッシュの取得: RuntimeError: lookup unavailable" in result.failures[0]
    repository.insert_raw_archive.assert_not_called()


@pytest.fixture
def pdf_bytes():
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    def build(*pages, password=None):
        writer = pypdf.PdfWriter()
        for text in pages:
            page = writer.add_blank_page(width=600, height=800)
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            })
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = content
        if password:
            writer.encrypt(password)
        stream = BytesIO()
        writer.write(stream)
        return stream.getvalue()

    return build


def test_extract_pdf_text_reads_all_pages(pdf_bytes):
    assert extract_pdf_text(pdf_bytes("First page", "Second page")) == "First page\nSecond page"


@pytest.mark.parametrize("kind", ["broken", "blank", "encrypted"])
def test_invalid_pdf_is_a_failure_and_never_stored(meeting, pdf_bytes, kind):
    raw = {
        "broken": b"<html>error page</html>",
        "blank": pdf_bytes(""),
        "encrypted": pdf_bytes("protected", password="fictional-password"),
    }[kind]

    def extract(raw_bytes):
        return STATEMENT if raw_bytes == RAW_STATEMENT else extract_pdf_text(raw_bytes)

    repository = FakeEventRepository()
    result = collector.collect_opinions(
        [meeting], repository, fetch=Mock(side_effect=[RAW_STATEMENT, raw]),
        clock=FixedClock(RETRIEVED), extract_text=extract,
    )
    assert (result.fetched, result.stored, result.unpublished) == (1, 0, 0)
    assert len(result.failures) == 1
    assert "主な意見の本文抽出: ValueError" in result.failures[0]
    assert repository.events == []


@pytest.mark.parametrize("failed", [False, True])
def test_cli_loads_configured_meetings_reports_counts_and_fails_nonzero(
    tmp_path, monkeypatch, capsys, failed,
):
    import trading.config

    meetings_path = tmp_path / "meetings.yaml"
    meetings_path.write_text("""meetings:
  - bank: BOJ
    decision_date: 2026-07-30
    statement_published_at: '2026-07-30T12:11:00+09:00'
    verified: true
    source_uri: https://example.invalid/statement.pdf
  - bank: FED
    decision_date: 2026-07-29
    statement_published_at: '2026-07-29T14:00:00-04:00'
    verified: true
    source_uri: https://example.invalid/other-statement
""", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "opinions_collector", "--env", "test", "--meetings", str(meetings_path),
    ])
    load_config = Mock(return_value=SimpleNamespace(
        storage=SimpleNamespace(dsn_env="FICTIONAL_DB_DSN")
    ))
    monkeypatch.setattr(trading.config, "load_config", load_config)
    monkeypatch.setenv("FICTIONAL_DB_DSN", "fictional-dsn")
    connect = MagicMock()
    repository = FakeEventRepository()
    repository_factory = Mock(return_value=repository)
    monkeypatch.setitem(sys.modules, "trading.storage.postgres", SimpleNamespace(
        connect=connect, PostgresEventRepository=repository_factory,
    ))
    fetch = Mock(side_effect=[RAW_STATEMENT, URLError("offline") if failed else RAW_OPINIONS])
    monkeypatch.setattr(collector.HttpTransport, "get_bytes", fetch)
    monkeypatch.setattr(collector, "SystemClock", lambda: FixedClock(RETRIEVED))
    # CLI も通常の収集ロジックを通し、PDF 抽出だけを差し替える。
    collect = collector.collect_opinions
    monkeypatch.setattr(collector, "collect_opinions", lambda *args, **kwargs: collect(
        *args, **kwargs, extract_text=bytes.decode,
    ))
    if failed:
        with pytest.raises(SystemExit) as exc:
            collector.main()
        assert exc.value.code == 1
    else:
        collector.main()
    load_config.assert_called_once_with("test")
    connect.assert_called_once_with("fictional-dsn")
    repository_factory.assert_called_once_with(connect.return_value.__enter__.return_value)
    connect.return_value.__exit__.assert_called_once()
    output = capsys.readouterr()
    if failed:
        assert "取得 0 件・保存 0 件（うち訂正版 0 件）・未公表 0 件・失敗 1 件" in output.out
        assert "BOJ 2026-07-30 主な意見の取得" in output.err
    else:
        assert "取得 1 件・保存 1 件（うち訂正版 0 件）・未公表 0 件・失敗 0 件" in output.out
        assert output.err == ""
    assert fetch.call_count == 2
    if not failed:
        fetch.side_effect = [RAW_STATEMENT, RAW_OPINIONS + "訂正".encode()]
        collector.main()
        assert "保存 1 件（うち訂正版 1 件）" in capsys.readouterr().out


def test_cli_requires_configured_dsn_before_io(monkeypatch):
    import trading.config

    monkeypatch.setattr(sys, "argv", ["opinions_collector"])
    monkeypatch.setattr(trading.config, "load_config", lambda env: SimpleNamespace(
        storage=SimpleNamespace(dsn_env="FICTIONAL_DB_DSN")
    ))
    monkeypatch.delenv("FICTIONAL_DB_DSN", raising=False)
    load_meetings = Mock()
    monkeypatch.setattr(collector, "load_meetings", load_meetings)
    with pytest.raises(SystemExit, match="FICTIONAL_DB_DSN is not set"):
        collector.main()
    load_meetings.assert_not_called()


def test_import_and_logic_need_no_optional_dependencies_or_network():
    script = """
import builtins
import socket
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'pypdf', 'psycopg', 'openai'}:
        raise ImportError('optional dependencies disabled')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
def no_network(*args, **kwargs):
    raise AssertionError('network access forbidden')
socket.create_connection = no_network
socket.getaddrinfo = no_network
from trading.data.policy import opinions, opinions_collector
from datetime import date
assert opinions.publication_from_statement(
    '主な意見――8月7日（金）8:50予定', date(2026, 7, 30)
).hour == 8
try:
    opinions.extract_pdf_text(b'pdf')
except RuntimeError as exc:
    assert 'pdf extra' in str(exc)
else:
    raise AssertionError('missing dependency must fail explicitly')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
