# issue #109: 介入イベントスタディの報道アンカーをショック窓の成否から独立させる

## 1. 背景（issue #109 の要点）

`src/trading/backtest/intervention_event_study.py` の `build_outcomes`（`intervention_event_study.py:301-319`）は、
ショックアンカーが立ったエピソード（`covered_episodes`）だけを `news_anchor` に渡している。そのため、
ショック探索窓（`SHOCK_WINDOW` = 36 時間）が未完了・欠損を含むエピソードは、報道時刻（`known_at`）以降に
連続した足があって報道上界のリターンを正しく測れる場合でも、報道集計から丸ごと脱落する。

当初この結合を入れた目的（欠損期間の `known_at` が何か月も先の足を拾う誤アンカーの防止）は、
後から `news_anchor` 自身に入った `NEWS_MAX_LAG`（3 日、`intervention_event_study.py:62`）の判定
（`intervention_event_study.py:243-244`）で既に達成されている。

## 2. ユーザー判断（2026-09-06）

**報道アンカーはショック窓の成否から独立させる。** 報道アンカーは全エピソードから組み立て、
遠すぎる足の拒否は `news_anchor` の `NEWS_MAX_LAG` に任せる。
ショックアンカー側のロジック・数値は一切変えない。変わるのは報道集計の母集団だけ。

PR #105 の引き継ぎメモにある「報道上界アンカーはショック窓に tick があるエピソードだけに付ける」は本 PR で撤回する。

## 3. 変更内容

### 3.1 `build_outcomes`（`src/trading/backtest/intervention_event_study.py:301-319`）

- `covered_episodes` の絞り込みをやめ、引数 `episodes` 全体を `news_anchor` に渡す。
- `covered_episodes` 変数は削除する（他に参照なし。`rg -n covered src tests` で確認済み。
  `report` 側の `covered_dates` は 3.3 で削除する）。
- `shock_anchors` の呼び出し・`shock_outcomes` の組み立ては現状のまま。
- docstring「二種類のアンカーを検出し、計測可能なエピソードを組み立てる。」に、
  報道アンカーはショック窓の成否と独立で、遠すぎる足の拒否は `news_anchor` が担う旨を 1〜2 行で足す
  （なぜ独立なのかを書く。「〜のために変更」のような文脈依存の書き方はしない）。

変更後のイメージ:

```python
def build_outcomes(
    episodes: Sequence[Episode],
    series: dict[str, list[Bar]],
    server_ahead_of_ny: timedelta,
) -> dict[str, list[Outcome]]:
    """二種類のアンカーを検出し、計測可能なエピソードを組み立てる。

    報道アンカーはショック窓の成否と独立に全エピソードから探す。報道から遠すぎる
    足を採らない判定は news_anchor が持つので、ショック窓の欠損で絞る必要はない。
    """
    shocks = shock_anchors(series["5m"], episodes)
    shock_outcomes = [
        build_outcome(anchor, series) for anchor in shocks.values() if anchor is not None
    ]
    news = [news_anchor(series["5m"], episode, server_ahead_of_ny) for episode in episodes]
    news_outcomes = [
        build_outcome(anchor, series) for anchor in news if anchor is not None
    ]
    return {SHOCK: shock_outcomes, NEWS: news_outcomes}
```

### 3.2 `news_anchor` の docstring（`intervention_event_study.py:231-251`）

- ロジックは変更しない。docstring は現状（「報道からその close まで市場休場で説明できない時間が空くなら採らない」）で
  独立設計と整合しているので、原則そのまま。必要なら 1 行だけ補う程度に留める。

### 3.3 `_news_anchor_lines` と `report`（`intervention_event_study.py:408-441`, `555-604`）

- `_news_anchor_lines` の引数 `covered_dates: set[date]` と、
  `if episode.action_date not in covered_dates: ... "no shock anchor"` の分岐を削除する。
  報道側はショックの成否と無関係になったため、報道アンカーが無い理由は既存の
  `"no bar close to known_at"` 行だけになる。
