# テストルール（プロジェクト固有）

テスト方針の正本は `AGENTS.md` の「テストルール」節。重複管理を避けるため、ここでは要点へのポインタのみ示す。

- フレームワーク（pytest + `tests/support.py` ファクトリ）・実在人物名の禁止 → `AGENTS.md`「テストルール」
- `tests/unit` / `tests/replay` / `tests/failure` はローカルで常時実行する
- `tests/broker` は Windows + MT5 環境のみ（他環境では自動skip、skip されることは正常）
- `tests/integration` は PostgreSQL が必要（`tests/integration/README.md` 参照）。DBのないローカルでは実行対象外
- **不変条件テスト（`test_invariants.py` 等）を通すためにテスト側を緩めない** → 先に ADR
