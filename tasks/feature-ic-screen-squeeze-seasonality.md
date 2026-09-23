# 特徴量スクリーンに「圧縮後のレンジ位置」と「同時刻リターンの平均」を追加する

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化・新しい依存）はしない。**コミットもしない**（コミットと PR は Claude が担当）。
作業前に `AGENTS.md`、`.claude/rules/testing-project.md`、`.claude/rules/change-management.md` を読む。
既存ハーネスの設計は [`tasks/feature-ic-screen.md`](feature-ic-screen.md) にある。

## 目的・背景

`src/trading/backtest/feature_screen.py`（PR #228）で、1 時間足の 4 特徴量を事前登録どおり測り、
探索期間の 12 セルすべてが `not_detected` だった（`docs/research/2026-09-23-feature-ic-screen-h1.md`）。
次に、一般的なアルゴ取引手法のうちスポット FX に当てはまり、まだ測っていない 2 つの仮説を同じハーネスで測る。

1. **ボラティリティ圧縮後のレンジ抜け**: 直前のレンジが過去と比べて狭いときに限ると、レンジを抜けた方向へ続く（または戻る）偏りがあるか
2. **時間帯の季節性**: 同じ時刻の過去のリターンの平均が、次の 1 本のリターンを順序づけるか

どちらも既存の 4 種類の特徴量では表せないので、`FeatureSpec` に 2 種類を足す。
統計・判定・出力の規則は変えない。事前登録ノート・計画 JSON・実データでの測定は Claude が別に行う。

## 変更するもの

- `src/trading/backtest/feature_screen.py`: `FeatureSpec` の union に 2 種類を追加し、`Plan.valid` の必要本数の検証と `feature_values` を対応させる
- `tests/unit/test_feature_screen.py`: 下記「テスト」を追加する

`Plan` の `schema_version`（`feature_screen_v1`）とレポートの `schema_version` は変えない。
既存の計画 `docs/research/2026-09-23-feature-ic-screen-h1.plan.json` がそのまま検証を通り、同じ値を出すこと（既存テストが通ること）。

## 追加する特徴量

どちらも、足 t の値には t 以前の足だけを使う。1 つの segment（`split_segments` の区間）の外の足は使わない。
既存の特徴量と同じく、足 t の値は `t >= segment の開始 + indicator_bars - 1` のときだけ出す（それより前は欠測）。
生値が有限でなければ、既存と同じく `ValueError` にする。

### `squeeze_range_position`

パラメータ: `lookback`（int ≥ 1）、`width_window`（int ≥ 1）、`max_width_share`（float、0 < 値 < 1）

- 各足 s について、s を**除く**直前 `lookback` 本の最高値 H_s と最安値 L_s から、幅 `W_s = H_s − L_s` を作る（`range_position` と同じ窓）
- 足 t の直前 `width_window` 個の幅 `W_{t−width_window} … W_{t−1}` のうち、`W_t` より**厳密に小さい**ものの割合を `share_t` とする
- `share_t <= max_width_share` のとき（直前のレンジが過去と比べて狭いとき）だけ、生値を `range_position` と同じ式で出す:
  `(close_t − (H_t + L_t)/2) / ((H_t − L_t)/2)`。`H_t == L_t` なら欠測
- `share_t > max_width_share` なら欠測

必要本数: `lookback + width_window + 1`（`W_{t−width_window}` が足 `t − width_window − lookback` から始まるため）。
`Plan.valid` で `indicator_bars` がこれ以上であることを検証する。

幅は segment ごとに一度だけ計算して使い回す（足ごとに `width_window × lookback` 回の走査をしない。実データは約 1.2 万本）。

### `same_slot_mean_return`

パラメータ: `occurrences`（int ≥ 1）

- 1 本のリターン `r_s = (close_s − close_{s−1}) / pip_size`（pips、`select_samples` と同じく Decimal の差を float にする）を、
  `start_s − start_{s−1}` がちょうど 1 本分（`TIMEFRAME_SECONDS[timeframe]`）で、s と s−1 が同じ segment にあるときだけ有効とする
