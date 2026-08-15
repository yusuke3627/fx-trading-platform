## 言語設定

**全てのやり取りを日本語で行うこと。** コード内のコメントやcommitメッセージも日本語を優先する（既存の英語docstringは維持してよい）。

---

## ドキュメント構成

このファイル（AGENTS.md）が Codex / Claude 共通の**正本**。`.claude/rules/` には両者共通の操作手順・コマンド例の補足のみを置き、方針の二重管理を避ける。ディレクトリ名は `.claude/` だが **Codex も対象**で、作業に入る前に該当ファイルを読むこと。エージェント固有の操作は各ファイル内で「Claude Code は〜 / Codex は〜」と併記する。

- 設計の正本: [`docs/SYSTEM_SPEC.md`](docs/SYSTEM_SPEC.md)（**v1.3 で凍結**。変更は本文改訂ではなく `docs/adr/` へ ADR 追加）
- 構成規約: [`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md)
- 相場観・研究仮説: `docs/research/`（設計に固定しない）

---

## プロジェクト概要

**fx-trading-platform** は、個人・自己資金運用の FX アルゴリズム取引プラットフォーム。初期対象は USD/JPY（OANDA 東京サーバー / MT5 経由執行）だが、構造は通貨ペア非依存。

Windows 1台上のモジュラーモノリスとして、Collectors → Point-in-Time Event Store → Fundamental/Regime → Strategy（scalp/intraday/swing）→ Portfolio → Risk → OMS → MT5 Execution のパイプラインを構成する。

---

## 技術スタック

- Python 3.11+ / pydantic v2 / PyYAML
- DB: PostgreSQL（psycopg 3、`db` extra）。スキーマは `migrations/*.sql`
- 執行: MetaTrader5 Python Integration（Windows 専用、`mt5` extra）
- テスト: pytest（+ pytest-asyncio）
- Lint: ruff（設定は `pyproject.toml`）
- Git hooks: lefthook

---

## 共通コマンド

```bash
# セットアップ
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # Windows 実行ホストでは '.[dev,db,mt5]'

# テスト
pytest                            # 全テスト（broker はMT5なし環境で自動skip）
pytest tests/unit/<file>          # 特定ファイル
pytest tests/unit/<file> -k name  # テスト名で絞り込み

# Lint
ruff check .
ruff check . --fix

# DB スキーマ適用（DSN は環境変数 TRADING_DB_DSN）。連番順に全て流す
for f in migrations/*.sql; do psql "$TRADING_DB_DSN" -v ON_ERROR_STOP=1 -f "$f"; done

# MT5 Demo Preflight（Windows + MT5 ターミナルでのみ実行可）
python -m trading.execution.mt5.preflight --env demo --symbol USDJPY

# Git hooks
lefthook install
```

---

## ワークフロー設計

### 1. 着手と計画
- 行動できるだけの情報が揃ったら着手する。解釈の違いで成果物が大きく変わるときだけ、実装前に方針を確認する
- アーキテクチャに関わる大きなタスクは、実装前に方針を短く共有して認識を合わせる
- 途中でうまくいかなくなったら、無理に進めずすぐに立ち止まって再計画する

### 2. サブエージェントへの委任
- 委任するのは、本当に独立していて並列化できる大きな作業だけ（広範な多ファイル調査など）
- 数回のツール呼び出しで終わる作業は委任しない。自分の作業の検証・ダブルチェックにサブエージェントを使わない
- 1体で足りるなら1体。起動数は低く保つ

### 3. 教訓の記録
- ユーザーから修正を受けたパターンは `tasks/lessons.md` に記録する（1教訓ごとに、なぜ重要だったかを添える）
- リポジトリやチャット履歴が既に持っている情報は保存しない。重複を作らず既存の教訓を更新し、誤りと分かったものは消す
- セッション開始時に、そのタスクに関連する lessons を参照する

### 4. 完了報告は証拠に基づく
- 報告する前に、各主張をこのセッションのツール結果（テスト・ログ・差分）と突き合わせる。証拠を指せる作業だけ完了と報告し、未検証なら未検証と明言する
- テストが落ちたら出力付きでそう報告する。飛ばした手順は飛ばしたと言う
- 別立ての「最終検証ステップ」や検証用サブエージェントを足さない（既定の検証挙動に任せる）

### 5. 自律的なバグ修正
- バグレポートを受けたら、手取り足取り教えてもらわずにそのまま修正する
- ログ・エラー・失敗しているテストを見て、自分で解決する
- 言われなくても、失敗しているCIテストを修正しに行く
- **ただし依頼の動詞でスコープを区切る**：「調査して」「確認して」「教えて」「原因は」と明示された依頼は、**調査・報告までで止め、コード変更・worktree作成・コミット・PR作成へ進む前に承認を取る**（issue番号が紐づいていても同じ）。「実装して」「修正して」「対応して」「進めて」と言われて初めて実装に着手する。報告に修正方針を含めるのは可だが、実際のコード変更は次のgoサインを待つ。

### 6. PR 作成前にコードレビューをかける（必須）
- コード変更を実装したら、**PR を作成する前に必ず現在の git 差分をレビューする**（実装後のセルフレビューでは指摘の提示で止まらず修正まで進める。ただし無関係な大規模リファクタはしない）
  - Claude Code: `/code-review-expert` スキルを使う
  - Codex: 本ファイル末尾「AIレビュー指示」のレビュー観点と重要度ラベルに沿って、自分で差分をレビューする
- P0（Critical）・P1（High）・P2（Medium）の指摘は**ユーザーへの確認を挟まず自動修正**し、コミット／PR 前に必ず解消する。仕様判断が必要な指摘（ASK 分類）と P3（Low）のみ、本 PR で対応するか follow-up issue 化を判断する
- 「調査して」「確認して」等でコード変更に進んでいない場合、およびドキュメントのみ・軽微なテキスト修正のみの変更はスキップしてよい
- 手順への組み込み位置は `.claude/rules/workflow.md` を参照

---

## プロジェクト固有ルール

### ブランチ・worktree運用
- **コードを変更する作業は issue の有無を問わず必ず worktree を作成して行う。メインリポジトリ（main）で直接編集しない**
- ブランチ名: issue 紐付きは `<type>/issue-<番号>-<内容>`、紐付かない依頼は `<type>/<内容>` 形式
- `<type>` は feat / fix / refactor / docs / test / chore / perf / ci のいずれか
- ブランチ名は必ずこのスラッシュ区切り形式にする。worktree 補助ツールが自動生成する `worktree-` プレフィックス付きの名前はそのまま採用しない（`git worktree add` を使った具体手順は `.claude/rules/workflow.md` を参照）
- issue 紐付き時はコミットメッセージに `Fixes #<番号>`、PR 本文の先頭に `Closes #<番号>` を記載する
- **PR 作成直後に PR へ `@codex review` とコメントし、Codex のレビューを依頼する**（`gh pr comment <PR番号> --body "@codex review"`）

### テストルール
- フレームワークは pytest。共有ファクトリは `tests/support.py` に置く
- テストデータに実在する人物・団体名を使わない（架空値のみ）
- ディレクトリの役割: `tests/unit`・`tests/replay`・`tests/failure` はローカルで常時実行 / `tests/broker` は Windows + MT5 環境のみ（他環境では自動skip）/ `tests/integration` は PostgreSQL 必須（`tests/integration/README.md` 参照）
- **`tests/unit/test_invariants.py` を通すためにテスト側を緩めない。** 不変条件テストが邪魔になる変更は設計変更を意味するので、先に `docs/adr/` へ ADR を追加して合意を取る
- グローバル設定の「カバレッジ80%必須 / TDD必須」方針は本プロジェクトでは**不採用**（変更した振る舞いへのテスト追加を基本とする）

### Git hooks
- commit前の品質チェックはlefthookで行う（pre-commit: ruff / pre-push: pytest）
- コミット時はlefthookのpre-commitを必ず通す
- `git commit --no-verify` はユーザーが明示した場合を除き使用しない
- lefthookでcommitが止まった場合は、ログを読み、原因を修正してから再度 `git add` と `git commit` を実行する

### 変更管理
- 機能やコードを追加・修正・削除する際は、影響範囲を確認する
- 構造把握（定義・参照箇所）は Serena MCP を優先し、全文検索は `grep` ではなく `rg` を使う
- `strategy_id`・feature 名（`intelligence/features.py` の定数）・config キーは、コード / `config/*.yaml` / `tests/` / `migrations/` をまたいで参照されるため、変更時は横断検索で追う
- ドメイン不変条件（下記）に触れる変更は ADR 追加とセットで行う
- 具体的な検索コマンド例は `.claude/rules/change-management.md` を参照

### DB変更
- スキーマ変更は `migrations/` に連番 SQL（`0002_*.sql` …）を追加する。適用済みマイグレーション（`0001_initial.sql` 含む）は書き換えない
- テーブル追加・変更時は `storage/repository.py` / `storage/postgres.py` の対応と、`docs/SYSTEM_SPEC.md` の整合を確認する

### 取引システム固有の不変条件（最重要）
以下は `tests/unit/test_invariants.py` ほかでコード化されており、**壊す変更は原則 reject**:

- Strategy / LLM 層から Broker・OMS・DB へ到達できない（`StrategyContext` に執行系を追加しない）
- `UNKNOWN` コマンドは再送せず Reconciliation でのみ解決する（`SUBMITTING` → `READY` の再claim禁止）
- Exit は裸の反対売買にしない（fresh position select + ticket 参照。Protection 決済済みなら NOOP）
- Backtest は `known_at <= replay_clock.now()` のデータしか見えない（look-ahead 禁止）
- Broker 最小ロットが Risk 許容量を超える場合は取引しない（`MINIMUM_BROKER_SIZE_EXCEEDS_RISK`）
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない
- Strategy 内で `datetime.now()` を直接呼ばない（Clock 注入）

---

## タスク管理

- 通常の作業計画は会話内またはCodexのplanで管理する
- 長期作業・複数セッションにまたがる作業だけ、必要に応じて `tasks/issue-<番号>.md` を作成する
- ユーザーから修正を受けたパターンは `tasks/lessons.md` に記録する

---

## コア原則

- **シンプル第一**：すべての変更をできる限りシンプルにする。影響するコードを最小限にする。
- **手を抜かない**：根本原因を見つける。一時的な修正は避ける。シニアエンジニアの水準を保つ。
- **影響を最小化する**：変更は必要な箇所のみにとどめる。バグを新たに引き込まない。

---

## AIレビュー指示（Codex / Claude 等のレビューエージェント向け）

### レビュー言語

- **必ず日本語でレビューする。** 要約・指摘・提案はすべて日本語で記述する。
- コードの識別子（変数名・関数名）は英語のままでよいが、説明文は日本語にする。

### レビュー観点

以下を重視してレビューすること。

1. **シンプルさ**
   - 変更の影響範囲が最小限になっているか
   - 過剰設計、早すぎる抽象化になっていないか
   - 不要なエラーハンドリング・フォールバック・後方互換シムを足していないか
   - テストでの使用が確認できない後方互換用のデッドコードを足していないか

2. **ドメイン不変条件との整合（最重要）**
   - Strategy / LLM 層から Broker・OMS・DB へ到達するコードを足していないか
   - `UNKNOWN` コマンドの再送・`SUBMITTING` の re-claim を許すパスを作っていないか
   - Exit が裸の反対売買（ticket 参照なし・fresh select なし）になっていないか
   - Backtest の look-ahead（`known_at` 未来参照）を作っていないか
   - LONG/SHORT と BUY/SELL の混同、Strategy 内での `datetime.now()` 直接呼び出しがないか
   - 通貨ペア・pip size・時間足のハードコード（`InstrumentSpec` / config を経由しない値）がないか

3. **Python / pydantic 慣習との整合**
   - ruff ルールに準拠しているか。型注釈が付いているか
   - 金額・数量・価格に float を使っていないか（Decimal を使う。Indicator 計算のみ float 可）
   - 引数や共有オブジェクトを破壊していないか（frozen モデル + `model_copy` のパターンを維持）
   - 検証はシステム境界（設定・外部API・Broker応答）のみで行い、内部関数間に防御的分岐を足していないか

4. **テスト**
   - 変更した振る舞いに対応するテストがあるか。実装の写経になっていないか
   - 通すために既存テスト（特に `test_invariants.py`）側を緩めていないか
   - テストデータに実在の人物・団体名を使っていないか

5. **セキュリティ**
   - ハードコードされたシークレット、API キー、口座番号、DSN がないか
   - SQL はプレースホルダで組んでいるか（文字列連結していないか)
   - Broker credential・LLM への権限付与が設計境界を越えていないか

6. **コメント / ドキュメント**
   - WHAT を説明する不要なコメントを書いていないか（識別子で表現する）
   - 「○○のために追加」などのコミット文脈に依存するコメントを書いていないか
   - AI レビューの引用や指摘元をコードコメントに残していないか

### 重要度ラベル

指摘には以下のラベルを付けて優先度を明確にすること。

- **Critical**: バグ、セキュリティ、資金・注文に関わるリスク、不変条件違反。マージ前に必ず修正。
- **High**: 設計上の問題、テスト不足など、マージ前の修正を強く推奨。
- **Medium**: 可読性・保守性向上のための改善提案。可能なら対応。
- **Low / Nit**: スタイル、命名など好みの範囲。任意対応。

### やらないでほしいこと

- 英語でのレビュー（理由がない限り日本語で書く）
- リファクタ提案を本筋の指摘に混ぜ込む（別セクションに分離する）
- スコープ外の変更を促す指摘（PR の目的に沿った範囲に絞る）
- 複数箇所に及ぶシステム的な修正を現在の PR で部分的に行うよう求めること（必要なら別 issue 化を促す）
- 「LGTM」だけの空コメント（指摘がない場合も観点ごとに簡潔に所感を残す）
- 純粋なリファクタリング PR で振る舞いの変更を混ぜ込むこと（差分が混在したら別 PR に分離する指摘を出す）
