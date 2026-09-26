# H9 バリューの研究用 CLI の実装計画

- 規範: [設計ノート](../docs/research/2026-09-27-h9-fx-value-design.md)。**定義・データ・コスト・統計量・判定規則はノートが正本**。
  この計画は、ノートに書いていない実装上の決めごとだけを足す。ノートと食い違ったら実装を止めて報告する
- 作業範囲: 研究ツール 1 本（`value_study.py`）と、`carry_study.py` の最小の変更、そのテスト。実データの取得、事前登録、
  実データでの測定は含めない
- H8 のハーネス（`carry_study.py`）は事前登録済みの H8 の結果を再現できなければならない。変更は下の 2 点だけにし、
  H8 の計算の順序と値を変えない

## 読むもの

- `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- `src/trading/backtest/carry_study.py`（H8 のハーネス。Plan・`load_data`・`load_wedges`・`common_date`・`usd_price`・
  `monthly_returns`・`measure`・`bootstrap`・`decide`・`secondary`・`render_markdown`・`measure_files`・`parse_csv`）
- `tests/unit/test_carry_study.py`、`tests/support.py` の `carry_plan()`・`carry_data()`
- 取得元の見本（ネットワークに出ずに形式を確かめるためのもの。コミットしない）: `tmp/h9-samples/`
  - `CPIAUCNS.csv`（FRED）、`oecd_JPN_M.csv`・`oecd_AUS_Q.csv`（OECD SDMX の csvfile。行は期間順に並んでいない）、
    `zmi2020aa.csv`（総務省、Shift_JIS）、`de_minr.csv`（Eurostat SDMX-CSV）、`ons_cdko.csv`・`ons_d7bt.csv`（英国統計局。
    年・四半期・月の行が混ざる）、`snb.csv`（スイス国立銀行。BOM 付き、`;` 区切り）

## `carry_study.py` の変更（2 点だけ）

1. `monthly_returns` と `measure` に、キーワード引数 `weights_for: Callable[[str, Sequence[Currency]], dict[str, float]] | None = None`
   を足す。`None` なら今の `target_weights(plan, data, active, month)` を使う。`measure` は受け取った `weights_for` を、主の計算と
   感応度の計算の両方の `monthly_returns` に渡す
2. `render_markdown` にキーワード引数 `title: str = "H8 キャリー研究"` を足し、1 行目の見出しに使う

既存の `tests/unit/test_carry_study.py` と `tests/integration/test_carry_study.py` を変えずに通すこと。

## 作るもの

`src/trading/backtest/value_study.py`。`python -m trading.backtest.value_study <サブコマンド>` で動く。DB は使わない。

### Plan（JSON、pydantic の frozen モデル、未知のキーは拒否）

ノートの値をすべて Plan に持たせ、コードに直書きしない。最低限、次を含める。

- `study_version`、`bootstrap_seed`
- `carry`: H8 から読む 4 ファイルの期待する sha256（`plan_sha256`・`manifest_sha256`・`wedges_sha256`・`report_sha256`）
- 期間: `full`（1986-01〜2026-08）、`post`（2013-07〜2026-08）、`pre`（1986-01〜2013-06）
- `first_holding_months`: 7 通貨それぞれの最初の保有月（EUR だけ 2004-07、他は 1986-01）。キーは H8 の Plan の通貨コードと
  一致すること
- `cpi_lag_months`（2）、`cpi_max_lag_months`（4）、`change_months`（60）、`fx_average_first_offset`（66）、
  `fx_average_last_offset`（54）
- `sources`: 取得元ごとに `name`・`url`・`format`（`fred`・`oecd`・`stat_jp`・`eurostat`・`ons`・`snb`）と、形式の検証に使う
  識別子（FRED の系列名、OECD の `REF_AREA`・`FREQ`、ONS の `CDID`、Eurostat の `geo`・`unit`・`coicop18`、SNB の
  cube と `D0`）。取得元は 10 本: `CPIAUCNS`（USD）、OECD の JPN・CAN（月次）と AUS・NZL（四半期）、総務省 2020 年基準、
  Eurostat の DE、ONS の CDKO と D7BT、SNB。URL はノートと `tmp/h9-samples/` の取得に使ったものと同じにする
  - OECD: `https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/{AREA}.{FREQ}.N.CPI.IX._T.N._Z?startPeriod=1978-01&dimensionAtObservation=AllDimensions&format=csvfile`
  - Eurostat: `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_minr/M.I25.TOTAL.DE?format=SDMX-CSV&startPeriod=1990-01`
  - ONS: `https://www.ons.gov.uk/generator?format=csv&uri=/economy/inflationandpriceindices/timeseries/{cdko|d7bt}/mm23`
  - SNB: `https://data.snb.ch/api/cube/plkopr/data/csv/en`
  - 総務省: `https://www.stat.go.jp/data/cpi/2020/csv/zmi2020aa.csv`
  - FRED: `https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCNS`
