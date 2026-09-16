# issue #130: MT5 `history_deals_get` の `None` を無条件に取得失敗として扱う（account collector）

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-130-deals-none-is-failure`
- ブランチ: `fix/issue-130-deals-none-is-failure`（base は `origin/main` = `6b6281d`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## 背景（issue #130 の要点）

`src/trading/data/account/collector.py` の `_realized_pnl_day` は、`history_deals_get()` が `None` を返したとき
`last_error()` のコードを見て分岐している。

- `code != RES_S_OK` → `MT5ConnectionError` を送出
- `code == RES_S_OK` → 「当日まだ約定が無い」とみなして空タプル扱い（`realized_pnl_day=0` を保存）

これは `src/trading/execution/mt5/adapter.py:87` のコメント
「MT5 getters return None on API failure and an empty tuple when nothing exists. The two must never be conflated.」
と整合しない。`MT5ExecutionAdapter` 側の 6 箇所（`positions_get` / `orders_get` / `history_orders_get` /
`history_deals_get`）はすべて `None` を無条件に `MT5ConnectionError` にしている。

issue が「未確定」としていた点は、2026-09-16 に Windows 実機（OANDA-Japan MT5 Demo、MT5 build 6140）で実測済み:

```
該当 deal の無い窓: history_deals_get(from, to) -> type=tuple value=() len=0 / last_error() -> (1, 'Success') / history_deals_total -> 0
30 日窓: 16 件の TradeDeal を含むタプル / last_error() -> (1, 'Success')
```

**該当 deal 無しは空タプルで返り、`None` は返らない。** したがって `None` は取得失敗だけを意味する。

## 確定している方針（変更しない）

issue の候補 2 を採用する。`_realized_pnl_day` は `None` を `last_error()` の値に関わらず `MT5ConnectionError` にし、
adapter と前提を一本化する。空結果が空タプルなので、約定の無い日に collector が落ちる懸念は無い。
候補 3（`history_deals_total` での確認）は不要。

---

## 変更範囲

対象ファイルは `src/trading/data/account/collector.py` と `tests/unit/test_account_collector.py` の 2 つ。ほかは触らない。

### 1. `src/trading/data/account/collector.py`

`_realized_pnl_day`（現在 131〜149 行）を次のようにする。

- `raw is None` なら `last_error()` の内容を含めて `MT5ConnectionError` を送出する。`code != RES_S_OK` の分岐と
  `raw = ()` へのフォールバックは消す。
- 例外メッセージの書式は **現状の `f"history_deals_get failed: ({code}, {description})"` を維持する**
  （`last_error()` を `code, description` に unpack して埋める）。既存テスト `test_history_fetch_failure_raises` が
  `history_deals_get failed: \(-10004, history unavailable\)` で照合しており、これを無変更で通すため。
  adapter 側の `f"... {self._mt5.last_error()}"`（tuple の repr。文字列に引用符が付く）には揃えない。
- 分岐の上にある日本語コメント（現在 137〜141 行、adapter との違いと issue #130 での追跡を説明している 5 行）は、
  実測で前提が確定したので書き換える。書くのは
  「MT5 は該当 deal が無いとき空タプルを返し、`None` は取得失敗だけを意味する（実機で確認済み）ので、
  adapter と同じく無条件に失敗として扱う」という**理由だけ**。issue 番号やコミット文脈に依存する記述、
  「以前はこうだった」という記述は残さない。2〜3 行に収める。
- モジュール定数 `RES_S_OK = 1`（現在 50〜52 行、上のコメント 2 行を含む）は、この分岐以外に使用箇所が無いので
  **削除する**（`rg -n RES_S_OK src tests` で確認。テスト側の import も 2. で消す）。
- モジュール docstring、`build_snapshot`、`collect_once`、`run()`、`main()` は変更しない。

### 2. `tests/unit/test_account_collector.py`

- `from trading.data.account.collector import (RES_S_OK, ...)` から `RES_S_OK` を外す。
- `test_no_deals_with_a_success_status_records_zero`（現在 312〜328 行）は前提が変わったので削除し、代わりに
  「`history_deals_get` が `None` を返したら、`last_error()` が成功コード `(1, "Success")` でも
  `MT5ConnectionError` になる」テストを置く。`FakeMt5(history_deals_none=True, error=(1, "Success"))` を使い、
  `pytest.raises(MT5ConnectionError, match=r"history_deals_get failed: \(1, Success\)")` で照合する。
  スナップショットが保存されていないこと（`repository.snapshots == []`）も 1 行で確認する。
  成功コードの `1` はテスト内のリテラル（または短いローカル定数）でよい。collector に定数を残さない。
- 「`history_deals_get` が**空タプル**を返したら `realized_pnl_day == Decimal(0)` を記録し、スナップショットが
  保存される」テストを 1 本足す。`FakeMt5()` の既定（`deals=()`、`history_deals_none=False`）で空タプルが返る。
  既存の `test_collect_once_appends_the_observation_to_the_series` は `realized_pnl_day` を assert していないので、
  別テストとして足す（既存テストは変更しない）。
- `test_history_fetch_failure_raises` は無変更で通る。
- `FakeMt5` の `history_deals_none` / `error` はそのまま使う（引数の追加・削除はしない）。
- テスト関数名は既存の流儀（`test_<振る舞いを文で>`）に合わせる。

### 触らないもの

- `src/trading/execution/mt5/adapter.py`（6 箇所の `None` 扱いはすでに正しい）
- `AccountSnapshotCollector.run()` の例外処理
- `tests/unit/test_invariants.py`、`tests/unit/test_adapter.py`、`tests/unit/test_backtest_daily_pnl.py`
- `docs/`（issue #136 の記録は別途行う）

---

## やらないこと

- 候補 3（`history_deals_total` での二重確認）
- adapter 側の変更、例外メッセージ書式の統一
- `AccountSnapshotCollector.run()` の例外処理・リトライの追加
- 周辺リファクタ・無関係な整形・docstring の言語変更

## 実装上の規約

- 検証はシステム境界（MT5 の応答）のみ。内部関数間に防御的分岐・フォールバックを足さない。
- WHAT を説明するコメント、コミット文脈に依存するコメント（「issue #130 のために変更」等）を書かない。理由（WHY）だけ短く書く。
- 日本語コメント優先。既存の英語 docstring はそのまま。
- 引数や共有オブジェクトを破壊しない。

## 完了条件

worktree 内で次がすべて通ること。

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` 無指摘
- 上記 3 ディレクトリの pytest が green（`tests/broker` は MT5 なしで自動 skip、`tests/integration` は PostgreSQL 必須のためここでは対象外）
- `rg -n RES_S_OK src tests` がヒット 0

## 影響範囲の確認

```
rg -n "RES_S_OK|_realized_pnl_day|history_deals_get" src tests
```

## コミットしないもの

- `tmp/`（Codex のログ・セッション ID）
- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md`

このファイル（`tasks/issue-130.md`）はコミットに含める。
