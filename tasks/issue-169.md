# issue #169: `swap_rollover3days` が範囲外のとき 3 日分 rollover の曜日を設定で補う

ブランチ: `fix/issue-169-swap-triple-weekday`
worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-169-swap-triple-weekday`

## 先に読むもの

- `AGENTS.md`（正本。特に「取引システム固有の不変条件」「テストルール」「AIレビュー指示」）
- `.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- `docs/SYSTEM_SPEC.md` §5.11（581〜624 行。swap/rollover の現行規範）
- `docs/adr/ADR-016-swap-rollover-pit-broker-cost.md`（旧決定の履歴。「水曜=triple をハードコードしない」の由来）
- `src/trading/domain/swap.py`、`tests/unit/test_swap_snapshot.py`、`tests/replay/test_swap_carry.py`

## 背景と課題

VPS 実測（2026-09-16、OANDA-Japan MT5 Demo、build 6140）で、`symbol_info().swap_rollover3days` が
USDJPY / EURUSD / GBPUSD / GBPJPY の 4 銘柄とも **7** を返し、per-day 倍率（`swap_sunday` 〜 `swap_saturday`）は
公開されなかった（NULL）。MQL5 の `ENUM_DAY_OF_WEEK` は 0（日曜）〜 6（土曜）で、7 は定義外。
4 銘柄とも `swap_mode=1`（POINTS）。

現在の `SwapSnapshot.rollover_multiplier` は per-day 倍率が無いとき `swap_rollover3days` と一致する曜日を 3 倍、
週末を 0、他を 1 にする。7 はどの曜日にも一致しないため 3 日分の rollover が一度も課されず、
水曜の 2 日分を取りこぼす。範囲外の値を黙って「3 倍日なし」と読んでいるのが問題。

OANDA Japan の FAQ では「土日分は水曜のロールオーバーで前倒し付与（水曜は 3 日分）」「祝日・銀行休業日で変則あり、
スワップカレンダーで確認」。基本は水曜（`ENUM_DAY_OF_WEEK` = 3）に 3 日分。

## 方針

1. `rollover_multiplier` は `swap_rollover3days` が 0〜6 の外なら「未指定」として扱い、黙って 1 倍にしない
2. 未指定のときの 3 日分曜日は設定（`InstrumentPolicy.swap_triple_weekday`）から取る。設定も無ければ fail-loud
   （`swap_mode` 非対応時の `UnsupportedSwapModeError` と同じ設計）
3. per-day 倍率が最優先である現在の規則は変えない
4. 祝日による変則（カレンダー）は範囲外

## 変更範囲

### 1. `src/trading/domain/swap.py`

- `UnsupportedSwapModeError` の隣に、未指定の 3 倍曜日を表す例外クラスを 1 つ足す
  （命名は既存に合わせる。例: `UnknownTripleSwapWeekdayError(ValueError)`）。
  docstring は「黙って 1 倍にせず落とす」趣旨を 1 文
- `rollover_multiplier(self, day: date, *, triple_weekday: int | None = None) -> Decimal` に引数を足す。判定順:
  1. per-day 倍率（`swap_sunday` 〜 `swap_saturday`）が `None` でなければそれを返す（現状どおり最優先）
  2. 週末（`_MQL_SUNDAY` / `_MQL_SATURDAY`）は `Decimal(0)`
  3. 3 倍曜日を決める: `swap_rollover3days` が 0〜6 ならそれ、外なら引数の `triple_weekday`
  4. 3 倍曜日が決まらなければ（引数も `None`）新しい例外を送出する。
     メッセージに symbol と実際に返ってきた `swap_rollover3days` の値を含める
  5. 決まった曜日と一致すれば `Decimal(3)`、他は `Decimal(1)`
- `carry_amount(..., triple_weekday: int | None = None)` を足し、`rollover_multiplier` へ渡す
- **`carry_amount` の判定順を入れ替える**: 現在は `multiplier = snapshot.rollover_multiplier(day)` を先に計算してから
  `multiplier == 0 or swap_mode == SWAP_MODE_DISABLED` を見ている。このままだと swap 無効の銘柄でも新しい例外が飛ぶ。
  `SWAP_MODE_DISABLED` の早期 return を multiplier の計算より前に出す（挙動は同じで、例外の巻き添えだけを避ける）
- docstring に、ブローカーが `ENUM_DAY_OF_WEEK` の範囲外の値を返す場合があること（実測）と、
  そのとき設定の曜日を使うことを書く。issue 番号やコミット文脈に依存する記述は書かない。
  モジュール docstring の「ハードコードしない」方針との関係（範囲外のときだけ設定で補う）が読めるようにする

### 2. `src/trading/config.py` の `InstrumentPolicy`

- `swap_triple_weekday: int | None = Field(default=None, ge=0, le=6)` を足す（設定境界の検証。
  `ENUM_DAY_OF_WEEK` は 0=日曜 〜 6=土曜）。`Field` の import が無ければ足す
- クラス docstring に 1 文足す: broker が範囲外の値を返す銘柄で使う 3 日分 rollover の曜日

### 3. `config/base.yaml`

- `instruments` の 4 銘柄（USDJPY / EURUSD / GBPUSD / GBPJPY）に `swap_triple_weekday: 3` を足す
- WHY コメントを 2 行程度: OANDA の terminal は `swap_rollover3days=7`（`ENUM_DAY_OF_WEEK` の範囲外）を返し
  per-day 倍率も公開しないため、水曜 3 日分を設定で与える。祝日による変則は未対応
