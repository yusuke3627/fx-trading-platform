# 保存済み観測から執行品質を測る

Issue [#203](https://github.com/yusuke3627/fx-trading-platform/issues/203) の調査から、遅延、約定後の価格変化、未確定注文の滞留と新規注文停止の3項目を測る研究用 CLI を追加した。入力は保存済み観測を正規化した JSON で、出力は JSON と Markdown。合成 fixture で一連の処理を確認した段階であり、実測によるコスト較正や収益性の検証は未実施である。

## 実行する

```bash
.venv/bin/python -m trading.backtest.execution_quality_study \
  --input tests/fixtures/execution_quality/synthetic.json \
  --output-dir /tmp/execution-quality-example
```

出力先は未作成のディレクトリを指定する。`report.json` と `report.md` を生成し、JSON に入力ファイルの SHA-256 を記録する。DB、Broker、LLM への接続はなく、既存の注文制御やコスト設定も更新しない。実装は [`execution_quality_study.py`](../../src/trading/backtest/execution_quality_study.py)。

終了コードは `0` が契約に沿った集計、`1` が注文の不整合を含む集計、`2` が入力契約違反または読み書き失敗。欠測はレポートへ残すため、`0` は計測完了や実運用への昇格を意味しない。

## 入力契約

正本は `StudyInput` と各観測モデル、実例は [`synthetic.json`](../../tests/fixtures/execution_quality/synthetic.json)。価格・数量・時間差の計算には `Decimal` を使う。数量は `InstrumentSpec` と同じ基軸通貨単位で、MT5 の lot 数をそのまま入力しない。

| 項目 | 内容 |
|---|---|
| `schema_version` | `execution_quality_v1` |
| `population_description` / `orders_complete` | 保存元、抽出条件、観測窓内に作成された全注文を含むか。拒否・期限切れ・未約定を除外しない。完全性は入力者の宣言であり、ツールは証明しない |
| `window_start` / `window_end` | 観測の開始と打切り時刻。注文・状態履歴・fill の正規化時刻・停止記録は窓内。開始前の未決注文を混ぜず、それを含む窓へ広げる |
| `horizons_seconds` / `quote_max_age_seconds` | 結果を見る前に定める fill 基準の評価期間と quote の最大経過秒。horizon は正の値、マイクロ秒精度 |
| `instruments` | 既存 `InstrumentSpec`。symbol、通貨、pip size を含め、異なる通貨の値を合計しない |
| `orders` | 注文 ID、BUY/SELL、数量、最終状態、作成・入力受信・判断・送信・応答の各時刻。欠ける任意時刻は `null` |
| `states` / `history_complete` | 観測順の全状態遷移と時刻。完全な場合は CREATED から始め、既存 OMS の許可遷移に従う。最終状態だけから過去の UNKNOWN を推測しない |
| `fills` / `fills_complete` | 観測終了までに判明した全 fill。ID、数量、価格、原 `broker_time`、`received_at`、正規化した `executed_at`。同じ fill を二重登録しない |
| `quotes` | symbol、`observed_at`、bid/ask。同じ symbol・由来・受信時刻の重複は入力エラー。観測窓より前のウォームアップ quote は鮮度内なら使える |
| `blocked_entries` / `blocked_entries_complete` | Risk が OPEN/INCREASE を止めた試行を一意なイベント ID で保存。原因注文 ID は任意で複数指定できる。ログ不足時の総数は `null` |

正規化時刻 `Stamp` はタイムゾーン付き `at` と `basis` の組。`basis` は実時計で観測した `observed_utc`、シミュレーターが生成した `simulated`、後から復元した `reconstructed` のいずれか。UTC 表記であるだけでは時計同期や復元精度を保証しない。保存元と変換根拠は母集団の説明に残す。欠測を便宜上の時刻で埋めない。

観測窓の `at` は、由来を問わず正規化済み UTC 値を含める共通の抽出範囲である。範囲外の値は対象外の入力として不整合にする。窓の `basis` は打切り時計の由来を示し、その時計と由来の異なる未完了区間の滞留秒数は `mixed_basis` とする。抽出範囲の判定と、状態間の時計順序・時間差の検証を分けている。

異なる由来の時刻を引き算せず、状態時刻の順序も同じ由来の間でだけ検証し、集計分布を分ける。間に別由来の状態があっても、同じ由来の前回状態より時刻が逆転すれば不整合とする。raw `broker_time` は比較に使わない。独立に保存した約定時刻、または時計の対応を検証した変換結果だけを `executed_at` に入れる。後者は `reconstructed` とし、同じ基準の quote がなければ markout も欠測にする。

## 計測規約

### 1. 遅延と全注文に対する計測可能率

入力受信→判断、判断→送信、送信→応答、送信→最初の約定、送信→最初の fill 受信を秒で出す。「最初」を特定するには全 fill と対象時刻が必要。約定時刻がなくても受信遅延を別に表示するが、約定遅延の代用にはしない。

全注文数、計測件数、比率、欠測理由、由来別 p50/p95/max を残す。分位点はソートした標本上の線形補間。拒否や未約定も全注文数に含め、約定だけから小さな遅延を主張しない。不整合注文は母数と最終状態の内訳には残し、指標の標本から除外する。

### 2. fill 基準の markout と判断時価格からの滑り

評価時点は `executed_at + horizon`。symbol・由来ごとの時刻順索引を1回作り、その時点以前に届いた最新 quote を二分探索で選ぶ。最大経過秒を超えたら欠測とする。未来に届く quote を評価期限へ遡って採用しない。評価時点が観測窓の終了を超えた場合は `right_censored`。

- BUY は `(評価時 bid − fill 価格) / pip_size`、SELL は `(fill 価格 − 評価時 ask) / pip_size`。有利な変化が正。mid 基準も個々の fill に併記する。
- 判断時からの滑りは BUY が `(fill 価格 − 判断時 ask) / pip_size`、SELL が `(判断時 bid − fill 価格) / pip_size`。こちらは不利な滑りが正。fill の時刻（約定時刻がなければ受信時刻）が判断時刻と同じ由来で、判断時刻以後の場合だけ計測する。quote が欠ける場合も時刻の逆転を先に判定し、逆転時は滑りだけを `invalid_chronology` の欠測にする。
- 複数 fill は実際の fill 数量で加重する。quote 通貨の価格差×数量も個別に残すが、手数料・carry を含む実現損益ではない。

symbol・horizon・由来別に、全注文に対する計測注文数と、既知 fill に対する計測 fill 数を出す。計測注文は、その条件で1件以上の fill を評価できた注文。全 fill が評価できたことを意味しない。fill 履歴の不完全な注文数も併記する。別由来の標本は `other_basis` とし、同じ分布へ混ぜない。

判断時の滑りも、計測できた fill の実数量で加重し、欠測を0として加重しない。JSON には注文内と symbol ごとに由来別の数量加重値、計測可能率、欠測理由を出し、Markdown には symbol ごとの表を表示する。約定がない注文は全注文の母数に残る。

### 3. UNKNOWN・部分約定の滞留と新規リスク停止

完全な状態履歴と fill 受信履歴がある注文について、UNKNOWN / PARTIAL_FILL へ入ってから次の状態観測までを区間にする。不完全な状態履歴の末尾が現在状態より古い場合や区間の由来が混在する場合は、この区間を欠測とし、独立して計測できる遅延・markout は保持する。PARTIAL_FILL は後続 fill・取消・UNKNOWN を許すため、それ自体を終端とは扱わない。broker の残数量終了を示す保存済み証拠があるときだけ、その状態観測に `terminal_evidence` を付ける。

次状態がない区間は `window_end` で右打切りにし、秒数・数量×秒は観測終了までの下限とする。数量×秒は「要求数量 − その時点までに受信した fill 数量」の時間積分。broker 上の未約定数量、既に約定したポジションのリスク、証拠金、実際の拘束資金を表す値ではない。受信時刻がない fill や由来混在がある区間は計算しない。

新規リスク停止は `NO_UNKNOWN_ORDERS` / `ACCOUNT_RECONCILED` / `NO_POSITION_MISMATCH` / `NO_UNTRACKED_FILL` の保存済み失敗記録を数える。UNKNOWN があるという理由だけで停止回数を推定しない。複数の原因注文がある試行も1件とし、注文された symbol に計上する。Exit の停止をこの指標へ混ぜない。

## 既存実装から取得できるもの・不足するもの

| 保存元・既存型 | 対応と不足 |
|---|---|
| [`ExecutionCommand`](../../src/trading/domain/order.py) / `execution_commands` | command_id→order_id、symbol、side、数量、最終 state、created_at は利用可能。broker_request_started_at は実際の呼出し境界を確認した上で sent_at の候補。claimed_at / submitting_at だけを約定・応答時刻にしない。応答時刻と旧行の全状態遷移履歴は不足 |
| [`Fill`](../../src/trading/domain/fill.py) / 保存済み fill | execution_command_id で注文に対応付ける。quantity / price / broker_time / received_at を転記可能。received_at と broker_time は別時計。比較可能な executed_at は既存型から保証できない |
| [`Tick`](../../src/trading/domain/market.py) / 保存済み tick | bid / ask とローカル受信を表す known_time を quote に対応付ける。broker の event_time を受信時刻として扱わない |
| [`state_machine.transition`](../../src/trading/oms/state_machine.py) | #212以後は注入`now`と遷移番号をcommandへ保持し、Postgres保存時に観測を追記する。保存前の複数遷移、旧行、欠測時刻を完全な履歴へ補完しない |
| [`RiskEngine`](../../src/trading/risk/engine.py) | 新規リスクだけを止める既存チェックを使用する。停止した試行 ID・時刻・完全なログがない場合は blocked_entries_complete=false |
| [`FillRecord`](../../src/trading/backtest/engine.py) / 既存 trades.json・CSV | command ID と状態・段階時刻が揃わないため、このファイルだけでは本契約を復元できない。下記exportも対象としない |

実測入力を作る際は、注文抽出件数と保存元件数を照合し、結合できない fill や重複、保存時計の同期状況を確認する。足りない情報を `null` または完全性 false として残した入力でも報告できる。study CLI自体は収集・DB接続・設定変更を行わない。#212のexportと状態観測は次節の範囲を追加する。

## 保存DBからのexport（#212）

[`execution_quality_export.py`](../../src/trading/backtest/execution_quality_export.py)は、全migration適用済みのDBを`REPEATABLE READ READ ONLY`の同一snapshotで読み、`input.json`と抽出根拠の`export.json`を生成する。接続先は`--dsn-env`で明示した環境変数だけを使う。引き継いだ`TRADING_DB_DSN`を暗黙に使わず、出力へDSNや口座番号を記録しない。

```bash
.venv/bin/python -m trading.backtest.execution_quality_export \
  --dsn-env QUALITY_READ_DSN \
  --start 2026-09-20T00:00:00Z --end 2026-09-21T00:00:00Z \
  --time-basis observed_utc --provenance '保存元と時計の対応を確認した根拠を記す' \
  --instruments /path/to/saved-instruments.json \
  --horizon-seconds 1 --horizon-seconds 5 --quote-max-age-seconds 2 \
  --output-dir /path/to/new-export
.venv/bin/python -m trading.backtest.execution_quality_study \
  --input /path/to/new-export/input.json --output-dir /path/to/new-report
```

`--instruments`は抽出する注文に対応する、事前保存した`InstrumentSpec`のJSON配列。Brokerへ取得しに行かず、pip sizeや数量単位も推測しない。実測・合成・復元の時計が混在する保存元を単一の`--time-basis`で出してはならない。由来の指定は利用者の申告であり、ツールが同期や変換精度を証明するものではない。

抽出母集団は`created_at`が開始・終了の両端を含む全command行で、状態やfillの有無で絞らない。口座別の抽出は行わない。`orders_complete`はこの保存行集合の完全性であり、broker上の全注文、保存前の失敗、削除済み行まで含むという意味ではない。期間内にcommandへ結合できないCOMMAND-origin fillがあれば件数を残し、完全性をfalseにする。各種類の行数が`--max-rows`（既定100,000）を超えた場合は切り捨てず中止する。

完全な状態観測があれば、DB保存時計と状態時計の前後関係によらず、状態時計で終了時点の状態と要求数量を復元する。不完全な履歴で終了後の状態変化（`observed_utc`では保存時刻も）が見える場合は現在状態を過去へ持ち込まず中止する。その場合は必要な期間の観測を取得するか、比較対象の時計で終了を広げる。抽出根拠には件数、最終状態の内訳、完全な状態履歴の件数、指定した由来、入力・InstrumentSpecファイルのSHA-256を残す。

`simulated`・`reconstructed`ではDB保存時計を状態時計の根拠に使わない。履歴が不完全でも、最新観測のrevision・状態・数量・状態時刻が現在行と一致し、その状態時刻が終了以前なら現在状態を採用できる。状態時刻のない旧行などは窓を広げるだけでは根拠が増えないため中止し、状態観測の追加を求める。`observed_utc`の旧行については、明示された同一時計の前提でDB更新時刻が終了以前の場合だけ現在行を採用する。

Signalの判断時刻がcommandの抽出窓外にある場合は、その判断時刻だけを欠測とし、窓内の注文・fill・状態を保持する。未記録との区別のため、`export.json`の`decision_timestamps_outside_window`へ件数を残す。

### 価格と時刻の採用条件

既存`market_ticks`はMT5のpollと履歴backfillが同じ形式で保存される。`received_at`は受信時刻であり、その価格が当時の現行quoteだったことを保証しない。このためquoteは既定で欠測とする。保存区間を確認して採用するときだけ、`--quote-provenance '採用できると判断した根拠'`を追加する。申告文も証明ではなく、その限界を`export.json`へ残す。

採用時は`received_at`を使い、開始前の鮮度幅から終了までを読む。同一symbol・受信時刻・bid/askの同値行だけをまとめ、同時刻に異なる価格が残れば入力契約違反として中止する。brokerのevent_timeで任意に並べ替えず、backfillの受信を市場の発生時刻へ置き換えない。

| 指標・入力 | 既存行のexport | 状態観測追加後 |
|---|---|---|
| 保存注文数・最終状態・既知fill数量 | 抽出可能。拒否・期限切れ・未約定を保持 | 同じ |
| 判断時刻 | 紐付いたSignalの`generated_at`。紐付かなければ欠測 | 同じ |
| 判断時の滑り | 上記判断時刻・fill受信・採用根拠付きquoteが揃う分だけ計測 | 同じ |
| 全状態履歴 | 旧行は不完全 | 遷移番号0からの連続性、CREATED時刻、許可遷移、時刻順、現在状態・数量の一致を満たす場合だけ完全 |
| UNKNOWN等の数量×秒 | fill完全性が未証明のため欠測 | 状態履歴が揃ってもfill完全性が未証明なら欠測 |
| 入力受信・実送信・応答・正規化約定時刻 | 欠測 | 本件のjournalでは増えない。実発注runnerや時計対応の観測が別途必要 |
| 約定遅延・markout | executed_at欠測 | 同じ。raw broker_timeを流用しない |
| 全fill・新規リスク停止の完全性 | false | 同じ。Shadowの拒否記録を実執行の停止件数へ混ぜない |

### 状態観測の保存と導入

`0011_execution_state_observations.sql`はcommandへ`state_revision`と`state_changed_at`を加え、`execution_state_observations`を作る。`transition`の注入Clock、Postgresのclaim時刻を保持し、insert・CAS更新・claimと観測を同じSQLで保存する。CAS失敗・観測保存失敗ではcommandだけが確定することはない。同一revisionの送信開始マーカー保存等は新しい状態遷移として数えない。取引許可やUNKNOWNの再送条件は変更しない。

遷移を2回行ってから1回保存した場合は番号が欠ける。旧行の履歴、途中状態で初めて保存した行、時刻なしの状態はbackfillしない。`recorded_at`はDB保存時刻で、欠けた遷移時刻・送信時刻の代用にしない。アプリ更新より先にmigrationを適用する。既存tickへ受信時刻indexを作るため、大きな保存表ではindex作成中の書込ロックと所要時間を考慮して適用する。本作業では運用DBへ適用していない。

現在は実発注runnerがこの保存経路へ接続されていないため、追加後の実測履歴はまだ得られていない。専用テストDBの架空注文でexportから既存study CLIまでを確認することと、実測データが揃って較正を完了することは別である。

既存 failure テストとの対応は、[`test_unknown_recovery.py`](../../tests/failure/test_unknown_recovery.py) の応答前クラッシュ、broker 注文が生きている部分約定、履歴照合による解決である。CLI はそれらの保存済み結果を読む立場であり、UNKNOWN 再送禁止や Reconciliation の解決条件は変更しない。期限切れは全注文の母数と EXPIRED 件数に残す。

## 合成入力の確認と実測へ進む条件

fixture の6注文は、分割約定 BUY、SELL、期限切れ、拒否、未解決 UNKNOWN、未終端 PARTIAL_FILL。2 symbol、3 horizon、1件の新規リスク停止を含む。例えば分割 BUY の最初の約定遅延は1秒、最初の受信遅延は2秒。UNKNOWN の未解決区間は8秒・8,000 USD数量×秒の下限で、実際の拘束資金とは異なる。

[`test_execution_quality_study.py`](../../tests/unit/test_execution_quality_study.py) で売買符号、部分 fill の数量加重、quote 鮮度境界、未来 quote 不採用、約定時計の欠測、由来の分離、部分約定の終端証拠、不整合・拒否・期限切れの母数保持、CLI の JSON / Markdown 生成を確認する。

実測研究では比較前に、母集団、horizon、鮮度、最低標本数と計測可能率、許容する欠測率を固定する。対照は同じ保存済み注文を既存 backtest のコスト仮定で評価した結果とし、実測由来の遅延分布・markout・未確定滞留を照合する。現時点では比較対象となる実測データは未投入で、較正は完了していない。注文抽出の完全性、時計対応、状態履歴が確認できない場合や、事前の欠測上限を超えた場合は較正判断を中止し、観測不足の報告にとどめる。

## 調査根拠と採用範囲

[jev-trader の固定版](https://github.com/jarrodwatts/jev-trader/tree/b587759e459ea049590102e54a0b07800864cdc3) は mock / dry-run、実モデル推論、実注文を区別する必要があり、合成 fill の交差判定だけでは maker の queue、取消遅延、逆選択を検証できない。このため本変更では、遅延を1つの平均値に集約せず、約定後の価格差と未確定区間を独立に扱う。

[JPYC 裁定の筆者記録](https://note.com/threef/n/nc19562011d8b) は承認・送金待ち、在庫、slippage / MEV、資金拘束の調査材料であり、申告利益を検証済み成績として採用していない。[Jev の紹介投稿](https://x.com/RohOnChain/status/2101344481400508459) を根拠に LLM 確率から直接 Kelly 数量を作る機能も追加しない。市場固有の校正・費用・アクセス条件の検証と、Portfolio → Risk → OMS → Execution の責務境界が引き続き前提となる。
