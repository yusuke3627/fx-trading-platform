# H8 キャリーの研究用 CLI の実装計画

- 規範: [設計ノート](../docs/research/2026-09-26-h8-fx-carry-design.md)。**定義・コスト・統計量・判定規則はノートが正本**。
  この計画は、ノートに書いていない実装上の決めごとだけを足す。ノートと食い違ったら実装を止めて報告する
- 作業範囲: 研究ツール 1 本と、そのテスト。実データの取得、スワップの記録、事前登録、実データでの測定は含めない

## 読むもの

- `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- 手本: `src/trading/backtest/event_currency_strength_study.py`（pydantic の frozen な Plan、`sha256()`、入力ファイルの
  ハッシュの照合、`report.json` と `report.md`、出力先を上書きしない `mkdir(exist_ok=False)`、DB を読み取り専用で開く
  `options="-c default_transaction_read_only=on"`、`percentile()` の線形補間）。`src/trading/backtest/run.py` の
  `git_state()` はそのまま使う
- スワップ: `migrations/0007_swap_snapshots.sql`、`src/trading/domain/swap.py`（`rollover_multiplier()` の規則）、
  `src/trading/data/swap/collector.py`（`build_snapshot()` が raw の `symbol_info` 全体を `events` の payload に保存し、
  `swap_snapshots.payload_hash` と同じハッシュを持つ）

## 作るもの

`src/trading/backtest/carry_study.py`。`python -m trading.backtest.carry_study <サブコマンド>` で動く。
DB を読むのは `wedges` だけで、`measure` は固定したファイルだけを読む。

### Plan（JSON、pydantic の frozen モデル、未知のキーは拒否）

ノートの値をすべて Plan に持たせ、コードに直書きしない。最低限、次を含める。

- `study_version`
- `fred_csv_url`: `https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}`
- `usd`: 3 か月金利と翌日物金利の系列名
- `currencies`: 通貨ごとに次を持つ一覧
  - `code`（`JPY` など）
  - `fx_series`
  - `fx_quote`（`usd_per_foreign` か `foreign_per_usd`）
  - `rate_3m_series`、`rate_overnight_series`
  - `first_holding_month`（ユーロだけ 1999-01、他は全期間の開始月）
  - `oanda_symbol`、`oanda_foreign_is_base`（XXXUSD なら true、USDXXX なら false）
- 期間: `full`（1986-01〜2026-08）、`post`（2013-01〜2026-08）、`pre`（1986-01〜2012-12、副）。いずれも保有月
- `signal_lag_months`（3）、`k_divisor`（3）、`gross_per_side`（0.5）
- `transaction_cost_bp`（3）、`day_count`（365）
- `bootstrap_samples`（10000）、`bootstrap_block_months`（6）、`bootstrap_seed`、`one_sided_level`（0.95）
- `reject_sharpe`（0.30）
- `sensitivity`: 上乗せと売買コストの倍率（どちらも 0 と 2）

### サブコマンド

1. **`fetch --plan P --output-dir D`**: Plan にあるすべての系列（為替 7、金利は USD と 7 通貨の 3 か月・翌日物）を
   `fred_csv_url` から取得し、受け取ったバイト列をそのまま `D/<系列名>.csv` に保存する。D が既にあれば失敗する
   - 取得はモジュールの関数を引数で差し替えられるようにし、テストではネットワークに出ない
   - 外部 API の境界なので形式を検証する: 1 行目が `observation_date,<系列名>`、各行の日付が ISO 形式、値は数値・空・
     `.` のいずれか。外れたら失敗する
   - `D/manifest.json` に、Plan の sha256、取得時刻（UTC）、系列ごとの URL・sha256・行数・値のある最初と最後の日付を書く
2. **`wedges --plan P --data-dir D --as-of T --output W`**: 研究用 DB（`TRADING_DB_DSN`、読み取り専用）から、7 ペアの
   スワップの上乗せ `u` を求めて W に書く。W が既にあれば失敗する
   - 各 `oanda_symbol` について `known_at <= T` の最新の `swap_snapshots` を 1 行取る。7 行の `known_at` の差が 10 分を
     超えたら「同じ時刻の記録ではない」として失敗する。どれかのペアの行が無ければ失敗する
   - `point` は、その行の `payload_hash` に対応する `events`（`event_type = 'SWAP_SNAPSHOT_RAW'`）の payload から取る
   - ノートの手順 2〜4 をそのまま実装する（`W` の決め方、年率化、価格は記録した日以前で最後の H.10 の値、理論値は
     固定したデータで両方の通貨に値がある最も新しい月、`u = max(0, 理論値 − 実測の年率)`、`swap_mode` が 1 でなければ失敗）
   - 出力には、ペアごとに行の id・`known_at`・`swap_long`/`swap_short`・`W`・point・価格とその日付・年率の実測・理論値と
     その月・`u_long`・`u_short` を持たせ、Plan と `D/manifest.json` の sha256 を含める
3. **`measure --plan P --data-dir D --wedges W --output-dir O`**: 測定と判定。`report.json` と `report.md` を O に書く。
   - 最初に、Plan・manifest・各データファイル・W の sha256 を照合し、合わなければ何も書かずに失敗する。
     出力先の作成は照合の後にする。O が既にあれば失敗する
   - report には、入力すべての sha256 と `git_state()` を含める

### 計算の決めごと（ノートの定義の実装）

- **価格**: 空と `.` は値なしとして読み飛ばす。値は正でなければ失敗する。`foreign_per_usd` の系列は逆数を取り `S_j` にする。
  リターンの計算は float でよい（研究の統計量。H7 と同じ扱い）
- **金利**: 通貨 `j` の月 `k` の金利は、3 か月の系列にその月の値があればそれ、無ければ翌日物の系列の値、どちらも無ければ
  通貨・月・系列名を示して失敗する。月の値はその月の 1 日付けの行を使う（FRED の月次は月初の日付で出る）
- **保有月の集合**: 全期間の各月 `m` について、`first_holding_month <= m` の通貨を対象にする
- **区切り**: `d_m` は、月 `m` の中で、月 `m` の対象通貨すべてに為替の値がある最初の日。終わりの日は、月 `m+1` の中で、
  **月 `m` の対象通貨**すべてに値がある最初の日。どちらかが見つからなければ失敗する
- **順位と持ち高**: `m−3` の金利の降順、同値は通貨コードの昇順を上位。`K = floor(N / k_divisor)`。上位 `K` に
  `+gross_per_side / K`、下位 `K` に `−gross_per_side / K`、他は 0
- **リターン**: ノートの `r_(j,m)`。為替は単純リターン、金利差は月 `m` 自身の金利、日数は暦日数
- **建て替え直前の比率**: `w̃_(j,m) = w_(j,m−1) × S_j(d_m) / S_j(d_(m−1)) / (1 + net_(m−1))`。前月に持っていない通貨は 0。
  最初の保有月（1986-01）はすべて 0。前月の終わりの日と当月の `d_m` は、対象通貨が変わらない月は定義上同じ日になる。
  違う日になった場合（ユーロが加わる 1999-01 だけ起こり得る）は、間の値動きを数えられないので失敗する
- **売買コスト**: `Σ_j |w_(j,m) − w̃_(j,m)| × transaction_cost_bp / 10000`。全期間の最後に持ち高を閉じるコストは数えない
- **スワップの上乗せ**: `Σ_j |w_(j,m)| × u × 日数 / day_count / 100`。`u` はペアと向きで選ぶ。外貨の買い（`w > 0`）は、
  `oanda_foreign_is_base` なら `u_long`、そうでなければ `u_short`。外貨の売りはその逆
- **純リターン**: 粗リターン − 売買コスト − 上乗せ。`w̃` の更新にもこの値を使う
- **シャープレシオ**: 期間内の月次の純リターンの `平均 / 標準偏差（不偏） × √12`。標準偏差が 0 なら未定義
- **ブートストラップ**: 期間ごとに、長さ `n` の月次の系列から、開始位置を `[0, n)` から一様に選び `bootstrap_block_months`
  個を循環して取るブロックを、`n` 個そろうまで足して切り詰める。1 回ごとにシャープレシオを求める。乱数は
  `random.Random(bootstrap_seed)` を 1 つ作り、全期間 → 発表後の順に使う。下限・上限は `percentile()` の 5・95
  パーセンタイル。未定義の回は除いて回数を出し、全体の 1% を超えたら判定を「判定不能」にして理由を残す
- **判定**: ノートの規則。両方を満たしたときは棄却
- **副（判定に使わない）**: ノートの「副に報告するもの」をすべて出す
  - 粗リターンのシャープレシオ、為替部分と金利差部分それぞれの年率平均
  - 感応度（上乗せ 0 倍・2 倍、売買コスト 0 倍・2 倍を 1 つずつ変えた全期間と発表後の点推定）
  - 発表前の期間の同じ統計量（区間を含む）
  - 純リターンを複利で積んだ最大ドローダウンとその期間、月次の純リターンの歪度、最悪の 1・3・12 か月（複利、時期つき）
  - 通貨ごとの買い側・売り側に入った月の割合、月あたりの平均回転
  - 月次の純リターンと、その月の対象通貨の為替の単純リターンの等加重平均（ドルに対する外貨のバスケット）との相関

## テスト

`tests/unit/test_carry_study.py`（DB を使わない部分）と、`tests/integration/` に `wedges` の小さなテストを 1 本。
架空の値だけを使う。最低限、次を手計算の期待値で確かめる。

- `fetch`: 取得した内容をそのまま保存し、manifest の sha256 が一致する。形式が崩れた CSV を拒む。出力先の上書きを拒む
- 金利: 3 か月 → 翌日物の順で埋める。どちらも無い月で失敗する
- 区切り: 月 `m` の最初の日が一部の通貨で欠けるときに次の日へずれる。終わりの日が月 `m` の対象通貨で決まる
  （ユーロが加わる月の前後）
- 順位: 3 か月前の金利を使う（前月の値を変えても持ち高が変わらない）。同値の並び。`N = 6` と `7` で `K = 2`
- リターン: `foreign_per_usd` の逆数、単純リターン、金利差の日数
- 建て替え直前の比率とコスト: 同じ通貨が続く月にも回転が出る。外れた通貨は全量決済になる。最初の月は 0 から
- 上乗せ: ペアの向き（XXXUSD と USDXXX、買いと売り）の選択。`W` の 3 通り（7 つとも・無し・一部）と、`swap_mode` が 1 以外、
  `max(0, …)` の切り捨て
- ブートストラップ: 同じ seed で一致し、ブロックが循環する
- 判定の 3 分岐と、両方を満たすときの棄却
- `measure`: ハッシュが合わないときに出力先を作らない。出力先の上書きを拒む
- integration の `wedges`: 7 ペアの記録と raw payload を入れた DB から `u` を求める。記録の時刻が 10 分を超えて
  離れていると失敗する

## 完了条件

- `ruff check .` が通る
- `pytest tests/unit/test_carry_study.py` と、追加した integration テストが通る。integration は全 migration を当てた
  使い捨ての DB（`TRADING_DB_DSN` をそこへ向ける）で流す。環境の `TRADING_DB_DSN` は Mac の収集用 DB なので、そのまま
  使わない
- 各サブコマンドの `--help` が動く
- 実データの取得・DB への書き込み・ネットワークへの接続はしない（テストは差し替えた取得関数で行う）
- コミットはしない。変更ファイル、実行した検証とその結果、ノートの定義から外れた点（あれば）を報告する

## 未確定事項

なし。ノートの定義で決まらない点が出たら、推測で埋めずに報告する。