- `report` 内の `covered_dates = set(_outcomes_by_date(outcomes_by_kind[SHOCK]))` と、
  `_news_anchor_lines` への `covered_dates` 引数の受け渡しを削除する。
- `_shock_anchor_lines` の `"no shock anchor"` 行はショック側なので変更しない。
- レポート先頭行 `"{n} intervention episodes ({shock_count} with quotes)"` はショック側の件数なので変更しない。
- 行の並び（`episodes` の順で 1 エピソード 1 行）は既存どおり。

### 3.4 モジュール docstring（`intervention_event_study.py:1-18`）

- 「ショック足は 36 時間の探索窓全体から事後選択し、窓が未完了または欠損を含むエピソードは測定しない。」は
  ショック側の記述なのでそのまま。
- 報道アンカーについては現状明示的な結合の記述が無いため、変更不要。
  もし「ショック窓に足があるエピソードだけ」に類する記述が残っていれば、独立設計へ書き換える。

## 4. テスト（`tests/unit/test_intervention_event_study.py` に追加。既存テストは緩めない）

既存ファクトリ: `m5(index, close, *, open, high, low)`（`T0 = 2026-05-04T00:00Z` から 5 分 × index、
`known_at = start + 5min`）、`d1(index, close, ...)`、`episode(action_date, *, known_at, cluster)`、
`ANCHOR = timedelta(hours=7)`（5 月は夏時間なので broker ラベル = UTC + 3h。
`test_news_anchor_requires_a_close_strictly_after_known_at` で `known_at=2026-05-03T21:05Z → label=T0+5min` を確認済み）。
`HOLE_MINIMUM` は `policy_event_study.py:73` の 5 日（= 5 分足 1440 本分）。

### 4.1 ショック窓前半が欠損でもショック側が None なら報道アンカーは立つ（新規）

`test_build_outcomes_keeps_the_news_anchor_when_the_shock_window_has_a_hole` のような名前で:

- 足: `m5(-1440, "150")` の後、`m5(300)`〜`m5(499)` を連続で用意する（`[m5(-1440, "150"), *[m5(i, "150") for i in range(300, 500)]]`）。
  - `m5(499).start = T0 + 41h35m` は `window_end = T0 + 36h` より後なので、探索窓は閉じている
    （`right == len(bars)` の「窓が未完了」分岐には入らない。`m5(400)` までしか置かないとこちらの分岐で `None` に
    なってしまい、欠損を理由にした拒否を検証できない）。
  - `m5(-1440)` と `m5(300)` の間は 1740 本 = 6 日超 ≥ `HOLE_MINIMUM` なので `gaps` が穴と判定し、
    `before.close_time < window_end` かつ `after.start > day_start` を満たすので `shock_anchors` は `None`
    （`test_shock_anchor_rejects_an_archive_gap_crossing_the_window_start` と同じ仕組み）。
- エピソード: `episode(known_at=datetime(2026, 5, 4, 22, 0, tzinfo=UTC))`。
  ラベルは `T0 + 25h` = `m5(300).start`。`bisect_right(close_times, label)` は `m5(300)`（close = T0+25h05m > label）を指し、
  ラグ 5 分 < `NEWS_MAX_LAG` なので報道アンカーが立つ。
- 日足: `[d1(index, "150") for index in range(3)]` 程度（`daily_entry` が動けばよい）。
- `outcomes = build_outcomes([event], series, ANCHOR)` に対して:
  - `outcomes[SHOCK] == []`
  - `[o.anchor.episode for o in outcomes[NEWS]] == [event]`
  - `outcomes[NEWS][0].anchor.entry == 1`（bars[1] = `m5(300)`）
  - `"4h" in outcomes[NEWS][0].returns`（entry=1 から 48 本先 = bars[49] まで連続しているので horizon が測れる）