- 通貨ごとの物価の割り当て: USD・CAD・AUD・NZD・CHF・EUR は 1 本。JPY は OECD と総務省の 2 本とつなぐ月
  （`jpy_link_month` = 2020-01）。GBP は RPI と CPI の 2 本と切り替えの月（`gbp_cpi_first_month` = 1996-01）
- `jpy_revision_windows`: 日本の基準改定で直る期間と置き換え先の月の一覧（1980〜2020 年の 9 本。`start` = B-01、
  `end` = (B+1)-06、`substitute` = (B−1)-12）

### サブコマンド

1. **`fetch --plan P --output-dir D`**: `sources` をすべて取得し、受け取ったバイト列をそのまま `D/<name>.csv` に保存する。
   D が既にあれば失敗する
   - 取得はモジュールの関数を引数で差し替えられるようにし、テストではネットワークに出ない（`carry_study.fetch` と同じ形）
   - 外部 API の境界なので形式を検証する（下の「読み方」）。外れたら失敗する
   - `D/manifest.json` に、Plan の sha256、取得時刻（UTC）、取得元ごとの URL・sha256・値の数・最初と最後の月を書く
2. **`measure --plan P --cpi-dir D --carry-plan CP --carry-data-dir CD --wedges W --carry-report R --output-dir O`**:
   測定と判定。`report.json` と `report.md` を O に書く
   - 最初に、次を照合し、どれかが合わなければ何も書かずに失敗する。出力先の作成は照合の後にする。O が既にあれば失敗する
     - H9 の Plan と CPI の manifest・各ファイルの sha256（`carry_study.load_data` と同じ形で、manifest と中身の一致も確かめる）
     - `CP`・`CD/manifest.json`・`W`・`R` の sha256 が Plan の `carry` と一致する（`W` は `carry_study.load_wedges` で読み、
       H8 の Plan と manifest との対応も確かめる）
     - `R` に記録された `provenance` の Plan・manifest・上乗せの sha256 が、`CP`・`CD`・`W` と一致する
     - H8 を `carry_study.monthly_returns(h8_plan, data, wedges)` で計算し直し、月の並びと各月の `net` が `R` の `monthly` と
       完全に一致する（`==`）。一致しなければ失敗する
   - report には、入力すべての sha256 と `git_state()` を含める

### 物価の読み方（外部データの境界）

どの形式も、値は有限の正の数でなければ失敗する。同じ期間が 2 回出たら失敗する。値の空欄は値なしとして読み飛ばす。
結果は「月 → 値」の辞書にし、四半期は最後の月（Q1 → 3 月）の値とする。

