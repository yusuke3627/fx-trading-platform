"""人手ラベルをローカルで確認する画面。外部API・DB・取引には接続しない。"""
from __future__ import annotations

import argparse
import html
import os
import secrets
import tempfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from threading import Lock
from urllib.parse import parse_qs, urlencode, urlsplit

from trading.data.policy import opinions_comparison as comparison


class ReviewSession:
    def __init__(self, run: Path, labels: Path, output: Path, resume: bool = False):
        self.manifest = comparison.load_manifest(run)
        self.output = output.resolve()
        for source in (labels, run / "labels.draft.jsonl"):
            if (self.output == source.resolve()
                    or (output.exists() and source.exists() and output.samefile(source))):
                raise ValueError("元の下書きとは別の保存先を指定してください")
        if output.is_symlink():
            raise ValueError("保存先にシンボリックリンクは使用できません")
        if output.exists() and not resume:
            raise ValueError("保存先が存在します。再開する場合は --resume を指定してください")
        if resume and not output.exists():
            raise ValueError("再開する保存先が見つかりません")
        path = output if resume else labels
        raw = path.read_bytes()
        comparison.human_labels(self.manifest, path)
        if raw != path.read_bytes():
            raise ValueError("入力ファイルが更新されています。もう一度起動してください")
        rows = [comparison.decode(line) for line in raw.decode("utf-8").splitlines()
                if line.strip()]
        expected = [opinion for case in self.manifest["cases"] for opinion in case["opinions"]]
        if not expected or len(rows) != len(expected) or len({r["id"] for r in expected}) != len(rows):
            raise ValueError("全意見のラベルが重複なく必要です")
        by_id = {row["opinion_id"]: row for row in rows}
        self.rows = [by_id[opinion["id"]] for opinion in expected]
        for row in self.rows:
            if comparison.digest(row["text"].encode()) != row["text_sha256"]:
                raise ValueError("意見本文のhashが一致しません")
            if row["status"] == "draft":
                row.update(stance=None, reviewer=None, reviewed_at=None)
            elif not isinstance(row["reviewer"], str):
                raise ValueError("確認者は文字列で指定してください")
        self.token = secrets.token_urlsafe(32)
        if not resume:
            with output.open("xb") as stream:
                stream.write(self.serialize(self.rows))
        self.revision = comparison.digest(output.read_bytes())

    @staticmethod
    def serialize(rows: list[dict]) -> bytes:
        return b"".join(comparison.encoded(row) + b"\n" for row in rows)

    def check_disk(self) -> bytes:
        data = self.output.read_bytes()
        if comparison.digest(data) != self.revision:
            raise ValueError("保存先が別の操作で更新されています。画面を終了し、--resume で再開してください")
        return data

    def save(self, index: int, stance: str, reviewer: str, action: str, revision: str) -> None:
        if revision != self.revision:
            raise ValueError("別の画面で保存されました。再読み込みして内容を確認してください")
        if not 0 <= index < len(self.rows) or action not in ("review", "reset"):
            raise ValueError("保存する意見または操作が不正です")
        reviewer = reviewer.strip()
        if action == "review" and (stance not in comparison.CRITERIA or not reviewer):
            raise ValueError("分類を選び、確認者名を入力してください")
        if len(reviewer) > 100:
            raise ValueError("確認者名は100文字以内で入力してください")
        self.check_disk()
        rows = [dict(row) for row in self.rows]
        rows[index].update(
            stance=stance if action == "review" else None,
            status="reviewed" if action == "review" else "draft",
            reviewer=reviewer if action == "review" else None,
            reviewed_at=datetime.now(UTC).isoformat() if action == "review" else None,
        )
        data = self.serialize(rows)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.output.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.check_disk()
            os.replace(temporary, self.output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.rows = rows
        self.revision = comparison.digest(data)

    def next_pending(self, index: int) -> int:
        order = list(range(index + 1, len(self.rows))) + list(range(index + 1))
        return next((i for i in order if self.rows[i]["status"] == "draft"), max(0, index))

    def render(self, index: int, reviewer: str = "", notice: str = "") -> bytes:
        row = self.rows[index]
        reviewed = sum(row["status"] == "reviewed" for row in self.rows)
        escape = html.escape
        choices = "".join(
            f'<label class="choice"><input type="radio" required name="stance" value="{key}" '
            f'{"checked" if row["status"] == "reviewed" and row["stance"] == key else ""}>'
            f'<span><strong>{key}</strong><span>{escape(description)}</span></span></label>'
            for key, description in comparison.CRITERIA.items())
        def link(i: int) -> str:
            return "/?" + escape(urlencode({"index": i, "reviewer": reviewer}))
        values = {
            "index": index, "number": index + 1, "total": len(self.rows), "reviewed": reviewed,
            "remaining": len(self.rows) - reviewed, "choices": choices,
            "opinion_id": escape(row["opinion_id"]), "text": escape(row["text"]),
            "status": "確認済み" if row["status"] == "reviewed" else "未確認",
            "saved_detail": (escape(f'{row["reviewer"]} · {row["reviewed_at"]}')
                          if row["status"] == "reviewed" else "分類はまだ保存されていません"),
            "reviewer": escape(reviewer or row["reviewer"] or "", quote=True),
            "token": self.token, "revision": self.revision, "notice": escape(notice),
            "previous": link(max(0, index - 1)), "following": link(min(len(self.rows) - 1, index + 1)),
            "pending": link(self.next_pending(index)), "rules": escape(comparison.RULES),
            "output": escape(str(self.output)),
            "completion": ("全件の確認が完了しました。保存したJSONLを比較ツールのevaluateへ渡せます。"
                        if reviewed == len(self.rows) else
                        "全件の確認が終わるまで、比較ツールは意味精度を判定しません。"),
        }
        template = Template(Path(__file__).with_suffix(".html").read_text(encoding="utf-8"))
        return template.substitute(values).encode()


class ReviewServer(ThreadingHTTPServer):
    def __init__(self, port: int, session: ReviewSession):
        self.session = session
        self.lock = Lock()
        super().__init__(("127.0.0.1", port), ReviewHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def respond(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
                location: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if location:
            self.send_header("Location", location)
        if content_type == "application/x-ndjson":
            self.send_header("Content-Disposition", 'attachment; filename="labels.reviewed.jsonl"')
        self.end_headers()
        self.wfile.write(body)

    def error_page(self, error: Exception) -> None:
        message = html.escape(str(error))
        body = ('<!doctype html><html lang="ja"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>確認結果を保存できませんでした</title><main>'
                f'<h1>操作を完了できませんでした</h1><p role="alert">{message}</p>'
                '<p><a href="/">確認画面へ戻る</a></p></main></html>')
        self.respond(409, body.encode())

    def allowed_host(self) -> bool:
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self.respond(403, "起動時に表示されたローカルURLを開いてください".encode(),
                         "text/plain; charset=utf-8")
            return False
        return True

    def do_GET(self) -> None:
        with self.server.lock:
            self.get_response()

    def get_response(self) -> None:
        if not self.allowed_host():
            return
        path = urlsplit(self.path)
        session = self.server.session
        try:
            if path.path == "/labels.jsonl":
                self.respond(200, session.check_disk(), "application/x-ndjson")
                return
            if path.path != "/":
                self.respond(404, b"Not found", "text/plain")
                return
            query = parse_qs(path.query, max_num_fields=5)
            index = int(query.get("index", [str(session.next_pending(-1))])[0])
            if not 0 <= index < len(session.rows):
                raise ValueError("意見番号が範囲外です")
            session.check_disk()
            notice = "保存しました" if query.get("saved") == ["1"] else ""
            self.respond(200, session.render(index, query.get("reviewer", [""])[0], notice))
        except (ValueError, OSError) as exc:
            self.error_page(exc)

    def do_POST(self) -> None:
        self.connection.settimeout(5)
        with self.server.lock:
            self.post_response()

    def post_response(self) -> None:
        if not self.allowed_host():
            return
        session = self.server.session
        if self.path != "/save" or self.headers.get("Origin") != self.server.origin:
            self.respond(403, b"Forbidden", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("入力サイズが不正です")
            fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=6)
            if any(len(value) != 1 for value in fields.values()):
                raise ValueError("入力が重複しています")
            data = {key: value[0] for key, value in fields.items()}
            if not secrets.compare_digest(data.get("token", "").encode(), session.token.encode()):
                self.respond(403, b"Forbidden", "text/plain")
                return
            index = int(data["index"])
            session.save(index, data.get("stance", ""), data.get("reviewer", ""),
                         data["action"], data["revision"])
            next_index = session.next_pending(index) if data["action"] == "review" else index
            self.respond(303, b"", location="/?" + urlencode({
                "index": next_index, "reviewer": data.get("reviewer", ""), "saved": "1"}))
        except (ValueError, KeyError, OSError) as exc:
            self.error_page(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="政策意見の人手ラベル確認画面（ローカル専用）")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--labels", type=Path, help="開始する下書き。省略時はrun内のlabels.draft.jsonl")
    parser.add_argument("--output", required=True, type=Path, help="元の下書きと別のJSONL保存先")
    parser.add_argument("--resume", action="store_true", help="既存の保存先から再開する")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        session = ReviewSession(args.run_dir, args.labels or args.run_dir / "labels.draft.jsonl",
                                args.output, args.resume)
        with ReviewServer(args.port, session) as server:
            print(f"確認画面: {server.origin}\n保存先: {session.output}\n終了: Ctrl+C", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"起動できません: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
