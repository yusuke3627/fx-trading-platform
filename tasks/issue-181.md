# issue #181: 既存 2 戦略の warmup に DEFAULT_BAR_COUNT の下限を入れる

## 目的

`IndicatorService` は各時間足を `max(DEFAULT_BAR_COUNT, period + 1)` 本（既定 200 本）読み、その全履歴で
Wilder ATR / EMA を更新する。`failed_spike_reversal` と `post_event_failed_breakout` の `warmup` には
この 200 本が入っていないため、宣言 warmup は 200 本ぶんの頭出しを保証しない。不足するかどうかは
`--from` の曜日次第で、`market_span_to_calendar` が足す 2 日が丸ごと閉場に落ちる（＝週明けに近い `--from`）と
開場時間の頭出しが `span * 7/5` だけになり、期間の冒頭を定常状態と違う ATR で評価する。
`bar_window` には既に `max(DEFAULT_BAR_COUNT, ...)` が入っているのに `warmup` には無い、という非対称を解消する。

修正の前例は PR #179 のコミット `1dd85fb`（`src/trading/strategy/intraday/range_edge_reversal.py` の
`warmup`）。同じ形にそろえる。

## 対象と、時間足ごとに下限が要るかどうか

下限が要るのは **IndicatorService 経由で読む時間足だけ**。`ctx.market.bars(...)` で本数を明示して読む
構造指標（`rolling_high` / `min`）は再帰平滑ではなく、要求本数より多く読んでも値が変わらない。

| 戦略 | IndicatorService 経由の読み | 直接読み | 下限を入れる対象 |
| --- | --- | --- | --- |
| `failed_spike_reversal` | `ctx.indicators.atr(symbol, entry_tf, atr_period)`（`scalp/failed_spike_reversal.py:104`） | tick のみ | entry |
| `post_event_failed_breakout` | `ctx.indicators.atr(symbol, entry_tf, atr_period)`（`intraday/post_event_failed_breakout.py:118`） | `ctx.market.bars(symbol, setup_tf, lookback + 5)`（同 123 行）＝ `rolling_high` / `min` | entry のみ |

### issue の記述からの逸脱（重要）

issue #181 の表は `post_event_failed_breakout` の不足を「5m を 185 本、15m を 175 本（約 44 時間）」と
書いているが、**15m（setup）側に不足は無い**。setup 足は `lookback + 5 = 25` 本を明示して読み、
`rolling_high(setup_bars[:-1], lookback)` と `min(...)` はどちらも読んだ本数に依存しない。
現行 warmup の `(lookback + 5) * setup_tf` は既にこの 25 本を満たしている。

したがって本 PR は setup 足に下限を入れない。研究ノートにも 15m を不足として書かない。

## 変更内容

### 1. `src/trading/strategy/scalp/failed_spike_reversal.py` の `warmup`

```python
span = (
    max(atr_period + 1, DEFAULT_BAR_COUNT) * TIMEFRAME_SECONDS[entry_tf]
    + window_seconds * 3
)
```

ATR が読む本数に下限を掛け、spike 検出の tick 窓（`window_seconds * 3`）はその上に載せる。
既存コメントを、下限を入れた理由が読めるように直す（ファイル内の既存コメントの言語に合わせる）。

### 2. `src/trading/strategy/intraday/post_event_failed_breakout.py` の `warmup`

```python
span = max(
    (lookback + 5) * TIMEFRAME_SECONDS[setup_tf],
    max(atr_period + 1, DEFAULT_BAR_COUNT) * TIMEFRAME_SECONDS[entry_tf],
)
```

setup 側は現状のまま（構造読みの実本数 `lookback + 5` で足りる）。entry 側の ATR にだけ下限を掛ける。

### 3. `strategy_version` は据え置く

`failed_spike_reversal` は `0.2.0`、`post_event_failed_breakout` は `0.2.0` のまま。理由は PR 本文に書く
（判断の要点は「決定関数は変わらない」「比較不能性は `git_commit` / `git_diff_sha256` / `warmup_days` が
既に担保している」）。副作用として「同じ `strategy_version` のまま warmup が変わるので、今後この 2 戦略を
再測定した結果は本 PR より前の run と厳密には同条件ではない」ことも PR 本文に書く。
**コードに「版を上げなかった理由」のコメントを足さない。**

### 4. テスト（`tests/unit/test_research_runner.py`）

既存の `test_warmup_follows_the_evaluated_configuration` の近くに、2 戦略それぞれ次の 2 ケースを含む
テストを足す。期待値は `market_span_to_calendar(...)` で厳密一致させる（`>` だけの弱い表明にしない）。

- **下限が効くケース**: 既定パラメータ。`atr_period + 1` が 200 を下回るので、entry 足の span が
  `DEFAULT_BAR_COUNT` 本ぶんになる。
- **下限を超えるケース**: `atr_period` を 200 より大きくした上書き（例 300）。entry 足の span が
  `atr_period + 1` 本ぶんになる。

`post_event_failed_breakout` は、setup 側が勝つ上書き（例 `resistance_lookback` を大きくする）も
1 ケース入れて、setup 項に下限が掛かっていないことを固定する。

既存の `test_warmup_follows_the_evaluated_configuration` は
`intraday.warmup(widened) > intraday.warmup(base_config)`（`resistance_lookback=500`）で、
変更後も 505 * 900 = 454,500 > 60,000 なので通る。緩めない。

### 5. 研究ノートへの限界追記（数値と判定は一切書き換えない）

