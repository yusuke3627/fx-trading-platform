# H7 イベントスタディの実装計画

- 関連: Issue #157 の候補 3
- 規範: [設計ノート](../docs/research/2026-09-24-h7-event-currency-strength-design.md)。**定義・統計量・判定規則はノートが正本**。
  この計画は、ノートに書いていない実装上の決めごとだけを足す。ノートと食い違ったら実装を止めて報告する
- 作業範囲: 研究ツール 1 本と、そのテスト。実データでの測定、事前登録、VPS の操作はこの計画に含めない

## 読むもの

- `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- 手本: `src/trading/backtest/feature_screen.py`（pydantic の Plan、入力ファイルの sha256、`report.json` と `report.md`、
  出力先を上書きしない `mkdir(exist_ok=False)`）、`src/trading/backtest/run.py` の `git_state()`（そのまま使う）
- 時刻の変換: `src/trading/data/market/dukascopy.py` の `known_to_broker_label`（実 UTC → broker ラベル）
- `market_ticks` の列: `id, symbol, bid, ask, event_time, received_at, source, ingestion_run`。`event_time` は broker ラベル
  （New York + 7 時間を UTC として保存した値）
- `macro_observations` の列: `series, observation_period, value, known_at, ...`

## 作るもの

`src/trading/backtest/event_currency_strength_study.py`。`python -m trading.backtest.event_currency_strength_study <サブコマンド>` で動く。
DB を読むのは `events` と `export` だけで、`spreads` と `measure` は書き出したファイルだけを読む（テストと再現のため）。

### Plan（JSON、pydantic の frozen モデル、未知のキーは拒否）

ノートの値をすべて Plan に持たせ、コードに直書きしない。最低限、次を含める。

- `study_version`、`symbols`（`usdjpy`・`eurusd` の銘柄名）、`usdjpy_pip_size`（Decimal）
- `series`（4 系列名）、`release_time`（`08:30`）、`release_timezone`（`America/New_York`）
- `broker_server_ahead_of_ny_hours`（7）
- `stages`: `explore` と `confirm` のそれぞれに、イベント日の範囲（両端を含む日付）
- `excluded_dates`: すでに見ている窓として除く日付と理由の一覧（イベントにもプラセボ候補にも適用）
- 時間の定数: 起点前 60 秒、発表後 15 分、終点 120 分、quote の古さの上限 60 秒、約定の遅延の上限 60 秒、
  欠測を見る範囲（t0−60 分 〜 t0+120 分を 1 時間ずつ 3 つ）、プラセボの候補（−7 日、+7 日の順）、
  取り込み・書き出しの窓（t0−60 分 〜 t0+135 分）
- `bootstrap_samples`（10000）、`bootstrap_seed`、`one_sided_level`（0.95）、`explore_gate`（0.10）
- `confirm_extra_cost_pips`（確認の損益に上乗せするコスト。Decimal、0 以上）

### サブコマンド

1. **`events --plan P --output events.json`**: 研究用 DB（`TRADING_DB_DSN`）の `macro_observations` だけを読み、
   事前登録に載せる一覧を作る。相場のデータは読まない。
   - イベント: 4 系列について、系列・観測期間ごとの最初の `known_at` を取り、ET の現地時刻が 08:30 のものを残す。
     同じ `known_at` は 1 件にまとめ、どの系列かを残す。段階の日付範囲と `excluded_dates` で絞る（除いた理由も残す）
   - プラセボの候補: 各イベント日の −7 日と +7 日の 08:30 ET。候補日がいずれかのイベント日（**段階を問わず、全期間の
     イベント日**）か `excluded_dates` に当たるか、段階の日付範囲の外なら、除外の理由を付けて残す
   - 窓の一覧: 段階ごとに、イベント日と使える候補日（前後とも）の窓 `[t0−60 分, t0+135 分)` を実 UTC で出す。
     VPS の Dukascopy 取り込み（`--since/--until` は実 UTC）にそのまま渡せる形にする
   - 出力 JSON は決定的な並びにし、Plan の sha256 を含める
2. **`export --plan P --events events.json --stage S --output ticks.csv.gz`**: 段階 S の窓（イベント日と候補日）について、
   2 銘柄の tick を研究用 DB から書き出す。
   - 最初に `max(id)` を読んで天井とし、全窓で `id <= 天井` に絞る（途中で増えた行を混ぜない）
   - 窓の実 UTC を `known_to_broker_label` で broker ラベルに直し、`symbol = %s AND event_time >= %s AND event_time < %s`
     の範囲照会で読む（`market_ticks_quote_key` の索引を使う。テーブル全体を読む照会は書かない）
   - 並びは `(symbol, event_time, id)`。列は `symbol,event_time,id,bid,ask`。bid/ask は DB の文字列表現のまま
   - 別ファイルの manifest（JSON）に、ファイルの sha256、天井の id、銘柄ごとの行数、events.json の sha256 を書く
3. **`spreads --plan P --events events.json --ticks ticks.csv.gz --stage S --output spreads.json`**: 損益の感応度を
   決めるためのコストの測定だけを行う。各イベント日の t0+15 分と t0+120 分で、約定に使う quote（下記）の
   USDJPY のスプレッド（pips）の分布（件数、平均、中央値、75・90 パーセンタイル）を出す。**リターンと統計量は計算しない**
4. **`measure --plan P --events events.json --ticks ticks.csv.gz --stage S --output-dir D`**: 測定と判定。
   `report.json` と `report.md` を D に書き、D が既にあれば失敗する。report に Plan・events・ticks の sha256 と
   `git_state()` を含める

### 計算の決めごと（ノートの定義の実装）

- **時刻**: t0 は ET の 08:30 を実 UTC にした時刻。tick を探すときは、同じ時刻を `known_to_broker_label` で broker ラベルに直す
- **欠測**: 日ごとに、欠測を見る 3 つの 1 時間の区間のどれかで、どちらかの銘柄の tick が 0 本なら、その日を使わない
- **仲値**: 時刻 T の仲値は、`event_time <= T` の最後の quote（同時刻は id の大きい方）の `(bid+ask)/2`。
  その quote が T より 60 秒を超えて古ければ、その日を使わない（ちょうど 60 秒は使う）
- **量**: pre = t0−60 秒、post = t0+15 分、end = t0+120 分。`r_UJ = ln(仲値UJ(post)/仲値UJ(pre))`、`r_EU` も同様、
  `m = (r_UJ − r_EU)/2`、`x = (r_UJ + r_EU)/2`、`fwd = ln(仲値UJ(end)/仲値UJ(post))`。プラセボの日も同じ式
- **対**: 使えるイベント日ごとに、−7 日の候補が使えればそれ、使えなければ +7 日の候補、どちらも無ければ対にしない。
  判定に使う統計量はすべて**対になったイベントの集合**で計算する。対にならないイベントの `A`・`C` は参考として別に出す
- **偏順位相関**: 3 変数をそれぞれ順位にし（同順位は平均順位）、順位の Pearson 相関から
  `r_xy·z = (r_xy − r_xz·r_yz) / sqrt((1 − r_xz²)(1 − r_yz²))`。`C = r(fwd, m · x)`、`A = r(fwd, m · r_UJ)`。
  分母が 0 なら未定義として扱う
- **ブートストラップ**: 対を単位に復元抽出（`random.Random(bootstrap_seed)`）し、毎回その標本の中で順位を付け直して
  `C`・`A`・プラセボの `C_p`・`A_p` と差 `ΔC = C − C_p`、`ΔA = A − A_p` を計算する。片側 95% 下限は
  標本の 5 パーセンタイル。未定義になった回は除き、その回数を report に出す。未定義が全体の 1% を超えたら、
  確認の判定を「判定不能」にして理由を残す
- **約定できる価格の損益**（対になったイベントだけ）: 建ては post **以後**の最初の USDJPY の quote、決済は end **以後**の
  最初の quote。どちらかが判定時刻から 60 秒以内に無ければ損益の集計から外し、件数を出す。符号 +1 は ask で建てて bid で
  決済（`exit.bid − entry.ask`）、−1 は `entry.bid − exit.ask`、0 は取引しない（件数を出す）。pips は Decimal で計算し、
  `usdjpy_pip_size` で割る。`m` の符号の規則と `r_UJ` の符号の規則のそれぞれで平均と件数、イベントごとの差の平均を出す。
  確認の段階では、1 取引あたり `confirm_extra_cost_pips` を引いた値も並べ、判定にはこちらを使う
- **対照**: `fwd` と `r_UJ` の順位相関（判定には使わない）
- **判定**:
  - 探索: 対になった集合で `A ≥ explore_gate` かつ `C ≥ explore_gate` なら「確認へ進む」、それ以外は「止める」
  - 確認: `A`・`C`・`ΔA`・`ΔC` のどれかの点推定が 0 以下なら「棄却」。そうでなく、4 つの片側下限がすべて 0 を超え、
    かつ `m` の規則の損益（上乗せ後）の平均が 0 を超えれば「支持」。それ以外は「判定不能」
- report には、除外の件数を理由別（範囲外・除外日・欠測・古い quote・約定の quote 無し・対なし）に出す

## テスト

`tests/unit/test_event_currency_strength_study.py`（DB を使わない部分）と、`tests/integration/` に DB を使う
`events` と `export` の小さなテストを 1 本ずつ。架空の値だけを使う。最低限、次を手計算の期待値で確かめる。

- イベント抽出: 最初の公表だけを採る、08:30 以外を捨てる、同時刻の 2 系列を 1 件にする、除外日と範囲
- DST をまたぐ日の t0（夏時間と冬時間）と、broker ラベルへの変換（08:30 ET がラベル 15:30 になる）
- プラセボの候補の除外（他段階のイベント日も含む）と、−7 → +7 → なし の選択
- 仲値の「以前の最後」と古さの境界（60 秒ちょうどは使い、超えたら使わない）、同時刻の id の順
- 欠測の 3 区間
- 偏順位相関の値（同順位を含む）と、分母 0 の扱い
- 約定の quote が「以後の最初」になること（直前の quote に遡らない）と、60 秒の上限、買いと売りの損益の符号
- 判定規則の 3 分岐（プラセボとの差が 0 以下で棄却になる場合を含む）と、探索の関門
- 同じ seed でブートストラップの結果が一致すること
- `export` が天井の id より後の行を含めないこと、`measure` が出力先の上書きを拒むこと

## 完了条件

- `ruff check .` が通る
- `pytest tests/unit/test_event_currency_strength_study.py` と、追加した integration テストが通る。integration は
  全 migration を当てた使い捨ての DB（`TRADING_DB_DSN` をそこへ向ける）で流す。環境の `TRADING_DB_DSN` は
  Mac の収集用 DB なので、そのまま使わない
- 各サブコマンドの `--help` が動く
- コミットはしない。変更ファイル、実行した検証とその結果、ノートの定義から外れた点（あれば）を報告する

## 未確定事項

なし。ノートの定義で決まらない点が出たら、推測で埋めずに報告する。