- `fred`: `carry_study.parse_csv(content, series, positive=True)` を使い、月の 1 日付けの行を月の値にする
- `oecd`: ヘッダーに `REF_AREA`・`FREQ`・`METHODOLOGY`・`MEASURE`・`UNIT_MEASURE`・`ADJUSTMENT`・`TIME_PERIOD`・`OBS_VALUE` が
  あること。全行が期待する `REF_AREA`・`FREQ` で、`METHODOLOGY=N`・`MEASURE=CPI`・`UNIT_MEASURE=IX`・`ADJUSTMENT=N`・
  `EXPENDITURE=_T` であること。`TIME_PERIOD` は月次なら `YYYY-MM`、四半期なら `YYYY-Qn`。行の並びに依存しない
- `stat_jp`: Shift_JIS（cp932）で読む。1 行目の 2 列目が `総合`、2 行目の 2 列目が `All items` であることを確かめ、
  1 列目が 6 桁の `YYYYMM` の行の 2 列目を値にする
- `eurostat`: SDMX-CSV。全行が `freq=M`・期待する `unit`・`coicop18`・`geo` であること。`TIME_PERIOD` と `OBS_VALUE` を使う
- `ons`: `"CDID"` の行が期待する系列名であること。1 列目が `YYYY MON`（`MON` は英語の月の 3 文字の大文字）の行だけを
  月の値にし、年（`YYYY`）と四半期（`YYYY Qn`）の行は無視する
- `snb`: UTF-8（BOM 付き）、`;` 区切り、値は `"` で囲まれる。`"CubeId";"plkopr"` を確かめ、`"Date";"D0";"Value"` の見出しの後の
  行のうち `D0` が `LD2010100` の行を使う

### 計算の決めごと（ノートの定義の実装）

- **H8 の Plan から H9 の見方を作る**: H8 の Plan を読み、`study_version`・`bootstrap_seed`・`full`・`post`・`pre` を H9 の値に、
  各通貨の `first_holding_month` を `first_holding_months` に置き換えた `carry_study.Plan` を検証つきで作る
  （`Plan.model_validate`）。売買コスト・上乗せ・`k_divisor`・`gross_per_side`・ブートストラップの回数とブロック・区間の
  水準・棄却の閾値・感応度の倍率は、H8 の Plan の値をそのまま使う
- **主の計算**: `carry_study.measure(h9_view, data, wedges, weights_for=value_weights)`。区切り `d_m`、リターン、`w̃`、
  売買コスト、上乗せ、ブートストラップ（全期間 → 発表後 → 発表前）、判定、副統計、感応度は `carry_study` のもの
- **`value_weights(month, active)`**: ノートの指標 `V_(j,m)` で並べ、`carry_study.target_weights` と同じ方法（大きい順、同値は
  通貨コードの昇順、`K = floor(N / k_divisor)`、`±gross_per_side / K`）で持ち高を返す。`V` は float で計算してよい
  - `d_m = carry_study.common_date(data, active, month)`（`monthly_returns` と同じ日になる）
  - `b_(j,m)`: 通貨 `j` の為替の系列で `d_m` より前の最後の日。無ければ失敗する
  - `S̄_(j,m)`: `m − fx_average_first_offset` 月から `m − fx_average_last_offset` 月までの各月で、通貨 `j` の値がある
    最初の日の `usd_price` の単純平均。どれかの月に値が無ければ失敗する
  - `e`（月次）: `m − cpi_lag_months` 月以前で値がある最も新しい月。`m − cpi_max_lag_months` 月より前になれば失敗する
  - `e`（四半期）: 最後の月が `m − cpi_lag_months` 月以前にある四半期のうち値がある最も新しいものの最後の月。同じ上限で失敗する
  - JPY: 月次の規則で決めた `e` が `jpy_revision_windows` のどれかの期間に入れば、その `substitute` に置き換える。
    置き換えた場合は上限の検査をしない。JPY の値は、`jpy_link_month` より前は OECD の値に
    「`jpy_link_month` の総務省の値 ÷ `jpy_link_month` の OECD の値」を掛けたもの、以後は総務省の値とする
  - GBP: `e − change_months` が `gbp_cpi_first_month` より前なら RPI、そうでなければ CPI を、両端に同じ系列として使う
  - `e − change_months` の値が無ければ失敗する。USD 側も同じ規則で自分の `e_USD` を決める
  - `V = −[ln(S_j(b) / S̄) + (ln P_j(e_j) − ln P_j(e_j − 60)) − (ln P_USD(e_USD) − ln P_USD(e_USD − 60))]`
