# 特徴量スクリーン（IC・分位・月次安定性）の研究 CLI

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化・新しい依存）はしない。**コミットもしない**（コミットと PR は別担当）。
作業前に `AGENTS.md`、`.claude/rules/testing-project.md`、`.claude/rules/change-management.md` を読む。

## 目的・背景

これまでの仮説 H1〜H6 は、戦略を丸ごと実装して数時間の研究リプレイを回し、そこで初めて棄却・判定不能と
分かってきた（`docs/research/2026-09-02-*`、`2026-09-06-*`、`2026-09-16-*`、`2026-09-17-*`）。
H4 は粗利 +0.164 pips に対して執行コスト 0.979 pips、H6 は仲値ベースでも負だった。

戦略の前提そのもの（「ある特徴量が先の値動きを順序づけるか」）を、リプレイより前に安く確かめる段を足す。
手法はアクティブ運用の基本式 r = IC × σ × Z に基づく。

- 特徴量を過去窓だけで標準化したスコア Z と、h 本先のリターンとの順位相関（Spearman IC）
- IC が執行コストを上回るのに必要な水準（損益分岐 IC）との比較
- Z の分位ごとの平均リターンが階段状に並ぶか
- 月ごとの IC の符号が安定しているか
- 時間順に分けた探索期間と確認期間

最初の利用先は PR #223（#222、ブレイク後の初回押し目）の本測定前の前提確認。ただしこの CLI は戦略に依存しない。
事前登録ノートと実データでの測定は Claude が別途行う（下記「範囲外」）。

## 追加するもの

- `src/trading/backtest/feature_screen.py`（新規）
- `tests/unit/test_feature_screen.py`（新規）
- 必要なら `tests/fixtures/feature_screen/`（小さな合成入力。大きなファイルを置かず、テスト内生成でもよい）

既存モジュールは変更しない。構成・CLI・出力の作法は `src/trading/backtest/session_study.py` に倣う
（frozen な pydantic `Plan`、入力の sha256、`git_state()`、`--output-dir` は新規作成のみ、終了コード 0/2）。

## CLI

```bash
python -m trading.backtest.feature_screen --plan <plan.json> --bars <bars.csv> --output-dir <新規ディレクトリ>
```

- 出力: `plan.json`（入力計画のバイト列そのまま）、`report.json`、`report.md`
- 終了コード: 0 = レポート生成成功（採用判定ではない）、2 = 入力・出力エラー（`OSError` / `ValueError` / `ValidationError`）

## 入力: 足の CSV（システム境界なのでここで検証する）

研究リプレイ（`trading.backtest.research`）が書き出す `bars_<timeframe>.csv` と同じ形式。
ヘッダは `research.BAR_CSV_HEADER`（`start,open,high,low,close,tick_volume`）と完全一致を要求する。
値は bid の OHLC、`start` は ADR-005 の broker ラベル軸の aware ISO 時刻。

検証（違反は `ValueError`）:
- ヘッダ一致、`start` が timezone-aware、厳密に昇順で重複なし
- `start` が計画の時間足の境界に揃っている（`trading.data.market.bars.bucket_start` と `trading.domain.market.TIMEFRAME_SECONDS` を使う）
- OHLC が有限の正、`high >= max(open, close)`、`low <= min(open, close)`
- ファイル全体の sha256、足の本数、最初と最後の `start` を記録する

価格は `Decimal` で読み、指標と統計の計算は float でよい（AGENTS.md: 指標計算のみ float 可）。
既存の `trading.indicators.atr.atr` と `trading.indicators.ema.ema_series` を使うため、足は
`trading.domain.market.Bar` に組み立てる。`symbol` / `timeframe` は計画から設定する。
CSV に実観測時刻 `known_at` は含まれないため、指標関数へ渡すためだけに
`start + TIMEFRAME_SECONDS[timeframe]` を `known_at` に設定する。この値は broker ラベル軸の
仮置きであり、実 UTC の観測時刻を復元したものではない。PIT の判定には使用せず、
この CLI の窓・期間判定は計画どおり `start` / `close_time` の broker ラベル軸で行う。

