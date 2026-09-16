# issue #168: tick collector の穴埋めの終端を +1 秒にし、polled より新しい行は捨てる

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-168-gap-fill-end-bound`
- ブランチ: `fix/issue-168-gap-fill-end-bound`（base は `origin/main` = `6b6281d`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #168 本文（全文転記）

> タイトル: tick collector の穴埋めが終端の秒と同一ミリ秒の quote を取れていない（copy_ticks_range は秒単位の半開区間）
>
> ### 背景
>
> issue #136 の 3 番目の確認を 2026-09-16 に Windows 実機（OANDA-Japan MT5 Demo、MT5 build 6140、MetaTrader5 Python パッケージ）で実測した結果、PR #132 の穴埋めが前提にしていた範囲の意味が実機と違っていた。
>
> ### 実測
>
> tz 付き UTC の datetime を渡して `copy_ticks_range(SYMBOL, t, t, COPY_TICKS_ALL)` と `copy_ticks_range(SYMBOL, t, t + 1s, COPY_TICKS_ALL)` を呼んだ。
>
> - `t` の秒に tick が 2 本、`t + 1s` の秒に 1 本ある標本で、`range(t, t)` は **0 行**、`range(t, t + 1s)` は **`t` の 2 本だけ**（`t + 1s` の 1 本を含まない）
> - つまり範囲は**秒単位の半開区間 `[from, to)`**。ミリ秒は切り捨てられ、`from == to` なら空
> - 補足: naive な datetime を渡すとローカル時刻（VPS は JST）として解釈されて 9 時間ずれる。collector は tz 付き UTC で渡しているので本番には影響しない
>
> ```
> t=2026-09-16T17:06:13+00:00 sample_at_t=2 sample_at_t+1s=1
> range(t,t)      -> 0 rows, seconds=[]
> range(t,t+1s)   -> 2 rows, seconds=['17:06:13']
> ```
>
> ### 影響
>
> `TickCollector.poll_once`（`src/trading/data/market/collector.py`）は `copy_ticks_range(symbol, previous[0], tick.time, COPY_TICKS_ALL)` で前回 quote から今回 quote までの穴を読む。終端が含まれないため、
>
> - polled tick と同じ秒にある quote（同じミリ秒の別値を含む）は取れない
> - `previous[0]` と `tick.time` が同じ秒なら常に空が返る
>
> PR #132 が狙った「同一 broker ミリ秒内で価格だけ変わったときの穴埋め」は実機では働いていない。`tests/unit/test_tick_collector.py` の fake `copy_ticks_range` は `date_from == date_to` で時刻が一致する行を返し、それ以外はミリ秒精度の `[from, to)` を返すため、この差を検出できなかった。
>
> ### 対応方針
>
> - `poll_once` の終端を `tick.time + 1 秒` にし、返った行のうち `time > tick.time` の行は捨てる（次のポーリングの範囲で拾われる。polled quote が最後に来る現在の並び規則を保つ）
> - fake を実機に合わせる: 秒に切り捨てた `[from, to)`、`from == to` は空
> - テスト: polled tick と同じ秒（同じミリ秒を含む）の別 quote が回収されること。`previous` と polled が同じ秒でも回収されること。`time > tick.time` の行を捨てること。既存の「polled quote が最後に来る」テストは無変更で通ること

---

## ユーザー判断で確定していること（変更しない）

- **終端は `tick.time + 1 秒`**。返った行のうち `time > tick.time` の行は捨てる（次のポーリングの範囲 `[floor(tick.time), …)` で拾われる）。polled quote が同時刻の行の最後に来る現在の並び規則（`last_polled_index`）はそのまま保つ。
- **下限側は変えない。** 実機では今も `floor(previous[0])` から返っており、`[floor(previous[0]), previous[0])` にある行は前回のポーリング（または前回の穴埋め）で書き込み済みで、`market_ticks` の一意キーが弾く。`poll_once` の戻り値は「実際に追加した行数」なので影響しない。下限の trim を新たに足さない。
- **`backfill` / `_windows` は触らない**（根拠は下記）。

## 前提として確認済みの事実（実装で使う）

- 実機の `copy_ticks_range(sym, from, to)` は `floor_sec(from) <= t < floor_sec(to)` の行を返す（`t` はミリ秒精度）。`floor_sec(from) == floor_sec(to)` なら空。
- `market_ticks` の一意キーは `(symbol, event_time, bid, ask)`（`migrations/0002_market_data.sql:21`）。`PostgresMarketTickRepository.insert_many` は `ON CONFLICT (symbol, event_time, bid, ask) DO NOTHING`（`src/trading/storage/postgres.py:450`）で、戻り値は実際に追加した行数。同じ行を二度渡しても件数は増えない。
- `_windows(start, end)`（`src/trading/data/market/collector.py:109`）は `[start, min(start+1d, end))` を `end` まで連続して切る。実機は各窓を `[floor(ws), floor(we))` で返すので、窓の内側境界（`we_k == ws_{k+1}`）は `floor` しても一致し、窓と窓の間に落ちる秒はない。最後の窓の終端は `end` そのもので、`backfill` の契約は元から `[start, end)`（終端排他。`test_long_backfill_is_split_into_windows` のコメントも境界の重複を前提にしている）。`--backfill-to` に秒未満の端数を付けたときだけ `[floor(end), end)` の端数分が対象外になるが、これは実機の秒精度の契約どおりで「秒が落ちる」わけではない。**よって `_windows` / `backfill` は変更不要。**
- `tests/unit/test_tick_collector.py` の `FakeTickRepository.insert_many` は重複を弾かない（渡された行をそのまま数える）。テストの fixture で `previous[0]` より前の同秒の行を置くと、そのぶん件数が増えて見える。新しいテストではそのような行を置かない。

---

## 変更範囲

対象ファイルは `src/trading/data/market/collector.py` と `tests/unit/test_tick_collector.py` の 2 つ。ほかは触らない。

### 1. `poll_once`（`src/trading/data/market/collector.py:162` 付近）

- `copy_ticks_range` の終端を `tick.time + timedelta(seconds=1)` にする。ローカル変数（例: `history_end`）に持ち、失敗時の `MT5ConnectionError` メッセージの範囲表示もその値にする。
- `tick_from_row` で作った行のうち `history_tick.time > tick.time` の行は `history_ticks` に入れない。既存の選別（`last_polled_index` / `endpoint_quotes` / 並び順）は**そのまま**、選別に渡す前に落とすだけにする。
- 落とす箇所には短いコメントで理由を書く（terminal の範囲は秒単位の半開区間なので終端の秒まで読む。polled より新しい行は次のポーリングが自分の範囲で拾う。落とさないと polled quote が最後に来る規則が崩れる）。
- docstring の「history supplies both updates between two broker times and other prices carrying the same millisecond stamp」の後に、終端の秒まで読む理由（terminal は範囲を秒単位の半開区間 `[from, to)` で解釈するため、終端を 1 秒先にして polled より新しい行は次のポーリングに残す）を 1 文足す。言語は周囲（英語）に合わせる。
- `POLL_GAP_MAX`・ポーリング間隔・`backfill`・`_windows`・`_chunks`・`main` は触らない。

### 2. fake `copy_ticks_range`（`tests/unit/test_tick_collector.py:72` 付近）

- `date_from == date_to` の特別扱いを消す。
- 両端を秒に切り捨て（`replace(microsecond=0)`）、`floor(date_from) <= _row_time(row) < floor(date_to)` の行を返す。行の時刻（`_row_time`）はミリ秒のまま。切り捨て後に両端が同じなら自然に空になる。
- fake の docstring に実機の挙動を書く: 両端を秒単位で読む半開区間 `[from, to)`、`from == to` は空（OANDA MT5 Demo build 6140 で実測、issue #168）。
- `range_calls` への記録・`on_range_call`・`None` の扱いは変えない。

### 3. テスト（同ファイル）

既存テストの変更は次の 1 か所だけ:

- `test_changed_quote_at_the_same_broker_time_fills_same_millisecond_history` の `assert mt5.range_calls[0][1:3] == (T0, T0)` を `(T0, T0 + timedelta(seconds=1))` にする（終端の変更そのもの）。それ以外の assert と fixture は変えない。

追加するテスト（`T0` は秒ちょうど。`T0_MSC + n` はミリ秒）:

- (a) **polled と同じミリ秒（別の秒）の別 quote が回収される**
  - `info_ticks`: `(T0_MSC, 158.840/158.844)`, `(T0_MSC + 1300, 158.850/158.854)`
  - `range_rows`: `(T0_MSC, 158.840/158.844)`, `(T0_MSC + 1300, 159.500/159.504)`, `(T0_MSC + 1300, 158.850/158.854)`
  - 期待: 1 回目 `poll_once == 1`、2 回目 `== 2`。`mt5.range_calls[0][1:3] == (T0, T0 + timedelta(milliseconds=1300) + timedelta(seconds=1))`。stored の bid は `[158.840, 159.500, 158.850]`（polled が同時刻の最後）。
  - 修正前の fake では終端が `T0 + 1.3s` → floor で `[T0, T0+1s)` になり `159.500` が落ちる。
- (b) **polled と同じ秒で数百ミリ秒前の quote が回収される**
  - `info_ticks`: `(T0_MSC, 158.840/158.844)`, `(T0_MSC + 1800, 158.850/158.854)`
  - `range_rows`: `(T0_MSC, 158.840/158.844)`, `(T0_MSC + 1200, 159.500/159.504)`, `(T0_MSC + 1800, 158.850/158.854)`
  - 期待: 2 回目 `poll_once == 2`。bid は `[158.840, 159.500, 158.850]`、`time` は昇順。
- (c) **`previous` と polled が同じ秒でも回収される**
  - `info_ticks`: `(T0_MSC + 100, 158.840/158.844)`, `(T0_MSC + 700, 158.850/158.854)`
  - `range_rows`: `(T0_MSC + 100, 158.840/158.844)`, `(T0_MSC + 400, 159.500/159.504)`, `(T0_MSC + 700, 158.850/158.854)`
  - 期待: 2 回目 `poll_once == 2`。bid は `[158.840, 159.500, 158.850]`。
  - 修正前は `range(T0+0.1s, T0+0.7s)` が floor で `[T0, T0)` = 空になり、`159.500` が落ちて `poll_once == 1` になる。
- (d) **polled より新しい行は書かず、次のポーリングが拾う**
  - `info_ticks`: `(T0_MSC, 158.840/158.844)`, `(T0_MSC + 1200, 158.850/158.854)`, `(T0_MSC + 1500, 158.860/158.864)`
  - `range_rows`: 同じ 3 本
  - 期待: 2 回目 `poll_once == 1` で stored の bid は `[158.840, 158.850]`（`T0+1.5s` の行は範囲 `[T0, T0+2s)` に入っているが書かれない）。3 回目 `poll_once == 1` で `[158.840, 158.850, 158.860]`、`len == 3`（重複なし）。`mt5.range_calls[1][1:3] == (T0 + 1.2s, T0 + 1.5s + 1s)`。
- (e) 既存テスト（`test_same_time_history_keeps_the_polled_quote_before_newer_quotes`、`test_last_matching_quote_stays_last_at_same_broker_time_for_bar_close`、`test_quotes_missed_between_two_polls_are_filled_from_the_tick_history`、`test_a_failed_tick_history_fetch_is_not_read_as_an_empty_burst`、backfill 系）は無変更で通ること。

テスト名は既存の命名（`test_<何が>_<どうなる>`、文で読める英語）に合わせる。各テストの先頭コメントは、なぜその挙動が要るか（実機の秒単位半開区間）を 1〜3 行で書く。

### 修正前に落ちることの確認（手順）

`git stash` は使わない。順番で確認する:

1. まず 2. の fake 変更と 3. のテスト追加・更新だけを入れて `.venv/bin/pytest tests/unit/test_tick_collector.py` を実行し、(a)(b)(c) と、既存の `test_changed_quote_at_the_same_broker_time_fills_same_millisecond_history`（`previous == polled` の時刻で範囲が空になる）が落ちることを確認する。落ちたテスト名と要旨を控える。(d) はこの時点で通っても落ちてもよい（修正前の範囲は `T0+1.5s` を含まない）。
2. 次に 1. の `poll_once` 修正を入れ、同じコマンドで全件通ることを確認する。

---

## 検証

- `.venv/bin/ruff check .`
- `.venv/bin/pytest tests/unit/test_tick_collector.py`
- `.venv/bin/pytest tests/unit tests/replay tests/failure`

## Codex に返してもらうもの

- 変更ファイルの一覧（未コミットのまま。commit / push / PR は Claude が行う）
- 手順 1 の「修正前に落ちた」pytest 出力の要約（テスト名と失敗理由）
- 手順 2 以降の ruff / pytest の結果
- 未確認の項目・計画と違えた点（あれば理由つき）

## やらないこと

- ポーリング間隔・`POLL_GAP_MAX` の変更
- `backfill` / `_windows` / `_chunks` / `main` の変更
- `bar_service` / `BarBuilder` / `storage` の変更
- 下限側（`previous[0]` より前の同秒の行）の trim 追加
- 周辺リファクタ・無関係な整形・コメントの書き直し
- `tests/unit/test_invariants.py` ほか他ファイルのテスト変更