- 足の「時刻枠」は `start.time()`（broker ラベル軸の時刻）とする
- 足 t の生値は、**足 t の次の足の時刻枠**（`(start_t + 1 本分).time()`）と同じ時刻枠を持つ、s ≤ t の有効なリターンのうち、
  新しいほうから `occurrences` 個の算術平均。`occurrences` 個に満たなければ欠測
- 足 t の次の足が実在するかは見ない（`start_t + 1 本分` の時刻だけで決まる。先のリターンの有効性は `select_samples` が判定する）

有効なリターンは segment の先頭から積み上げる（`indicator_bars` の開始位置より前のリターンも平均に入る）。
時刻枠ごとに長さ `occurrences` の deque を持ち、segment を 1 回走査して求める。

必要本数の検証は足さない（必要な過去は `occurrences` 個の同時刻リターンで決まり、足りなければ欠測になる）。
ただし `Plan.valid` で、この種類を使う計画の `timeframe` が 1 日（86,400 秒）未満であることを検証する（日足以上では時刻枠が意味を持たない）。

## テスト（`tests/unit/test_feature_screen.py` に追加）

実装の写経にしない。壊したら落ちることを確かめる。既存の `make_bar` などのヘルパーを使ってよい。

- `squeeze_range_position`:
  - 手で組んだ足で、幅が過去より狭い足では値が `range_position` の式と一致し、広い足では欠測になる
  - `W_t` と等しい過去の幅は「小さい」に数えない（境界の手計算）
  - PIT: 足 k 以降を書き換えても、k より前の値と Z が変わらない
  - 長い欠損で segment が分かれると、分割後は幅の履歴を持ち越さない
  - `indicator_bars < lookback + width_window + 1` の計画を拒否する
- `same_slot_mean_return`:
  - 手で組んだ足で、次の足の時刻枠の過去 `occurrences` 個の平均と一致する（別の時刻枠のリターンが混ざらない）
  - 週末や欠損をまたぐ差（1 本分でない差）はリターンに入らない
  - `occurrences` 個に満たなければ欠測
  - PIT: 足 k 以降を書き換えても、k より前の値が変わらない
  - 長い欠損で segment が分かれると、分割前のリターンを持ち越さない
  - 日足以上の timeframe の計画を拒否する
- 2 種類を含む計画で CLI を端から端まで実行でき、`report.json` にセルが出る

合成データは seed 固定の `random.Random` で作る。実在の人物・団体名は使わない。

## 検証

```bash
.venv/bin/ruff check .
env -u TRADING_DB_DSN .venv/bin/pytest -q tests/unit/test_feature_screen.py
env -u TRADING_DB_DSN .venv/bin/pytest -q
```

## 実装後の差分レビュー

実装とテストが通ったら、[`.agents/skills/code-review-expert/SKILL.md`](../.agents/skills/code-review-expert/SKILL.md) の手順と
`AGENTS.md` の「AIレビュー指示」で、未追跡を含む全差分をレビューする。
P0〜P2 の指摘はこの計画の範囲内で修正し、検証を再実行する。仕様判断が必要な指摘と P3 は修正せず報告する。

## 範囲外（Claude が行う）

- 事前登録ノート（`docs/research/`）と実データ用の計画 JSON、実データでの測定と結果の追記
- 統計・判定・出力（`summarize`・`verdict`・`run`・`markdown`）の変更
- 既存戦略・config・migrations・storage の変更、新しい依存の追加
- commit・push・PR

## 実装前の照合

実装を始める前に、この計画と `feature_screen.py`・既存テストを照合する。
計画が実際のコードと食い違う点があれば、このファイルを直してから実装し、直した点を報告する。
特徴量の定義（上記の式・窓・欠測の条件）は変えない。変える必要があると判断したら、実装せずに理由を報告する。