## 計画（Plan）

`schema_version: "feature_screen_v1"`。`extra="forbid"`・frozen。項目:

| 項目 | 型・制約 | 意味 |
|---|---|---|
| `population_description` | str（空不可） | 入力の出所の説明 |
| `basis` | `"replay_bars"` / `"simulated"` | `simulated` は合成データ。全セルの判定を `synthetic_only` にする |
| `instrument` | `InstrumentSpec` | pip 換算に `pip_size` を使う（session_study と同じく計画に持つ） |
| `timeframe` | `TIMEFRAME_SECONDS` のキー | 入力足の時間足 |
| `explore` / `confirm` | `{start, end}`（aware、半開区間、broker ラベル） | `explore.end <= confirm.start` を要求 |
| `max_gap_hours` | float > 0 | 連続する足の `start` の差がこれを超えたら系列を分割する（週末は通すが長期欠損で切る） |
| `indicator_bars` | int | 指標を計算する末尾の足の本数。既定 200（`trading.indicators.DEFAULT_BAR_COUNT`）。各特徴量の必要本数以上を要求 |
| `atr_period` | int ≥ 1 | 既定 14 |
| `normalization_window` | int ≥ 20 | Z の標準化に使う直前の生値の個数 |
| `entry_z` | float > 0 | 損益分岐 IC の平均 \|Z\| を取る閾値（\|Z\| ≥ entry_z） |
| `features` | 下記 FeatureSpec のタプル（1 件以上、`id` 一意） | |
| `horizons` | 正の int のタプル（一意・昇順） | 何本先のリターンを見るか |
| `round_trip_cost_pips` | Decimal > 0 | 往復の執行コスト |
| `cost_rationale` | str（空不可） | コストの根拠 |
| `quantiles` | int 2〜10 | 分位の数 |
| `min_samples` | int ≥ 30 | 判定に要る標本数 |
| `min_month_samples` | int ≥ 5 | 月次 IC を数える月の最小標本数 |
| `staircase_min` | float 0〜1 | 分位の階段の単調性の下限 |
| `month_sign_min` | float 0〜1 | 月次 IC の符号一致率の下限 |
| `confidence` | float（0.5〜0.999、既定 0.90） | 両側信頼水準 |

FeatureSpec は `kind` で判別する union。数値パラメータは計画に書き、コードに既定値を埋めない（`atr_period` と `indicator_bars` を除く）。
各 FeatureSpec は共通して空でない `id` を持つ。

| kind | パラメータ | 生値（足 t の確定時点。t を含む過去の足だけ） |
|---|---|---|
| `range_position` | `lookback` | t を**除く**直前 `lookback` 本の最高値 H・最安値 L。`(close_t − (H+L)/2) / ((H−L)/2)`。H == L なら欠測 |
| `ema_slope_atr` | `ema_period`, `slope_lookback` | 末尾 `indicator_bars` 本の終値の `ema_series`。`(ema[-1] − ema[-1−slope_lookback]) / ATR_t` |
| `distance_from_ema_atr` | `ema_period` | `(close_t − ema[-1]) / ATR_t` |
| `momentum_atr` | `lookback` | `(close_t − close_{t−lookback}) / ATR_t` |

ATR_t は末尾 `indicator_bars` 本に `atr(..., atr_period)`。None または 0 以下なら欠測。
既存 API の必要本数に合わせ、`indicator_bars` の下限を次のとおり検証する。
`range_position` は `lookback + 1`、`momentum_atr` は `max(lookback + 1, atr_period + 1)`、
`distance_from_ema_atr` は `max(ema_period, atr_period + 1)`、`ema_slope_atr` は
`max(ema_period + slope_lookback, atr_period + 1)`。`atr` は `period + 1` 本を要求し、
`ema_series` は先頭の SMA から始まる長さ `入力本数 - period + 1` の配列を返す。

## 計算規則（PIT と非重複）

