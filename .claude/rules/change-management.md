# 変更管理と影響範囲確認

影響範囲チェックの方針は `AGENTS.md` の「変更管理」節を正本とする。
このファイルは具体的な検索コマンド例のみを載せる。

## 影響範囲の確認コマンド（rg を使用）

```bash
# 関数・クラスを削除/リネームする場合
rg -w "symbol_name" src tests

# strategy_id を変更する場合（config・テスト・ドキュメントにも現れる）
rg "strategy_id_value" src config tests docs

# feature 名（intelligence/features.py の定数値）を変更する場合
rg "feature_name" src config tests

# config キーを変更する場合（YAML 6環境 + ローダー + テスト）
rg "config_key" config src/trading/config.py tests

# テーブル・カラムを変更する場合（migration は追記、storage の SQL も追う）
rg -w "column_name" migrations src/trading/storage tests
```

## 注意点

- 定義・参照箇所の構造把握は Serena MCP（`find_symbol` / `find_referencing_symbols`）を優先し、全文検索は rg を使う
- enum 値（`CommandState` / `FillOrigin` 等）は `migrations/0001_initial.sql` の CHECK 制約にも埋まっているため、値の追加・変更時は SQL 側も確認する
- ドメイン不変条件に触れる変更（`StrategyContext` のフィールド追加、OMS 状態遷移の変更等）は `tests/unit/test_invariants.py` / `tests/unit/test_state_machine.py` が落ちる。テストを緩める前に ADR を追加して合意を取る
