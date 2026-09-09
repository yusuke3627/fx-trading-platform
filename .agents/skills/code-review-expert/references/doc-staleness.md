# Documentation Staleness Checklist

## 概要

コードが変更されると、関連するドキュメントが陳腐化することがある。このチェックリストは、差分とあわせて見直すべきドキュメントを特定するためのものである。

## 確認すべき項目

### 1. インラインドキュメント

- **メソッド/関数の docstring**: パラメータ名・型・戻り値・説明が実装と一致しているか？
- **モジュールレベルのコメント**: 「このモジュールは X を行う」というコメントが現状を反映しているか？
- **TODO/FIXME/HACK コメント**: 参照している課題は解決済みか？ 削除するか更新する。
- **コメント内のサンプルコード**: サンプルが正しくコンパイル/実行できるか？

### 2. README とガイド

| Change Type | Documentation to Check |
|-------------|----------------------|
| New endpoint / route | README API section, route documentation |
| New environment variable | README setup section, `.env.example` |
| Changed CLI flags / arguments | README usage section, `--help` output |
| New dependency | README prerequisites, installation steps |
| Changed build steps | README build/deploy section, CI config |
| New feature | Feature documentation, user guides |
| Removed feature | Remove from all documentation |
| Changed config format | Configuration guide, example configs |

### 3. API ドキュメント

- **OpenAPI/Swagger spec**: リクエスト/レスポンスのスキーマがコードと一致しているか？
- **GraphQL スキーマのドキュメント**: フィールドの説明は最新か？
- **Postman/Insomnia コレクション**: サンプルリクエストはまだ有効か？

### 4. アーキテクチャドキュメント

- **Architecture Decision Records (ADR)**: この変更は過去の決定を置き換えるものか？
- **システム図**: データフロー図やコンポーネント図を更新する必要があるか？
- **ARCHITECTURE.md / DESIGN.md**: 記述されている構造が現状と一致しているか？

### 5. 設定とサンプル

- **`.env.example`**: 必要な変数がすべて記載されているか？ 削除された変数は整理されているか？
- **設定ファイルのテンプレート**: デフォルト値やコメントが現在の挙動を反映しているか？
- **Docker/compose ファイル**: サービス定義が現在の依存関係と一致しているか？
- **CI/CD パイプラインの設定**: build/test/deploy のステップが現在のワークフローと一致しているか？

## 検出ヒューリスティック

差分に以下が含まれる場合、ドキュメントの陳腐化を指摘する。

| Signal in Diff | Likely Stale Documentation |
|---------------|---------------------------|
| Route added/changed/removed | README, API docs, Swagger |
| `ENV["NEW_VAR"]` or `ENV.fetch` | `.env.example`, README setup |
| Method signature changed | Docstring, caller documentation |
| Database migration | Schema documentation, ERD |
| New gem/package added | README prerequisites |
| Config key added/removed | Config templates, deployment docs |
| Error message changed | User-facing documentation, support docs |
| Feature flag added/removed | Feature documentation, rollout guides |

## 出力フォーマット

陳腐化を検出した場合は、レビューにドキュメントのセクションを含める。

```markdown
## Documentation Staleness

| File | Issue | Action |
|------|-------|--------|
| `README.md:45` | New `REDIS_URL` env var not documented | Add to setup section |
| `.env.example` | Missing `REDIS_URL` entry | Add with default value |
| `app/services/foo.rb:12` | Docstring says 2 params, method now takes 3 | Update docstring |
| `docs/api.md` | `/api/v1/posts` endpoint removed but still documented | Remove section |

**Severity**: P3 (does not block merge, but should be addressed)
```

## スコープのルール

**リポジトリ内にあり**、かつ**差分に直接影響を受ける**ドキュメントのみを指摘する。以下は指摘しない。
- 外部の wiki ページ（指摘ではなくリマインダーとして言及する）
- 変更されていないコードのドキュメント
- もともと存在しなかった、不足しているドキュメント（これは別の関心事である）
