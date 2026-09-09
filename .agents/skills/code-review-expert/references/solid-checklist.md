# SOLID Smell Prompts

## SRP (Single Responsibility)

- 1 つのファイルが無関係な関心事を抱えている（例: HTTP + DB + ドメインルールが 1 ファイルに同居）
- 凝集度が低い、または変更理由が複数ある大きなクラス/モジュール
- 無関係な多くのステップを取りまとめている関数
- システムについて知りすぎている God object
- **Ask**: 「このモジュールが変更される唯一の理由は何か？」

## OCP (Open/Closed)

- 新しい振る舞いを追加するのに多数の switch/if ブロックの編集が必要
- 機能の拡張に、拡張ではなくコアロジックの修正が必要になる
- バリエーションのための plugin/strategy/hook ポイントがない
- **Ask**: 「既存コードに触れずに新しいバリアントを追加できるか？」

## LSP (Liskov Substitution)

- サブクラスが具象型をチェックしたり、基底メソッドで例外を投げたりする
- オーバーライドしたメソッドが事前条件を強めたり、事後条件を弱めたりする
- サブクラスが親の振る舞いを無視したり no-op にしたりする
- **Ask**: 「呼び出し側に気づかれずに任意のサブクラスへ差し替えられるか？」

## ISP (Interface Segregation)

- メソッドが多く、その大半が実装側で使われていないインターフェース
- 狭い用途のために広いインターフェースへ依存している呼び出し側
- インターフェースメソッドの空実装/スタブ実装
- **Ask**: 「すべての実装側がすべてのメソッドを使っているか？」

## DIP (Dependency Inversion)

- 高レベルのロジックが具象的な IO・ストレージ・ネットワーク型に依存している
- 抽象化や注入ではなく、ハードコードされた実装を使っている
- ビジネスロジックをインフラに結合させる import の連鎖
- **Ask**: 「ビジネスロジックを変更せずに実装を差し替えられるか？」

---

## Common Code Smells (Beyond SOLID)

| Smell | Signs |
|-------|-------|
| **Long method** | 関数が 30 行超、ネストが多層 |
| **Feature envy** | メソッドが自分のクラスより他クラスのデータを多く使っている |
| **Data clumps** | 同じパラメータの組が繰り返し一緒に渡されている |
| **Primitive obsession** | ドメイン型の代わりに文字列/数値を使っている |
| **Shotgun surgery** | 1 つの変更に多数のファイルへの編集が必要 |
| **Divergent change** | 1 つのファイルが無関係な多くの理由で変更される |
| **Dead code** | 到達不能、または一度も呼ばれないコード |
| **Speculative generality** | 仮定上の将来ニーズのための抽象化 |
| **Magic numbers/strings** | 名前付き定数のないハードコードされた値 |

---

## Refactor Heuristics

1. **Split by responsibility, not by size** - 小さなファイルでも SRP に違反し得る
2. **Introduce abstraction only when needed** - 2 つ目のユースケースが現れるまで待つ
3. **Keep refactors incremental** - 移動する前に振る舞いを切り離す
4. **Preserve behavior first** - 再構築の前にテストを追加する
5. **Name things by intent** - 命名が難しいなら、その抽象化は間違っているかもしれない
6. **Prefer composition over inheritance** - 継承は密な結合度を生む
7. **Make illegal states unrepresentable** - 型を使って不変条件を強制する
