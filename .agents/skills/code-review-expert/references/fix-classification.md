# Fix Classification: AUTO-FIX vs ASK

## 判断表

問題を見つけたら、AUTO-FIX（確認なしで修正してよい）か ASK（ユーザーの確認が必要）かを分類する。

| カテゴリ | AUTO-FIX | ASK |
|----------|----------|-----|
| **整形 / スタイル** | 空白、末尾カンマ、import 順序 | 公開 API のリネーム、規約の変更 |
| **デッドコード** | return/throw 後の到達不能コード | feature flag の背後にあるコード、TODO 付きのコメントアウトされたブロック |
| **Null 安全性** | 内部コードへの nil/null ガード追加 | 公開メソッドのシグネチャを非 null 化する変更 |
| **型エラー** | 明らかな型の不一致、型注釈の欠落 | ジェネリック型の変更、union 型の拡大/縮小 |
| **セキュリティ** | ユーザー入力のエスケープ、パラメータ化クエリの追加 | 認証フローの変更、CORS ポリシーの変更、トークンの取り扱い |
| **パフォーマンス** | 定数への `.freeze` 追加、未使用の `SELECT *` 列の削除 | クエリ戦略の変更、インデックスの追加/削除、キャッシュ |
| **エラー処理** | 既知の例外に対する欠落した `rescue`/`catch` の追加 | エラー伝播戦略の変更、リトライロジック |
| **テスト** | 壊れたアサーションの修正、期待値の更新 | テストの削除、テスト戦略の変更、境界のモック化 |
| **依存関係** | 浮動バージョンの固定 | メジャーバージョンの更新、新規依存関係の追加 |
| **データベース** | モデルへの欠落したバリデーション追加 | 列型の変更、マイグレーションの追加/削除 |

## 分類ルール

1. **次のすべてを満たすなら AUTO-FIX:**
   - 変更が局所的である（呼び出し元ではなく当該コードのみに影響する）
   - 振る舞いが保たれる、または厳密に改善される
   - 公開 API/インターフェースの変更がない
   - ロールバックが容易である（その行を revert するだけ）
   - 主観的な判断を必要としない

2. **次のいずれかを満たすなら ASK:**
   - 変更が他のファイルや呼び出し元に影響する
   - 有効な手法が複数存在する
   - トレードオフが伴う（パフォーマンス vs 可読性、DRY vs 明快さ）
   - 公開 API/契約に対する破壊的変更である
   - 妥当性の検証にドメイン知識を必要とする
   - データマイグレーションまたはスキーマ変更を伴う

## 質問のまとめ方

ASK 項目が複数ある場合は、テーマごとにまとめて単一の判断ポイントとして提示する。

```markdown
### Decisions needed

**Error handling strategy** (affects 3 files):
- Option A: Raise and let caller handle (current pattern in `app/services/`)
- Option B: Return Result object (used in `app/models/`)
- Recommendation: Option A — consistent with existing codebase

**Cache invalidation** (affects 2 endpoints):
- The `index` and `show` actions both fetch stale data after update.
- Option A: Add `expires_in: 5.minutes` (simple, eventual consistency)
- Option B: Explicit cache bust on write (immediate consistency, more code)
- Recommendation: Option B — data accuracy matters here
```

## 出力フォーマット

レビュー出力の各指摘にタグを付ける。

```markdown
1. **[file:line]** Missing null check on `user.profile` [AUTO-FIX]
   - Added `&.` safe navigation operator

2. **[file:line]** N+1 query in `#index` action [ASK]
   - Option A: `includes(:posts)` — eager load
   - Option B: Counter cache column — denormalize
   - Recommendation: Option A (simpler, no migration)
```
