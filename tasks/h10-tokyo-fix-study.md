# H10 五十日の仲値前のドル高の研究用 CLI の実装計画

- 規範: [設計ノート](../docs/research/2026-09-27-h10-tokyo-fix-design.md)。**日の定義・価格・コスト・統計量・判定規則はノートが正本**。
  この計画は、ノートに書いていない実装上の決めごとだけを足す。ノートと食い違ったら実装を止めて報告する
- 作業範囲: 研究ツール 1 本（`src/trading/backtest/tokyo_fix_study.py`）とそのテスト。実データの取得、DB への接続、
  事前登録、実データでの測定は含めない
- 既存のモジュールは変更しない。部品は import して使う

## 読むもの

- `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- `src/trading/backtest/carry_study.py`（`Record`・`digest`・`json_bytes`・`write_json`・`git_state` の使い方・`measure_files` の照合の順序・
  `main` の DB 接続（`TRADING_DB_DSN` を読み取り専用で開く））
- `src/trading/data/market/dukascopy.py`（`hour_url`・`decode_bi5`・`POINT_SCALES`・`known_to_broker_label`）
- `src/trading/backtest/event_currency_strength_study.py` の `percentile`
- `tests/unit/test_dukascopy_importer.py`（bi5 のバイト列の作り方）、`tests/integration/test_carry_study.py`（DB を使うテストの形）、
  `tests/support.py`

## 作るもの

`src/trading/backtest/tokyo_fix_study.py`。`python -m trading.backtest.tokyo_fix_study <fetch|quotes|measure>` で動く。

### Plan（JSON、`carry_study.Record` を基底にした frozen モデル、未知のキーは拒否）

ノートの値をすべて Plan に持たせ、コードに直書きしない。最低限、次を含める。

- `study_version`、`bootstrap_seed`、`bootstrap_samples`（10,000）、`one_sided_level`（0.95）、`reject_threshold_bp`（0.5）、
  `cost_multipliers`（0 と 2）、`trim_fraction`（0.01）、`secondary_block_months`（6）
- `symbol`（`USDJPY`）、`pip_size`（`0.01`、Decimal）
- `holiday_csv_url`（`https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv`）、`bank_closed_days`（`12-31`・`01-02`・`01-03`）、
  `gotobi_days`（5・10・15・20・25・30）。月の最後の日は常に五十日に入れる
- 時刻（日本時間、`HH:MM:SS`）: `entry`（09:30:00）、`exit`（09:52:00）、副の論文の形の `paper_entry`（09:50:00）・`paper_switch`（09:55:00）・
  `paper_exit`（10:00:00）。`quote_window_seconds`（60）
- 期間（日本時間の暦日、両端を含む）: `fetch_range`（2010-01-01〜2026-08-31）、`calibration`（2010-01-01〜2013-12-31）、
  `main`（2014-01-01〜2026-08-31）、`post`（2015-10-01〜2026-08-31）、`oanda`（2024-07-23〜2026-08-31）。
  `calibration`・`main`・`post`・`oanda` は `fetch_range` の中にあること
- `oanda`: `source`（`MT5`）、`server_ahead_of_ny_hours`（7）、`max_tick_id`（研究用 DB の `market_ticks.id` の上限。事前登録のときに埋める）

### 日の決め方

- **祝日の CSV**（外部データの境界）: Shift_JIS（cp932）。1 行目が `国民の祝日・休日月日,国民の祝日・休日名称` であること。以降の行は
  `YYYY/M/D,名称`。日付の形式が崩れていれば、同じ日付が 2 回出れば失敗する。`fetch_range` の最初の年から最後の年までの各年に
  1 行以上あること
- **営業日**: 土日でなく、祝日の CSV に無く、`bank_closed_days` に当たらない日
- **五十日**: 各月について、`gotobi_days` のうちその月にある日と、月の最後の日を取り、営業日でなければその日より前の最後の営業日に
  移す。重なれば 1 日にする。期間に入るかは、**移した後の日付**で決める（2014-01-05 が 2013-12-30 に移れば見当付けの期間の日）。
  そのため期間の最初の月の 1 つ前の月から計算する

### サブコマンド 1: `fetch --plan P --output-dir D`

- 取得するもの: 祝日の CSV を `D/syukujitsu.csv` に、`fetch_range` の各営業日 `d` について Dukascopy の `hour_url(symbol, d の 00:00 UTC)`
  （UTC 0 時台 = 日本時間 9 時台）を `D/dukascopy/YYYY-MM-DD.bi5` に、受け取ったバイト列をそのまま保存する。空のボディは空のファイルとして
  保存する。HTTP 404 は `D/dukascopy/YYYY-MM-DD.missing`（中身は `404` の 3 バイト）を置く
- **再開**: D が既にあり `D/manifest.json` が無ければ、続きから取る。既にある `.bi5`・`.missing` は取り直さず、中身もそのまま使う。
  `D/syukujitsu.csv` が既にあれば取り直さない。`D/manifest.json` が既にあれば失敗する
- **取得の仕方**: 1 本の HTTPS 接続（`http.client.HTTPSConnection`、keep-alive）を使い回し、要求の間に 0.5 秒あける。
  接続を毎回張ると、数分で TLS の接続確立を拒否されたため（設計ノートの「期間」）。接続の失敗・タイムアウト・404 以外の HTTP の
  失敗は、接続を張り直し、待ち時間を 5・15・45・120・300 秒と伸ばし、その後は 600 秒で取り直す。600 秒の待ちが 12 回続いたら、
  取得済みの分を残して失敗する（同じコマンドで再開できると表示する）。待ちと取得は引数で差し替えられるようにし、テストでは
  ネットワークに出ず、待たない
- 取得したら `decode_bi5` で復号できることを確かめる（外部データの境界）。できなければ失敗する
- すべての営業日のファイルか `.missing` が揃ったら、`D/manifest.json` を書く: Plan の sha256、完了時刻（UTC）、祝日の CSV の
  URL・sha256・バイト数、営業日の数、日ごとの `{sha256, bytes, ticks}`（`.bi5`）の一覧、`.missing` の日の一覧。
  manifest に載せるのは `fetch_range` の営業日だけで、ディレクトリにほかのファイルがあっても無視する

### サブコマンド 2: `quotes --plan P --data-dir D --output O`

研究用 DB から OANDA の気配を読み、ファイルに固定する。DB は `TRADING_DB_DSN` を読み取り専用で開く（`carry_study.main` と同じ形）。

- 最初に D の manifest と Plan の sha256 を照合する（祝日の CSV から営業日と五十日を作るため）。O が既にあれば失敗する
- `oanda` の期間の各営業日について、次の 5 つの気配を読む。時刻 `T` は日本時間の `d` の時刻で、実 UTC に直してから
  `known_to_broker_label(実 UTC, timedelta(hours=server_ahead_of_ny_hours))` で DB の `event_time` の軸に写す
  - `entry`・`exit`・`paper_entry`・`paper_switch`: `T` 以後で `T + quote_window_seconds` より前の最初の気配
    （`ORDER BY event_time, id LIMIT 1`）
  - `paper_exit`: `T − quote_window_seconds` 以後で `T` より前の最後の気配（`ORDER BY event_time DESC, id DESC LIMIT 1`）
  - どれも `symbol = plan.symbol AND source = plan.oanda.source AND id <= plan.oanda.max_tick_id`。SQL はプレースホルダで組む
- O（JSON）に書くもの: Plan の sha256、D の manifest の sha256、`max_tick_id`、日ごとの 5 つの気配（見つからなければ null。
  あれば `id`・`event_time`・`bid`・`ask` を文字列で）、そして**スプレッドの要約**: 5 つの時刻それぞれについて、`oanda` の期間の
  **五十日**のうち気配がある日のスプレッド（`(ask − bid) / pip_size`、pips）の平均と日数。値動き（気配どうしの差）は計算しない
- 要約のスプレッドが主と副のコストに使う値になる。事前登録ではこの値を記録する

### サブコマンド 3: `measure --plan P --data-dir D --quotes Q --output-dir O`

- 最初に次を照合し、どれかが合わなければ何も書かずに失敗する。O が既にあれば失敗する。出力先の作成は照合の後にする
  - D の manifest の Plan の sha256 と、manifest に載ったすべてのファイル（祝日の CSV、`.bi5`）の sha256。`.missing` の日は
    ファイルがあること
  - Q の Plan の sha256 と D の manifest の sha256。Q の `max_tick_id` が Plan と一致すること
- **Dukascopy の価格**: 日 `d` のファイルを `decode_bi5` で読む（`hour_start` は `d` の 00:00 UTC）。時刻 `T` の価格 `P_T` は、
  `T` 以後で `T + quote_window_seconds` より前の最初の tick の `(bid + ask) / 2`。`paper_exit` だけは `T − quote_window_seconds` 以後で
  `T` より前の最後の tick。tick の並びは復号した順（時刻の昇順）のままとし、同じ時刻の tick が並ぶときは、最初の tick では
  先に出たものを、最後の tick では後に出たものを使う（`quotes` の DB の読み方 `ORDER BY event_time, id` と
  `ORDER BY event_time DESC, id DESC` に向きをそろえる）
- **主の取引**: `g = (P_exit − P_entry) / P_entry × 10,000`、`c = (s_entry + s_exit) / 2 × pip_size / P_entry × 10,000`、`n = g − c`（bp）。
  `s` は Q の要約のスプレッドの平均（pips）。価格の計算は Decimal で行い、bp に直した後の集計は float でよい
- **論文の形（副）**: `g_A = (P_switch − P_paper_entry) / P_paper_entry × 10,000 − (P_paper_exit − P_switch) / P_switch × 10,000`、
  `c_A = (s_paper_entry / 2 + s_switch + s_paper_exit / 2) × pip_size / P_paper_entry × 10,000`
- どれかの時刻の価格が無い日（`.missing`、空のファイル、窓の中に tick が無い）は、その取引から除き、日付を report に残す
- **OANDA の確認**: `oanda` の期間の五十日のうち、Q で `entry` と `exit` の気配がある日について
  `n_O = (bid_exit − ask_entry) / mid_entry × 10,000`（`mid = (bid + ask) / 2`）
- **区間（主）**: 暦月（日本時間）ごとに事象をまとめ、事象のある月を単位とする。月の数だけ復元抽出し、純利益の合計 ÷ 事象の数を
  `bootstrap_samples` 回求め、`percentile`（`event_currency_strength_study`）で `1 − one_sided_level` と `one_sided_level` の値を
  下限・上限とする
- **区間（副）**: 期間の暦月を時間順に並べ（事象の無い月も含める）、`secondary_block_months` の長さの循環ブロックを、月の数に
  届くまで抜き出して先頭から月の数だけ使う。統計量は同じく合計 ÷ 事象の数
- 乱数: 区間ごとに `random.Random(f"{bootstrap_seed}:{名前}")` を作り、呼ぶ順番に結果が左右されないようにする
- **判定**: 支持 = 主の期間の五十日の下限 > 0 かつ OANDA の確認の平均 > 0。棄却 = 主の期間の上限 < `reject_threshold_bp`
  または OANDA の確認の上限 < `reject_threshold_bp`。両方なら棄却。どちらでもなければ判定不能
- **統計量の一式**（期間 × 日の組ごと）: 日数、除いた日数、平均の粗利・コスト・純利益、純利益の標準偏差、上下 `trim_fraction` ずつを
  除いた平均、年率のシャープレシオ（平均 ÷ 標準偏差 × √(日数 ÷ 期間の年数)。年数 = 期間の暦日数 ÷ 365.25）、主の区間と副の区間
- 出すもの:
  - 主の期間の五十日（判定）と、OANDA の確認（判定）
  - 副: 主の期間の五十日以外の営業日、主の期間の五十日と五十日以外の差（平均の差）、`post` の五十日、`calibration` の五十日と
    それ以外、コストの倍率ごとの主の期間の五十日の平均の純利益と区間、主の期間の五十日の年ごとの平均の純利益と日数、
    純利益の最も悪い 5 日と最も良い 5 日、論文の形の主の期間の五十日と `calibration` の五十日、OANDA の確認の日の
    Dukascopy の中値の粗利と OANDA の中値の粗利（`(mid_exit − mid_entry) / mid_entry × 10,000`）の差の平均と標準偏差
- `report.json` に、上のすべて、日ごとの行（日付、五十日か、価格、g・c・n、除いた理由）、Plan、provenance（Plan・D の manifest・
  Q の sha256、`git_state()`）を書く。`report.md` に要約の表を書く（見出しは `H10 仲値前の研究`）

## テスト

`tests/unit/test_tokyo_fix_study.py`（DB とネットワークを使わない）と、`quotes` の DB の読み方だけを確かめる
`tests/integration/test_tokyo_fix_study.py`（既存の integration テストと同じ形）。架空の値だけを使い、Plan と小さなデータを作る関数を
`tests/support.py` に足す。最低限、次を手計算の期待値で確かめる。

- 祝日の CSV: 正しく読む。見出し違い・日付の形式・重複・年の抜けを拒む
- 営業日と五十日: 土日・祝日・12/31・1/2・1/3、休日を前の営業日へ移す、2 月に 30 日が無い、30 日と 31 日が同じ日に移るときに 1 日に
  する、移した結果が前の月・前の期間に入る
- 価格の取り方: 窓の中の最初の tick、窓の端（`T + 60` 秒ちょうどは入らない）、`paper_exit` の最後の tick、tick が無い日を除く
- 主の取引の g・c・n と、論文の形、`n_O` の値
- スプレッドの要約: 五十日だけの平均、気配の無い日を数えない
- 区間: 同じ種で同じ結果、月の単位、事象の無い月の扱い（主では単位にしない、副では含める）、呼ぶ順番に依存しない
- 判定の 4 通り（支持・棄却・判定不能・両方なら棄却）
- `fetch`: 取得した内容をそのまま保存し manifest の sha256 が一致する、404 で `.missing`、再開で既にあるファイルを取り直さない、
  一時的な失敗の後に取り直す、待ちが上限を超えたら失敗して取得済みの分が残る、manifest があれば失敗する、復号できない内容を拒む
- `quotes`（integration）: `source` と `max_tick_id` で絞る、窓の最初と最後、DST の前後で `event_time` の軸に正しく写す
- `measure`: いずれかのハッシュが合わないとき、Q の `max_tick_id` が Plan と違うときに、出力先を作らずに失敗する。出力先の上書きを拒む。
  小さなデータで report の判定と主な値が期待どおりになる

## 完了条件

- `ruff check .` が通る
- `pytest tests/unit` が通る。integration は `TRADING_DB_DSN` を指定せずに unit だけを流す（DB を使うテストは Claude が使い捨ての DB で流す）
- 各サブコマンドの `--help` が動く
- 実データの取得・ネットワークへの接続・DB への接続はしない
- コミットはしない。変更ファイル、実行した検証とその結果、ノートの定義から外れた点（あれば）を報告する

## 未確定事項

なし。ノートの定義で決まらない点が出たら、推測で埋めずに報告する。