- 他の env overlay（`shadow.yaml` / `micro_live.yaml` / `production.yaml` / `backtest.yaml` / `demo.yaml`）は触らない

### 4. `src/trading/backtest/engine.py`

- `BacktestEngine.__init__` に `swap_triple_weekday: int | None = None` を足して保持する
  （`broker_server_ahead_of_ny_hours` の隣が自然）
- `_charge_carry`（573 行前後）の `carry_amount(...)` 呼び出しへ `triple_weekday=` として渡す。
  `carry_amount(` の呼び出し元は src ではここだけ（`rg -n "carry_amount\(" src` で確認済み）

### 5. `src/trading/backtest/research.py`

- 493 行前後の `BacktestEngine(` 組み立てで `swap_triple_weekday=config.instruments[symbol].swap_triple_weekday` を渡す。
  `config.instruments` に `symbol` が無い経路があるなら、その場合は `None` のままにする（`.get` 等で分岐）
- `src/trading/backtest/run.py` の `BacktestEngine(` は swap snapshot を渡さない合成データの経路で
  `carry_amount` に到達しないため変更しない

### 6. ADR

- `docs/adr/ADR-039-<kebab-case>.md` を 1 本足す（次番号は 039。`ls docs/adr | sort | tail -3` で確認済み）。
  書式は `docs/adr/ADR-038-strategy-take-profit.md` に合わせる（`# ADR-039: …` / `**Status:** Accepted (2026-09-17)` /
  `## Context` / `## Decision` / `## Consequences`）
- 内容: SYSTEM_SPEC §5.11（ADR-016 由来）が「broker 返却値を truth source にし、水曜=triple をハードコードしない」と
  決めていることに対する例外規則の明文化。broker が `ENUM_DAY_OF_WEEK` の範囲外を返した場合に限り
  `InstrumentPolicy.swap_triple_weekday` を使い、設定が無ければ落とす。per-day 倍率が最優先である点は変わらない。
  祝日カレンダー（スワップカレンダー）は対象外で、その取りこぼしは未対応として残る。
  実測値（4 銘柄とも `swap_mode=1` / `swap_rollover3days=7` / per-day は NULL、2026-09-16、build 6140）を Context に書く
- 既存 ADR と `docs/SYSTEM_SPEC.md` の本文は書き換えない（追加のみ）

### 7. テスト

`tests/unit/test_swap_snapshot.py`（既存の `snapshot()` ファクトリと `THURSDAY` / `WEDNESDAY` / `SATURDAY` / `SUNDAY` 定数を使う）:

- `swap_rollover3days=7` で `triple_weekday=3` を渡すと水曜が 3 倍、木曜が 1 倍、週末が 0
- `swap_rollover3days=7` で `triple_weekday` 無しは新しい例外で落ちる（メッセージに symbol と `7` が含まれる）
- `swap_rollover3days` が 0〜6 のときは `triple_weekday` を渡しても broker 値が優先される（従来どおり）
- per-day 倍率がある行は `swap_rollover3days=7` でも per-day が優先され、例外も出ない
- `swap_mode=SWAP_MODE_DISABLED` かつ `swap_rollover3days=7` の `carry_amount` は 0 を返す（例外を出さない）
- 既存のテストは無変更で通す

`tests/unit/test_config.py`（`load_config` と `CONFIG_DIR`、`tmp_path` に YAML を書く既存パターンを使う）:

- `swap_triple_weekday` が 0〜6 の外だと設定読み込み（または `InstrumentPolicy` 直接生成）で `ValidationError`
- `config/base.yaml` の 4 銘柄が `swap_triple_weekday == 3`

`tests/replay/test_swap_carry.py`（既存の `_snapshot` / `_run` / `_times_across` を使う）に 1 本だけ足す:

- `swap_rollover3days=7` の snapshot と `swap_triple_weekday=3` を渡した run で、水曜の boundary
  （2026-08-12 21:00Z）を跨ぐ建玉に 3 倍の carry が計上されること。既存の `_snapshot` は `swap_rollover3days=3`
  固定なので、引数で上書きできるようにするか専用の snapshot を組む。既存 carry テストは無変更で通す

## やらないこと

- 祝日カレンダー、スワップカレンダーの取り込み
- `swap_mode` の他モード対応
- collector（`src/trading/data/swap/collector.py`）の変更（生の broker 値をそのまま保存する現在の挙動は正しい）
- `migrations/` の変更
- 既存 ADR・SYSTEM_SPEC の本文改訂
- 研究ノートの更新、VPS 成果物への参照
- 周辺リファクタ・無関係な整形

## 完了条件と検証

- 上記 1〜7 が実装されている
- `.venv/bin/ruff check .` がクリーン
- `.venv/bin/pytest tests/unit tests/replay tests/failure` が全て通る（broker は自動 skip、integration は対象外）
- 変更ファイル一覧、実行した検証コマンドと結果、未確認項目を返す
- **commit / push / PR 作成はしない**（Claude が担当）

## 未確定事項

なし。方針はユーザー判断で確定済み（範囲外の値は設定の曜日で補い、設定が無ければ落とす。祝日カレンダーは範囲外）。