H4 / H5 のノートは、どちらも末尾に `### 測定条件の限界` を立てて次の 2 点を書く。**15m を不足として
書かない。既存の数値・判定は書き換えない。**

1. 算出式に entry 足 200 本の下限が入っておらず、宣言 warmup が 200 本を保証していなかったこと。
2. ただし当該 run では実際には不足していないこと（下の「実際の run で不足していたか」を参照）。
   併せて、期間途中の tick 欠損はハーネスが検出しないため、lead-in 区間に穴が無かったことまでは
   確認していないと断る。
- `docs/research/2026-09-17-h6-range-edge-reversal-preregistration.md` の `## まだ測っていないこと` へ
  次の 1〜2 行を追加（H6 の他の記述・判定は書き換えない）:
  2026-08-08〜08-22 の smoke run（約定 5 件）で決済内訳は損切り 3・時間切れ 2 で、レンジ中央の利確は
  1 件も成立しなかった。smoke の期間は測定期間の内側にあるため、この観測を理由にパラメータ
  （`expected_horizon_seconds`、利確の RR 閾値、`entry_band_fraction`）は変更していない。

## warmup がどれだけ延びるか（既定パラメータ）

`market_span_to_calendar(s) = timedelta(seconds=s * 7 / 5) + timedelta(days=2)`

| 戦略 | 変更前の market span | 変更後の market span | 変更前 warmup | 変更後 warmup |
| --- | --- | --- | --- | --- |
| `failed_spike_reversal`（entry 1m） | 15×60 + 180 = 1,080 s | 200×60 + 180 = 12,180 s | 2 d 00:25:12 | 2 d 04:44:12 |
| `post_event_failed_breakout`（setup 15m / entry 5m） | max(25×900, 15×300) = 22,500 s | max(22,500, 200×300) = 60,000 s | 2 d 08:45:00 | 2 d 23:20:00 |

## 保存 tick の頭出しが足りるかの机上確認（VPS を触らずに行う）

`research.py` の `read_from = args.start - warmup`、`_ensure_head_covered` は
`open_market_seconds(read_from, first_tick) > EDGE_GAP_TOLERANCE_SECONDS (3600)` で `SystemExit`。
`open_market_seconds` は broker ラベルの土日を closed として数えない。

H4 / H5 が使った `--from 2024-08-01T00:00:00+00:00`（broker ラベル）での `read_from`:

| 戦略 | 変更前 read_from | 変更後 read_from |
| --- | --- | --- |
| `failed_spike_reversal` | 2024-07-29T23:34:48 | 2024-07-29T19:15:48 |
| `post_event_failed_breakout` | 2024-07-29T15:15:00 | 2024-07-29T00:40:00 |

どちらも月曜 2024-07-29 の内側にとどまり、直前の週末（07-27 / 07-28）へは入らない。週明けは
broker ラベルの月曜 00:00 なので、変更後の `read_from` は週明け以降にある。

さらに、同じ tick 集合に対して H6（`range_edge_reversal`、宣言 warmup 13 日 16 時間）が同じ
`--from` で事前登録されており、その `read_from` は 2024-07-18T08:00 と 11 日以上手前になる。
H6 が `ensure_period_covered` を通る tick 集合なら、本 PR の `read_from` は余裕で内側に入る。

この確認は机上のみ（VPS の DB は参照しない）。Mac 側の DB に USDJPY tick は無いため実測はしない。

## 実際の run で不足していたか（H4 / H5）

研究リプレイは warmup 区間の tick でも足を組み立てる（`backtest/engine.py` の
"Warm-up ticks build bars, indicators and features but leave no trace in the outputs"）。足の保持上限は
`max(BAR_CAPACITY, bar_window)` = 10,000 本で、`IndicatorService` が読むのは 200 本（`DEFAULT_BAR_COUNT`）。
lead-in の開場時間が 200 本ぶんあれば、初回評価時点で entry 足は 200 本以上組み立て済みになる。`--from 2024-08-01`（木曜）の lead-in は月曜〜水曜の平日だけで埋まるため、
**H4 / H5 の run では不足していない**。

| 戦略 | 宣言値 | read_from | lead-in の開場時間 | entry 足の本数 |
| --- | --- | --- | --- | --- |
| H4 `failed_spike_reversal` | 変更前 2 d 00:25:12 | 2024-07-29 23:34:48（月） | 48.4 h | 1m 2,905 本（≥ 200） |
| H5 `post_event_failed_breakout` | 変更前 2 d 08:45:00 | 2024-07-29 15:15:00（月） | 56.8 h | 5m 681 本（≥ 200） |

不足が出るのは `--from` が週明けに近い場合。`--from` を月曜 00:00 に置くと:

| 戦略 | 変更前の entry 足 | 変更後の entry 足 |
| --- | --- | --- |
| `failed_spike_reversal` | 1m 25 本 | 1m 284 本 |
| `post_event_failed_breakout` | 5m 105 本 | 5m 280 本 |

つまり修正の価値は「H4 / H5 の数値が誤っていた」ではなく「宣言 warmup が開始曜日に依存せず 200 本を
保証するようになる」ことにある。

## 検証

worktree の絶対パス: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-181-warmup-bar-count-floor`

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

## やらないこと

- 戦略ロジック（`_evaluate` 以降）の変更
- `IndicatorService` / `bar_window` / `tick_window_seconds` の変更
- H4 / H5 の数値・判定の書き換え
- `docs/SYSTEM_SPEC.md` の本文改訂（v2.0 で凍結）
- 周辺リファクタ・無関係な整形
- コミット / push / PR 作成（Claude が担当）