- さらに `text = report(outcomes, series, [event], ANCHOR)` で、`"news anchors"` 以降の部分に
  `m5(300).start.isoformat()` が含まれ、かつ `"no shock anchor"` が含まれないことを確認する
  （`text.split("news anchors", 1)[1]` で報道セクション以降を取り出す。ショックセクションには
  `"no shock anchor"` が残るので、分割前の全文で否定アサートしない）。

### 4.2 `known_at` 以降の最初の足が `NEWS_MAX_LAG` 以上遠ければ、独立後も報道アンカーは立たない（新規）

`test_build_outcomes_still_rejects_a_news_bar_beyond_the_lag_limit` のような名前で:

- `known_at = datetime(2026, 5, 3, 21, 5, tzinfo=UTC)`（ラベル = T0 + 5min）、
  足 = `[m5(864 + i, "150") for i in range(60)]`（最初の足の close は T0 + 72h05m、ラベルとの差がちょうど 3 日 = `NEWS_MAX_LAG`）。
  既存 `test_news_anchor_rejects_a_bar_beyond_the_news_lag_limit` と同じ境界値を `build_outcomes` 経由で確認する。
- `outcomes = build_outcomes([episode(known_at=known_at)], series, ANCHOR)` に対して
  `outcomes[NEWS] == []`（ショック側も探索窓に足が無いので `outcomes[SHOCK] == []`）。

### 4.3 既存テスト

- `test_report_contains_missing_overlap_individual_summary_and_profile_sections` は
  `"no shock anchor"` をショックセクションで引き続き満たす（`missing` エピソードは報道側でも
  `"no bar close to known_at"` になる）ので変更不要。落ちた場合は実装側を疑う。
- ショックアンカー系テスト（`test_shock_anchor_*`）は無変更で通ること（ショック側は不変）。
- `tests/unit/test_shock_trigger_study.py` は `intervention_event_study` を import しているので無変更で通ること。

## 5. 完了条件（実行コマンド）

worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-109-news-anchor-independent`

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure -q
.venv/bin/pytest tests/unit/test_intervention_event_study.py tests/unit/test_shock_trigger_study.py -q
```

すべて green（`tests/unit/test_invariants.py` を含む）で、`ruff check .` が無変更で通ること。

## 6. 変更対象ファイル

- `src/trading/backtest/intervention_event_study.py`（`build_outcomes` / `_news_anchor_lines` / `report` と docstring）
- `tests/unit/test_intervention_event_study.py`（テスト 2 本追加）

マイグレーション: 無し。config / スキーマ / 戦略: 変更無し。

## 7. やらないこと

- ショックアンカーの探索・クラスタ（`cluster_anchors`）・統計（`stats` / `baseline_span` / `unconditional`）ロジックの変更
- `policy_event_study.py` / `shock_trigger_study.py` / `rate_differential_study.py` の変更
- `tasks/intervention-event-study.md`（PR #105 当時の計画）や `docs/research/` の書き換え
- スキーマ・config・戦略の変更
- 周辺リファクタ・無関係な整形・追加の抽象化
- コミット（Claude 側で行う）

## 8. 規約（AGENTS.md より転記）

- 金額・価格は `Decimal`。統計計算のみ `float` 可（本変更では新しい数値計算は無い）
- frozen モデル + 新しい値を返す。引数や共有オブジェクトを破壊しない
- 検証はシステム境界だけ。内部関数に防御的分岐・フォールバックを足さない
- WHAT を説明するコメントは書かない。「なぜ」だけ docstring に書く。
  AI レビューの引用や「〜のために追加」のような文脈依存コメントを残さない
- テストデータに実在の人物・団体名を使わない（合成データのみ）
- ruff（`pyproject.toml` の設定）に準拠。型注釈を付ける
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない
- 全てのやり取り・コメントは日本語
