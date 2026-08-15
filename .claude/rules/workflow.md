# 標準開発ワークフロー

方針の正本は `AGENTS.md`。このファイルは手順詳細（使用ツール・コマンド）を補足する。**Claude Code と Codex の両方が対象**で、エージェント固有の操作は「Claude Code は〜 / Codex は〜」と併記する。

実装する際は、以下の手順を自動的に実行すること：

1. **Worktree作成**
   - **必須: コード変更を伴う作業は issue の有無を問わず必ず worktree を作成して行う。メインリポジトリ（main）で直接作業しない。**
   - `git fetch origin main` で main を最新化する
   - ブランチ命名規則: `<type>/issue-<番号>-<内容>` 形式（例: `feat/issue-12-add-vwap-session`）
     - `<type>` は feat / fix / refactor / docs / test / chore / perf / ci のいずれか
     - `<内容>` は kebab-case で30文字程度に要約する
     - issue 番号が紐付かない依頼の場合は `<type>/<内容>` で省略可
   - **作成方法（重要）**: 規約どおりのブランチにするため、次の2段階で作成する:
     1. `git worktree add --no-track -b <ブランチ名> .claude/worktrees/<ディレクトリ名> origin/main`
        （`<ディレクトリ名>` はブランチ名の `/` を `+` に置換したもの。`--no-track` を付けて upstream が `origin/main` に張られるのを防ぐ。upstream は手順7の初回 `git push -u origin HEAD` で張る。同名ローカルブランチが既に在る場合は `-b` を外して `git worktree add .claude/worktrees/<ディレクトリ名> <ブランチ名>`）
     2. そのworktreeを作業ディレクトリにする
        - Claude Code: `EnterWorktree` に `path=.claude/worktrees/<ディレクトリ名>` を渡す（`name=` を渡す自動作成は使わない）
        - Codex: 以降のコマンドを worktree 内で実行する（`codex exec` なら `--cd <worktreeの絶対パス>` で固定する）
   - worktree作成後、venv を作成して依存関係をインストールする:
     `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`
   - `.env` を使う作業の場合はメインリポジトリから手動でコピーする（gitignore 対象のため worktree には含まれない）

2. **実装**（worktree内で作業）
   - 指示された要件を実装
   - 必要に応じてテストを追加/更新
   - **DBスキーマ変更時は必須**: `migrations/` に連番 SQL を追加し（適用済みSQLは書き換えない）、`storage/` の対応を更新する

3. **品質チェック**（worktree内で実行）
   - `ruff check .` で lint チェック（自動修正は `ruff check . --fix`）
   - `pytest` を実行（broker テストはMT5なし環境で自動skip、integration はDBなしなら対象外）

4. **コードレビュー**（PR 作成前は必須）
   - 現在の git 差分をレビューする（P0–P3 で指摘）。Claude Code は `/code-review-expert` スキル、Codex は `AGENTS.md`「AIレビュー指示」の観点で自己レビューする
   - P0（Critical）・P1（High）・P2（Medium）の指摘はユーザーへの確認を挟まず自動修正し、修正後は品質チェック（手順3）を再実行する
   - 仕様判断が必要な指摘（ASK 分類）と P3（Low）のみ、本 PR で対応するか follow-up issue 化を判断する
   - ドキュメントのみ・軽微なテキスト修正のみの変更ではスキップ可

5. **コミット**
   - 変更をステージング
   - 意味のあるコミットメッセージで記録（lefthook の pre-commit を必ず通す）
   - issue 紐付き時はコミットメッセージに `Fixes #<issue番号>` を含める（紐付かない依頼では不要）

6. **プルリクエスト作成**
   - 変更をリモートにプッシュ（初回は `git push -u origin HEAD` で upstream を張る）
   - `gh pr create` でPRを作成
   - **issue 紐付き時は PR 本文の先頭に `Closes #<issue番号>` を記載**（squash merge でも確実に閉じるため、コミットの `Fixes #N` と両方書く）
   - PR の説明に issue の内容と実装内容を記載
   - **PR 作成直後に Codex レビューを依頼する**: `gh pr comment <PR番号> --body "@codex review"`

7. **Worktree終了**
   - PRがマージされたらworktreeを片付ける
     - Claude Code: `ExitWorktree`
     - Codex: `git -C <メインリポジトリの絶対パス> worktree remove <worktreeの絶対パス>`（worktree 内からは相対パスが worktree 配下として解決されるため必ず絶対パスで指定する）

## issue 連動チェックリスト

issue 起票済みの依頼を実装する際は以下を必ず満たす:

- [ ] worktree/ブランチ名に `issue-<番号>` が含まれている
- [ ] コミットメッセージに `Fixes #<番号>` を含む
- [ ] PR 本文先頭に `Closes #<番号>` を記載した
- [ ] PR 作成直後に `@codex review` コメントを投稿した
