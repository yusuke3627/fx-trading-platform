# issue #164: ablation_compare の CI90 に block bootstrap を入れる（約定損益の自己相関）

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-164-block-bootstrap-ci`
- ブランチ: `feat/issue-164-block-bootstrap-ci`（base は `origin/main` = `d70fd67`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #164 本文（全文転記）

> タイトル: ablation_compare の CI90 に block bootstrap を入れる（約定損益の自己相関）
>
> ### 背景
>
> PR #163（H4 の研究ノート）のレビューで指摘された内容。
>
> `ablation_compare.arm_summary` が呼ぶ `bootstrap_interval` は、約定ごとの損益を 1 件ずつ復元抽出する。つまり各約定を独立で交換可能な標本として扱っている。しかし同じ戦略の連続した約定は同じ日や同じボラティリティ局面に固まりやすく、損益に自己相関があると有効標本数を過大に見積もって CI が本来より狭くなる。
>
> `policy_event_study.bootstrap_interval` は ablation の判定（`judge`）とイベントスタディの双方で使われているので、影響は H4 のノートに閉じない。
>
> ### やること
>
> - 日やセッションを単位にした block bootstrap を `ablation_compare` に入れ、既存の i.i.d. bootstrap と並べて出す
> - ブロックの単位（JST 暦日 / broker 営業日 / セッション）と、block 長の決め方を先に決める
> - 既存の判定規則（`KEEP` / `REMOVE` / `UNDECIDED_*`）をどちらの区間で評価するかを決める。事前登録との整合が要るので、既存ノートの判定を遡って上書きしない
>
> ### 本 PR で対応しない理由（PR #163 側の記述）
>
> PR #163 はドキュメント 1 ファイルの追加で、判定は issue #148 で実行前に固定した規則（`ablation_compare` の bootstrap による CI90）をそのまま当てたもの。結果を見てから統計手法を差し替えると事前登録の意味が無くなるため、手法の変更は別途行う。再計算には VPS 上の run 成果物が要る点も、ノート単体の PR では扱えない。

---

## ユーザー判断で確定していること（変更しない）

- **ブロック単位は broker 営業日**（broker の壁時計の暦日。NY 17:00 クローズが broker の深夜に固定されている軸）。block 長は 1 営業日固定で、可変長・セッション分割はしない。
- **判定（`judge`）は既存の i.i.d. 区間のまま**。issue #148 で事前登録した規則を変えない。
- **block 区間は参考値**として i.i.d. 区間の隣に並べて出す。判定規則に採用するには事前登録の更新が要る。
- 既存の研究ノート（`docs/research/`）の判定・数値は遡って書き換えない。

## 前提として確認済みの事実（実装で使う）

- `trades.csv` の `entry_at` は **broker ラベル軸**の値である。経路: `ExecutionSimulator` が `Fill.broker_time=tick.time`（`src/trading/backtest/simulator.py:195, 335`）を付け、engine が `state.entry_at[ticket] = fill.broker_time`（`src/trading/backtest/engine.py:997`）として `TradeRecord.entry_at` に入れ、`write_report` が `isoformat()` で書く。`Tick.time` は broker の壁時計を `+00:00` として刻印した値（ADR-005、`src/trading/backtest/research.py` の `broker_label` docstring）。
- manifest の `period_from` / `period_to` も同じ broker ラベル軸（`research.py` の `--from/--to` は `broker_label` で解釈され、`args.start.isoformat()` がそのまま manifest に入る）。`verify_comparable` の `period_from <= at < period_to` はラベルどうしの比較で整合している。
- したがって **ブロック鍵は `entry_at.astimezone(UTC).date()` そのもの**。`known_to_broker_label` / `broker_label_to_known`（`src/trading/data/market/dukascopy.py:67` / `src/trading/data/market/clock.py:8`）を `entry_at` に**重ねて掛けない**（既にラベル軸なので、掛けると anchor 分だけ二重にずれる）。manifest の `broker_server_ahead_of_ny_hours` はこの写像に不要。
- `RunArtifacts.pnls` は `entry_id` 単位（部分決済を合算）だが、`RunArtifacts.entry_ats` は現状 **行単位**（部分決済の行ぶん重複する）。ブロック鍵を `pnls` と揃えるには entry 単位の `entry_at` が要る（下記 1.）。

---

## 変更範囲

対象ファイルは `src/trading/backtest/ablation_compare.py` と `tests/unit/test_ablation_compare.py` の 2 つ。ほかは触らない。

### 1. `load_run` / `RunArtifacts`: `entry_ats` を entry 単位に揃える

`load_run` の読み取りループで `pnls_by_entry` と同じ順序で entry ごとの `entry_at` を集める（同じ `entry_id` の行はすべて同じ `entry_at` を持つので、最初に見た行の値を採る。`dict.setdefault` で十分）。`RunArtifacts.entry_ats` をこの entry 単位のリストにし、`pnls` と同じ長さ・同じ順序で整列させる。

- 部分決済の行を重複させない以外、`entry_ats` の用途（`run_coverage` の first / last / months、`verify_comparable` の期間外判定）は結果が変わらない（同じ `entry_at` の重複を落とすだけ）。
- 行数の検査（`row_count` と `summary.metrics.trades` の一致）は行単位のまま。
- 新しい並列リストを別に足さない。`RunArtifacts` のフィールドは増やさない。

### 2. ブロック鍵と block bootstrap

`ablation_compare.py` に以下を足す。

- `broker_day(label: datetime) -> date`: `label.astimezone(UTC).date()`。docstring か短いコメントで「`entry_at` は broker ラベル軸なので、その暦日がそのまま broker 営業日（NY 17:00 クローズ = broker 深夜）になる」という理由を書く。
- `block_bootstrap_interval(pnls: Sequence[float], blocks: Sequence[date], seed: int) -> tuple[float, float]`
  - `pnls` と `blocks` は同じ長さ（`zip(..., strict=True)`）。日ごとに損益をまとめ、**日付順**に並べた D 個のブロックを作る（順序を決めておくのは、CSV の行順に依らず同じ seed で同じ区間を再現するため）。
  - D < 2 なら `(nan, nan)`（`policy_event_study.bootstrap_interval` の `len(values) < 2` と同じ扱い。約定数が多くても日が 1 つなら判定不能）。
  - 1 replicate = D 個のブロックを復元抽出し、選ばれた日の約定を**全部つなげた平均**（ブロックの大きさに比例した重みになる）。`BOOTSTRAP_SAMPLES` 回、`random.Random(seed)`、パーセンタイル区間の取り方（`tail = (1 - BOOTSTRAP_LEVEL) / 2`、`int(tail * (n - 1))` / `int((1 - tail) * (n - 1))`）は `bootstrap_interval` / `difference_interval` と同じ。
- `block_difference_interval(with_pnls, with_blocks, without_pnls, without_blocks, seed) -> tuple[float, float]`
  - 既存 `difference_interval` と同じ構造。腕ごとに独立に日を復元抽出し、with の平均 − without の平均を 1 replicate とする。どちらかの腕のブロック数が 2 未満なら `(nan, nan)`。
- 日ごとのグループ化は両関数で共有する小さな内部関数にしてよい（1 か所なら不要）。
- `policy_event_study.bootstrap_interval` は**変更しない**（イベントスタディと既存の i.i.d. 区間の正本）。

### 3. `ArmSummary` / `arm_summary`

- `ArmSummary` に `block_low: float`、`block_high: float`、`blocks: int` を足す（既定値なし）。`blocks` は日ブロックの数。
- `arm_summary` にブロック鍵の引数を足す（例: `arm_summary(pnls, blocks, max_drawdown, seed)`）。既存の `low` / `high` は**今までと同じ呼び出し**（`bootstrap_interval([float(p) for p in pnls], seed)`）で求め、値を変えない。block 区間は `block_bootstrap_interval` で同じ `seed` から求める。
- `judge` は**変更しない**。新フィールドを読まない。

### 4. `report` の表示

既存の行（`trades` / `net_pnl_total` / `expectancy(mean)` / `hit_rate` / `max_drawdown` / `carry_total` / `unpriced_rollovers` / `mean CI90 [low, high]` / `difference of means ...` / `verdict:`）の**名前・書式・値は一切変えない**。既存の研究ノートがこれらを参照している。

追加する行:

- 表に `blocks`（腕ごとの日ブロック数）と `expectancy_ci90_block`（`[low, high]`、`mean CI90 [low, high]` 行と同じ `_format_float` 書式）の 2 行を、`mean CI90 [low, high]` 行の直後に足す。
- `difference of means ...` 行の直後に block 版の差の行を 1 行足す（例: `difference of means (with - without) block CI90 [low, high] seed=42`）。`verdict:` 行は最後のまま。
- `report` 内でブロック鍵を作るのは `[broker_day(at) for at in run.entry_ats]`（`entry_ats` が 1. で entry 単位になっているので `pnls` と揃う）。

### 5. モジュール docstring

`ablation_compare.py` 冒頭の docstring に 1〜2 文足す。内容: block 区間（broker 営業日を単位に日ごと復元抽出）は参考値として並記する。判定（`judge`）は事前登録した i.i.d. 区間で行い、block 区間を判定規則に採用するには事前登録の更新が要る。言語は周囲の docstring に合わせてよい。

### CLI

`--seed` は既存のまま両方の bootstrap に使う。CLI 引数は足さない。

---

## テスト（`tests/unit/test_ablation_compare.py` に追加・更新）

既存テストは、`ArmSummary` の新フィールドを埋めるための `arm()` ヘルパーの更新と、`RunArtifacts` / `arm_summary` の呼び出しを新しい形に合わせる機械的な変更以外は変えない。`judge` のパラメータ化ケース（`test_judge_applies_the_pre_registered_rules`）は**ケースを変えずに通す**（新フィールドが判定に影響しないことの確認）。

追加するもの:

- (a) **同じ瞬間の表記違いが同じブロックになる**: `+09:00` 表記と `+00:00` 表記の同じ aware datetime が同じ `broker_day` を返す（本モジュールの期間比較と同じ aware 比較の規約に揃える）。あわせて、broker 深夜（ラベル `00:00+00:00`）の直前と直後が別ブロックになる。
- (b) **ブロック境界が broker 営業日である**（engine の変換との整合）: 実 UTC の瞬間 `known` を `known_to_broker_label(known, timedelta(hours=7))` でラベルにし、`broker_day(label) == label.date()` かつ、UTC 暦日が変わる例（冬時間の 22:30 UTC など）では `broker_day(label) != known.astimezone(UTC).date()` を固定する。`broker_label_to_known(label, anchor) == known` の往復も 1 行で確かめる（DST 遷移帯は使わない）。`known_to_broker_label` は `trading.data.market.dukascopy`、`broker_label_to_known` は `trading.data.market.clock`（`trading.backtest.research` からも import 可）。
- (c) **日ごとの偏りで block 区間が i.i.d. 区間より広い**: 2 ブロック以上（例: 4 日）で日ごとの平均が大きく違う人工データ（同じ日は同じ符号の損益に固める）を作り、`block_bootstrap_interval` の `[low, high]` が `bootstrap_interval` の区間を外側から包む（`block_low < iid_low` かつ `block_high > iid_high`）。seed 固定で決定的であること（同 seed で同値、別 seed で別値）も 1 行で確認する。
- (d) **ブロックが 1 個だけなら `(nan, nan)`**: 約定が多数でも同じ日なら `(nan, nan)`。`block_difference_interval` も、片腕のブロック数が 1 なら `(nan, nan)`。
- (e) 既存の `judge` テストが無変更で通ること（上記）。
- `report` の往復テスト（`test_load_run_and_report_round_trip_trade_pnls_and_provenance`）に、`blocks` 行（with=2、without=3）と `expectancy_ci90_block` 行、block 版の差の行が出ることの assert を足す。既存の assert は残す。
- `test_partial_closes_are_one_bootstrap_sample`: `run.entry_ats` が entry 単位（`[trades[0].entry_at, trades[2].entry_at]`）になる assert に更新し、部分決済のある entry がブロックに 1 回だけ数えられることを確認する。

### 触らないもの

- `tests/unit/test_invariants.py` は触らない。
- `tests/unit/test_policy_event_study.py` ほか他モジュールのテストは触らない。

---

## やらないこと

- `policy_event_study.bootstrap_interval` とイベントスタディ（`policy_event_study` / `intervention_event_study` / `rate_differential_study` / `shock_trigger_study`）の変更
- 判定規則（`judge` / `MIN_TRADES` / `COMPARABLE_FIELDS` / `TRAILING_BLACKOUT_MAX_RATIO`）の変更
- 既存研究ノート（`docs/research/*.md`）の再計算・書き換え。VPS 上の run 成果物への参照
- CLI 引数の追加、ブロック単位の切り替えオプション
- `engine.py` / `report.py` / `simulator.py` の変更（`entry_at` の軸は変えない）
- 周辺リファクタ・無関係な整形

---

## 実装上の規約

- 金額・数量は `Decimal`。bootstrap の内部計算は既存と同じく float 可（`bootstrap_interval` / `difference_interval` と同じ流儀）。
- frozen dataclass を壊さない。引数や共有オブジェクトを破壊しない。
- 検証はシステム境界（run 成果物 = `manifest.json` / `summary.json` / `trades.csv`）のみ。内部関数間に防御的分岐・フォールバック・既定値の shim を足さない。
- WHAT を説明するコメント、コミット文脈に依存するコメント（「issue #164 のために追加」等）を書かない。理由（WHY）が要る箇所だけ短く書く。
- 日本語コメント優先。既存の英語 docstring に追記する場合は周囲に合わせてよい。

## 完了条件

worktree 内で次がすべて通ること。

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` 無指摘
- 上記 3 ディレクトリの pytest が green（`tests/broker` は MT5 なしで自動 skip、`tests/integration` は PostgreSQL 必須のためここでは対象外）

## 影響範囲の確認

```
rg -n "arm_summary|ArmSummary|RunArtifacts|entry_ats" src tests docs
rg -n "bootstrap_interval|difference_interval" src tests
```

`docs/research/` のヒットは参照確認のみ（ノートは編集しない）。

## コミットしないもの

- `tmp/`（Codex のログ・セッション ID）
- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md`

このファイル（`tasks/issue-164.md`）はコミットに含める。