- **各月の指標の記録**: report の `signals` に、月ごとに `d_m` と、通貨ごとの `V`・`b`・`S̄`・`e`・使った系列・置き換えの有無、
  `e_USD` を残す
- **副（判定に使わない）**: `carry_study.measure` が出すもの（H8 と同じ一式）に加えて、次を出す
  - H8 の月次の純リターン（計算し直したもの）との相関。保有月でそろえ、全期間・発表後・発表前
  - 50/50 の組み合わせ: H8 の Plan に H9 の期間を入れた見方（通貨の最初の保有月は H8 のまま、ユーロは 1999-01）で、
    `carry_study.monthly_returns(combo_view, data, wedges, weights_for=combo_weights)` を計算する。`combo_weights(month, active)`
    は、通貨ごとに `0.5 × carry_study.target_weights(h8_plan, data, active, month) + 0.5 × H9 の主の計算のその月の持ち高`
    （H9 で対象外の通貨は 0）。区切りの日は `monthly_returns` が `active`（H8 の対象通貨）から決める。全期間・発表後・
    発表前の年率シャープレシオの点推定と年率の平均純リターンを出し、月次の内訳も report に残す
- **report.md**: `carry_study.render_markdown(report, title="H9 バリュー研究")` の後に、H8 との相関、組み合わせ、
  JPY の置き換えが起きた保有月の一覧を足す

## テスト

`tests/unit/test_value_study.py`（DB を使わない）。架空の値だけを使い、`tests/support.py` に H9 の Plan と物価の小さな
データを作る関数を足す。最低限、次を手計算の期待値で確かめる。

- 6 つの形式の読み方: `tmp/h9-samples/` と同じ形の小さな架空の入力で正しく読む。ヘッダー・識別子・期間の形式・負の値・
  期間の重複を拒む（見本のファイルそのものはテストに使わない）
- `fetch`: 取得した内容をそのまま保存し、manifest の sha256 が一致する。形式が崩れた応答を拒む。出力先の上書きを拒む
- `e` の選び方: 月次の欠け（前の月へ下がる）、上限を超えると失敗、四半期の対応、JPY の置き換え（上限の検査をしない）、
  5 年前の値が無いと失敗
- JPY のつなぎ方と、GBP の RPI・CPI の切り替え（境目の前後の月）
- `b` は `d_m` より前の最後の日で、`d_m` の値の変化が持ち高を変えない。`S̄` は 13 か月の平均
- `V` の値と順位、同値の並び、ユーロが加わる前後で `K` が 2 のまま
- `carry_study` の変更: `weights_for` を渡さない場合の結果が変わらない（既存テストが通る）。`weights_for` を渡すと主と感応度の
  両方に使われる
- 組み合わせ: 同じ通貨の逆向きの持ち高が相殺され、上乗せが相殺後の向きで数えられる。50/50 に戻す回転がコストに入る
- `measure`: いずれかのハッシュ、report の provenance、H8 の計算し直しが合わないときに、出力先を作らずに失敗する。
  出力先の上書きを拒む

## 完了条件

- `ruff check .` が通る
- `pytest tests/unit` が通る。integration は `TRADING_DB_DSN` を指定せずに unit だけを流す（DB を使うテストは Claude が
  使い捨ての DB で流す）
- 各サブコマンドの `--help` が動く
- 実データの取得・ネットワークへの接続・DB への接続はしない（テストは差し替えた取得関数で行う）
- コミットはしない。変更ファイル、実行した検証とその結果、ノートの定義から外れた点（あれば）を報告する

## 未確定事項

なし。ノートの定義で決まらない点が出たら、推測で埋めずに報告する。