1. **系列の分割**: 連続する足の `start` 差が `max_gap_hours` を超える所で区切る（区間 = segment）。
   以降のあらゆる窓は 1 つの segment の中に収まる必要がある。
2. **特徴量**: 足 t の値には t 以前の足だけを使う。末尾 `indicator_bars` 本が同じ segment 内に無ければ欠測。
3. **Z**: 同じ segment 内の直前 `normalization_window` 個の有効な生値（t の値を**含めない**）の平均と
   母標準偏差で `(x_t − mean) / std`。個数不足・std == 0 は欠測。全期間の統計で標準化しない（look-ahead）。
   `shock_trigger_study.z_scores` と同じ考え方。
4. **先のリターン**: エントリーは足 t の確定時点（`start_t + 時間足`）。
   `r = (close_{t+h} − close_t) / pip_size`（bid 終値どうし、pips）。
   `start_{t+h} − start_t == h × 時間足` を満たすときだけ有効（週末・欠損・休場をまたぐ窓は無効）。
5. **期間への割り当て**: `period.start <= close_time(t)` かつ `close_time(t+h) <= period.end` の標本だけを
   その期間に入れる（探索期間のリターンが確認期間に食い込まない）。
6. **非重複の間引き**: horizon と期間ごとに、Z とリターンが有効な t を時間順に走査し、
   直前に採用した足からの index 差が h 以上なら採用する（決定的な貪欲法）。

## 統計（特徴量 × horizon × 期間ごと）

- `n`、`ic`（Spearman。同順位は平均順位）、参考の `pearson`
- `ic` の信頼区間: Fisher 変換 `atanh(ic) ± z_crit × 1.06 / sqrt(n − 3)` を `tanh` で戻す
  （1.06 は Spearman 用の Fieller–Hartley–Pearson 近似）。
  `z_crit = NormalDist().inv_cdf(1 − (1 − confidence) / (2m))`。m は Bonferroni の族の大きさ（下記）
- `sigma_pips`（r の母標準偏差）、`mean_return_pips`
- エントリー標本（\|Z\| ≥ `entry_z`）: 件数、`mean_abs_z`、参考の `entry_mean_pips = mean(sign(Z) × sign(ic) × r)` と
  `entry_net_pips = entry_mean_pips − round_trip_cost_pips`
- `break_even_ic = round_trip_cost_pips / (sigma_pips × mean_abs_z)`（エントリー標本 0 件なら None）
- 分位: Z で昇順に並べ（同値は時刻順）、位置 p の標本を分位 `p × quantiles // n` に入れる。
  各分位の件数・Z の範囲・平均リターン（pips）。`staircase = Spearman(分位番号, 分位の平均リターン)`
- 月次: `close_time(t)` の broker ラベル月ごと。標本が `min_month_samples` 以上の月だけの IC を並べ、
  累積和、月次 IC の平均と t 値（平均 ÷ (標本標準偏差 ÷ √月数)）、全体の IC と符号が一致する月の割合 `month_sign_share`

## 判定（事前登録で固定する規則。上から順に最初に該当したもの）

探索期間（族 m = 特徴量数 × horizon 数）:

| 条件 | 判定 |
|---|---|
| `basis == "simulated"` | `synthetic_only`（統計は全部出す） |
| `n < min_samples` またはエントリー標本 0 件 | `insufficient` |
| 信頼区間が 0 を含む | `not_detected` |
| `abs(ic) < break_even_ic` | `below_cost`（統計的にはあるがコストに届かない） |
| `staircase × sign(ic) < staircase_min`、または `month_sign_share < month_sign_min`、または数えられる月が 0 | `unstable` |
| それ以外 | `candidate` |

確認期間は、探索期間で `candidate` のセルだけを判定する（族 m = 候補の数）。それ以外のセルは統計だけ出して `not_evaluated`。

