"""架空の意見だけで確認画面・保存・評価との接続を検証する。"""
import copy
import http.client
import socket
import threading
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlencode

import pytest

from trading.data.policy import opinions_comparison as comparison
from trading.data.policy import opinions_review as review


@pytest.fixture
def corpus(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    opinions = [{"id": f"fictional-o{i}", "text": f"架空の意見 {i}。政策金利を引き上げる。"}
                for i in range(3)]
    for row in opinions:
        row["text_sha256"] = comparison.digest(row["text"].encode())
    cases = [{"id": "fictional", "source_sha256": "source-hash", "opinions": opinions}]
    (run / "source-manifest.json").write_bytes(b"{}")
    manifest = {"version": comparison.VERSION, "models": comparison.MODELS, "cases": cases,
                "source_manifest_sha256": comparison.digest(b"{}"),
                "corpus_sha256": comparison.digest(comparison.encoded(cases)),
                "requests": [], "split": "confirmation", "repeats": 1}
    comparison.write_json(run / "manifest.json", manifest)
    rows = [{"opinion_id": row["id"], "text": row["text"], "text_sha256": row["text_sha256"],
             "source_sha256": "source-hash", "corpus_sha256": manifest["corpus_sha256"],
             "status": "draft", "stance": None, "reviewer": None, "reviewed_at": None}
            for row in opinions]
    draft = run / "labels.draft.jsonl"
    comparison.write_jsonl(draft, rows)
    return run, draft, tmp_path / "reviewed.jsonl"


def test_save_resume_reset_preserves_source_and_evaluation_gate(corpus, tmp_path):
    run, draft, output = corpus
    original = draft.read_bytes()
    session = review.ReviewSession(*corpus)
    untouched = copy.deepcopy(session.rows)
    assert session.next_pending(-1) == 0
    assert "checked" not in session.render(0).decode().split("<body>")[1]
    assert comparison.human_labels(session.manifest, output)[0] == {}
    session.save(0, "HIKE", " 架空確認者 ", "review", session.revision)
    saved = session.rows[0]
    assert saved["reviewer"] == "架空確認者"
    assert datetime.fromisoformat(saved["reviewed_at"]).tzinfo is not None
    assert session.rows[1:] == untouched[1:]
    assert comparison.human_labels(session.manifest, output)[1]["reviewed"] == 1
    for key in ("opinion_id", "text", "text_sha256", "source_sha256", "corpus_sha256"):
        assert saved[key] == untouched[0][key]
    resumed = review.ReviewSession(*corpus, resume=True)
    assert resumed.rows == session.rows
    assert resumed.next_pending(-1) == 1
    for i, stance in enumerate(("HOLD", "CUT", "UNSPECIFIED")):
        resumed.save(i, stance, "架空確認者", "review", resumed.revision)
    report = comparison.evaluate(run, output, tmp_path / "evaluation")
    assert report["labels"]["status"] == "ready"
    assert resumed.next_pending(-1) == 0
    resumed.save(0, "", "", "reset", resumed.revision)
    assert resumed.rows[0]["status"] == "draft"
    assert all(resumed.rows[0][key] is None for key in ("stance", "reviewer", "reviewed_at"))
    report = comparison.evaluate(run, output, tmp_path / "pending-evaluation")
    assert report["labels"]["status"] == "pending_human_review"
    assert all(row["semantics"] is None for arm in report["arms"].values()
               for row in arm["repeats"])
    assert draft.read_bytes() == original


@pytest.mark.parametrize("stance,reviewer", [("", "架空確認者"), ("HIKE", " "), ("OTHER", "a")])
def test_confirmation_requires_explicit_choice_and_name(corpus, stance, reviewer):
    session = review.ReviewSession(*corpus)
    original = corpus[2].read_bytes()
    with pytest.raises(ValueError, match="分類を選び"):
        session.save(0, stance, reviewer, "review", session.revision)
    assert corpus[2].read_bytes() == original


def test_stale_tab_external_change_and_failed_write_do_not_overwrite(corpus, monkeypatch):
    session = review.ReviewSession(*corpus)
    initial = session.revision
    session.save(0, "HIKE", "架空確認者", "review", initial)
    with pytest.raises(ValueError, match="別の画面"):
        session.save(1, "HOLD", "架空確認者", "review", initial)
    original = corpus[2].read_bytes()
    def fail(*args):
        raise OSError("架空の書込失敗")
    monkeypatch.setattr(review.os, "replace", fail)
    with pytest.raises(OSError):
        session.save(1, "HOLD", "架空確認者", "review", session.revision)
    assert corpus[2].read_bytes() == original
    assert session.rows[1]["status"] == "draft"
    corpus[2].write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="別の操作"):
        session.save(1, "HOLD", "架空確認者", "review", session.revision)
    assert corpus[2].read_bytes() == original + b"\n"


