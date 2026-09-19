"""BOJ「主な意見」の公表日時と raw archive。

段階1の声明抽出の精度測定を受け、段階2では素材だけを PIT で保存する。
PDF の Last-Modified は告知された公表より14〜64時間早かったため使わない。
初回の known_at は声明に記載された公表日時、訂正版は取得完了時刻とする。
訂正の公表日時は不明なので published_at は None とし、当初の告知日時は
payload の announced_published_at に残す。

空の DB への backfill では、既に静かに差し替えられていた内容も初回として
告知日時が付く。過去分について初版と差し替え後の内容を区別する方法はない。
"""
from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

from trading.data.policy.meetings import PolicyMeeting
from trading.domain.event import EventEnvelope

BOJ_SUMMARY_OF_OPINIONS_RAW = "BOJ_SUMMARY_OF_OPINIONS_RAW"
JST = timezone(timedelta(hours=9))
_PUBLICATION = re.compile(
    r"主な意見[―—]+(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"\([月火水木金土日]\)(?P<hour>\d{1,2}):(?P<minute>\d{2})予定"
)


def opinions_url(decision_date: date) -> str:
    return (
        f"https://www.boj.or.jp/mopo/mpmsche_minu/opinion_{decision_date:%Y}/"
        f"opi{decision_date:%y%m%d}.pdf"
    )


def japanese_statement_pdf_url(decision_date: date) -> str:
    """公表予定欄を読むための日本語声明 PDF の URL。

    YAML の source_uri は会合によって英語版声明を指しており、英語版には
    公表予定欄が無い。そのため採点事実の出典とは別に、会合日から導出する。
    """
    return (
        f"https://www.boj.or.jp/mopo/mpmdeci/mpr_{decision_date:%Y}/"
        f"k{decision_date:%y%m%d}a.pdf"
    )


def publication_from_statement(text: str, decision_date: date) -> datetime:
    """声明の告知を読む。欠落・曖昧な記載は時刻を推測せず失敗させる。"""
    compact = "".join(unicodedata.normalize("NFKC", text).split())
    matches = list(_PUBLICATION.finditer(compact))
    if len(matches) != 1:
        raise ValueError("主な意見の公表日時を一意に取得できません")
    parts = {key: int(value) for key, value in matches[0].groupdict().items()}
    year = decision_date.year + (parts["month"] < decision_date.month)
    published_at = datetime(year=year, tzinfo=JST, **parts)
    if published_at.date() < decision_date:
        raise ValueError("主な意見の公表日が会合日より前です")
    return published_at


def extract_pdf_text(raw: bytes) -> str:
    """PDF extra は実際の抽出時だけ読み込む。空・壊れた文書は保存しない。"""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:
        raise RuntimeError("PDF 抽出には pdf extra が必要です: pip install '.[pdf]'") from exc

    try:
        reader = PdfReader(BytesIO(raw))
        text = "\n".join(page.extract_text() for page in reader.pages)
    except PyPdfError as exc:
        raise ValueError(f"PDF のテキスト抽出に失敗しました: {exc}") from exc
    if not text.strip():
        raise ValueError("PDF に本文テキストがありません")
    return text


def event_from_opinions(
    meeting: PolicyMeeting,
    *,
    raw: bytes,
    text: str,
    published_at: datetime,
    retrieved_at: datetime,
    is_revision: bool = False,
) -> EventEnvelope:
    """本文を inline 保存し、後段の処理に再取得や PDF extra を要求しない。

    macro の raw archive と同様にイベント自体を保存先とするので raw_uri は None。
    本文に加えて原文 PDF も Base64 で保持し、パーサ修正時に同じ原文から再解析
    できるようにする。payload_hash は JSON ではなく原文バイト列の SHA-256。
    """
    return EventEnvelope(
        event_id=uuid4(),
        event_type=BOJ_SUMMARY_OF_OPINIONS_RAW,
        source="BOJ_OFFICIAL",
        source_uri=opinions_url(meeting.decision_date),
        raw_uri=None,
        payload={
            "bank": meeting.bank,
            "decision_date": meeting.decision_date.isoformat(),
            "statement_source_uri": japanese_statement_pdf_url(meeting.decision_date),
            "announced_published_at": published_at.isoformat(),
            "text": text,
            "pdf_base64": base64.b64encode(raw).decode("ascii"),
        },
        payload_hash=hashlib.sha256(raw).hexdigest(),
        published_at=None if is_revision else published_at,
        known_at=retrieved_at if is_revision else published_at,
        retrieved_at=retrieved_at,
    )