| 条件 | 判定 |
|---|---|
| `n < min_samples` またはエントリー標本 0 件 | `insufficient` |
| `sign(ic)` が探索期間と逆、または信頼区間が 0 を含む | `not_confirmed` |
| `abs(ic) < break_even_ic`（確認期間の値で計算） | `below_cost` |
| それ以外 | `confirmed` |

各判定には、どの条件で落ちたかを示す理由を添える。

## 出力

`report.json`（`schema_version: "feature_screen_report_v1"`）:
`profitability_established: false`、`plan`、`plan_sha256`、`bars`（sha256・本数・最初と最後・segment の一覧）、
`git`（`trading.backtest.run.git_state()`）、`family_size`（探索・確認）、`z_crit`、
`cells`（特徴量 × horizon ごとに探索・確認の統計と判定・理由、分位、月次）、
`ic_decay`（特徴量ごとに horizon 順の探索・確認の IC）。

`report.md`: 日本語。冒頭に「前提の選別用で、収益性・将来の再現性は示さない」旨と `basis`。
探索・確認それぞれの表（特徴量・horizon・n・IC・区間・損益分岐 IC・staircase・月次符号一致率・判定）、
IC 減衰の表、分位の表。`simulated` なら合成データであることを明記する。

## テスト（`tests/unit/test_feature_screen.py`）

実装の写経にしない。壊したら落ちることを確かめる。

- Spearman: 同順位を含む手計算の値と一致する
- **PIT**: 足 k 以降を書き換えても、k より前の特徴量と Z が変わらない
- 非重複: 採用された標本の index 差が h 以上。週末・長期欠損・期間境界をまたぐ窓が入らない
- 合成データで r が Z に比例する成分を持つとき、IC が正で `candidate` → `confirmed` になる（`basis: replay_bars`）。
  純粋なノイズでは `not_detected`。同じ入力で `basis: simulated` なら `synthetic_only`
- 損益分岐 IC の式と、ic が損益分岐未満のとき `below_cost` になること
- 分位の階段が逆向き・非単調のとき `unstable`
- Bonferroni: 族が大きいほど `z_crit` が大きい
- 入力検証: ヘッダ不一致・非昇順・時間足境界ずれ・高安の矛盾で `main` が 2 を返す。`--output-dir` が既存なら 2
- CLI の端から端: 3 ファイルが作られ、`report.json` に入力 hash と `plan_sha256` が入る

合成データは seed 固定の `random.Random` で作る。実在の人物・団体名は使わない。

## 検証

```bash
.venv/bin/ruff check .
env -u TRADING_DB_DSN .venv/bin/pytest -q tests/unit/test_feature_screen.py
env -u TRADING_DB_DSN .venv/bin/pytest -q
```

加えて、テストの合成入力で CLI を実際に実行し（出力は `tmp/` 配下）、`report.md` の中身を確認する。

## 範囲外（Claude が行う、または別作業）

- 事前登録ノート（`docs/research/`）、実データ用の計画 JSON、実データでの測定と結果の追記
- 既存戦略・config・migrations・storage の変更、DB からの読み込み、新しい依存の追加
- LASSO・ExtraTrees などの機械学習による特徴選択
- commit・push・PR

## 実装前の照合

実装を始める前に、この計画とコード（`session_study.py`、`research.py` の `BAR_CSV_HEADER`・`write_bar`、
`domain/market.py` の `Bar` / `TIMEFRAME_SECONDS`、`data/market/bars.py` の `bucket_start`、
`indicators/atr.py`・`ema.py`、`run.git_state`）を照合する。
計画が実際の API と食い違う点があれば、このファイルを直してから実装し、直した点を報告する。
統計・判定の規則そのものは変えない（変える必要があると判断したら、実装せずに理由を報告する）。

照合済み: CSV ヘッダは改行付き文字列で、`write_bar` は `known_at` を出力しない。
`git_state()` は引数なしで、`git_commit` / `git_dirty` と、変更がある場合は
`git_diff_sha256` を返す。`session_study` と同じく、その返り値を `git` に保存する。
上記の修正は API の参照先・必須フィールド・必要本数の明記のみで、統計・判定の規則は変更しない。