def test_refuses_original_and_existing_output(corpus, tmp_path):
    run, draft, _output = corpus
    for target in (draft, tmp_path / "alias.jsonl"):
        if target != draft:
            target.hardlink_to(draft)
        with pytest.raises(ValueError, match="別の保存先"):
            review.ReviewSession(run, draft, target, resume=True)
    review.ReviewSession(*corpus)
    with pytest.raises(ValueError, match="--resume"):
        review.ReviewSession(*corpus)


@pytest.mark.parametrize("fault", ["text", "id", "missing", "duplicate", "hash"])
def test_rejects_invalid_source(corpus, fault):
    _run, draft, output = corpus
    rows = [comparison.decode(line) for line in draft.read_bytes().splitlines()]
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[1] = rows[0]
    else:
        key = {"text": "text", "id": "opinion_id", "hash": "source_sha256"}[fault]
        rows[0][key] = "不正な値"
    comparison.write_jsonl(draft, rows)
    with pytest.raises(ValueError):
        review.ReviewSession(*corpus)
    assert not output.exists()


@contextmanager
def serving(corpus):
    session = review.ReviewSession(*corpus)
    server = review.ReviewServer(0, session)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield server, session
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def test_browser_protocol_download_resume_and_origin_guards(corpus, tmp_path):
    with serving(corpus) as (server, session):
        status, page, headers = request(server, "GET", "/")
        assert status == 200 and "未確認 3 件" in page.decode()
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "same-origin"
        body = urlencode({"index": 0, "stance": "CUT", "reviewer": "架空確認者",
                          "action": "review", "revision": session.revision, "token": session.token})
        assert request(server, "POST", "/save", body)[0] == 403
        assert request(server, "GET", "/", headers={"Host": "attacker.invalid"})[0] == 403
        assert request(server, "GET", "/manifest.json")[0] == 404
        headers = {"Origin": server.origin, "Content-Type": "application/x-www-form-urlencoded"}
        assert request(server, "POST", "/save", body.replace(session.token, "invalid"), headers)[0] == 403
        status, _, result = request(server, "POST", "/save", body, headers)
        assert status == 303 and "index=1" in result["Location"]
        status, download, result = request(server, "GET", "/labels.jsonl")
        assert status == 200 and "attachment" in result["Content-Disposition"]
        downloaded = tmp_path / "downloaded.jsonl"
        downloaded.write_bytes(download)
        resumed = review.ReviewSession(corpus[0], corpus[1], downloaded, resume=True)
        assert resumed.rows == session.rows
        assert resumed.rows[0]["stance"] == "CUT"
        assert comparison.human_labels(session.manifest, downloaded)[1]["reviewed"] == 1


def test_page_escapes_untrusted_fields_and_hides_draft_candidates(corpus):
    rows = [comparison.decode(line) for line in corpus[1].read_bytes().splitlines()]
    rows[0]["stance"] = "HIKE"
    comparison.write_jsonl(corpus[1], rows)
    session = review.ReviewSession(*corpus)
    session.rows[0]["text"] = '<script>alert("x")</script>'
    page = session.render(0, '<img src=x onerror="bad">').decode()
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page
    assert "checked" not in page.split("<body>")[1]
    assert session.rows[0]["stance"] is None


def test_idle_browser_preconnection_does_not_block_page(corpus):
    with (serving(corpus) as (server, _session),
          socket.create_connection(("127.0.0.1", server.server_port))):
        assert request(server, "GET", "/")[0] == 200
