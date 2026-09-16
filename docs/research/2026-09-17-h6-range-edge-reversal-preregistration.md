# レンジ端の反転（H6）の事前登録

- 日付: 2026-09-17
- 対象: 仮説 H6「横ばいの 1h レンジの端で失敗した抜けは、レンジ中央へ戻る」。intraday 戦略 `range_edge_reversal`（issue #157 の候補 1）
- 判定: **まだ無い。** 本ノートは測定前の事前登録であり、結果・判定・解釈は VPS で run を流した後に別 PR で追記する
- 関連: issue #157（候補 1〜3）、issue #153 / [ADR-037](../adr/ADR-037-backtest-account-level-loss-halts.md)（研究リプレイの口座水準の損失停止）、issue #154（到達範囲）、issue #164（block bootstrap）、[ADR-036](../adr/ADR-036-horizon-exit.md)（時間切れ決済）、[ADR-038](../adr/ADR-038-strategy-take-profit.md)（利確距離）、研究ノート [`2026-09-16-h4-failed-spike-reversal-base-edge.md`](2026-09-16-h4-failed-spike-reversal-base-edge.md)、[`2026-09-10-h5-macro-confirmation-ablation.md`](2026-09-10-h5-macro-confirmation-ablation.md)

## 背景

issue #157 は Meme/壇上の記事の着想を 3 候補に具体化し、候補 1「レンジ端の反転」を最初の検証対象にした。上位足で取引する価格帯を決め、中央では待ち、端に来てから下位足で反転を確認する。損切りの根拠になる価格（反転極値）と、エントリーを検討する価格帯（端から 20%）を分ける。

先行する 2 つの測定は、いずれも決済の構造が成績を決めていた。H5（`post_event_failed_breakout`、[`2026-09-10-h5-macro-confirmation-ablation.md`](2026-09-10-h5-macro-confirmation-ablation.md)）では決済 239 件中 236 件が protection fill で、損切りにも利確にも当たらない建玉が銘柄あたり 1 本の枠を占め続け、25 か月中 13 か月で新規建玉が通らなかった。H4（`failed_spike_reversal`、[`2026-09-16-h4-failed-spike-reversal-base-edge.md`](2026-09-16-h4-failed-spike-reversal-base-edge.md)）は時間切れ決済を入れて期間全体を測れたが、8,798 件の粗損益 +0.164 pips に対して執行コスト 0.979 pips で棄却された。どちらも利確を持たず、出口は損切りか（H4 では）時間切れだった。

H6 では、[ADR-038](../adr/ADR-038-strategy-take-profit.md) で使えるようになった broker 側の利確と、[ADR-036](../adr/ADR-036-horizon-exit.md) の時間切れ決済を最初から入れ、損切り・利確・時間切れの決済内訳と、利確・時間切れ・エントリー帯のそれぞれの寄与を測る。issue #157 が「測定前に確定する項目」として挙げた点（セッションの扱いと DST、移動平均と閾値、「一度割る→戻る」の対象足、quote と RR、保有期限とロールオーバーの優先順位、判定規則）をここで確定する。結果を見てから変えた案は、新しい実験として区別する。

## 仮説

**H6（主仮説）**: 1h の EMA(20) の傾きが小さくレンジ幅が十分な局面で、直近 24 本の 1h 足の高値・安値をレンジとし、5m 足がレンジ端を一度割って終値でレンジ内へ戻った直後に端の側から入り、レンジ中央で利確・反転極値の外側で損切り・60 分で時間切れとすると、約定あたり純損益（執行コスト込み）は正である。

**副仮説（要素の寄与）**: 利確、時間切れ決済、エントリー帯（端から 20%）のそれぞれは、無い場合より約定あたり純損益を改善する。

## 事前登録した売買ルール

戦略 `range_edge_reversal`（`strategy_version` 0.1.0、horizon INTRADAY、status RESEARCH_ONLY、USDJPY、時間足は regime 1h / entry 5m）。数値は `config/base.yaml` の `strategies.range_edge_reversal.parameters` に置き、以下ではその名前で参照する。live の overlay（`shadow` / `micro_live` / `production`）には載せない。

### 1. セッションとレンジ

- セッションは `src/trading/indicators/session.py` の窓をそのまま使う。Tokyo 09:00–18:00 Asia/Tokyo、London 08:00–17:00 Europe/London、New York 08:00–17:00 America/New_York。IANA timezone で判定するので DST は自動で追従し、broker のサーバー時刻には依存しない（ADR-024）。
- レンジは「いずれかのセッションが開始した時点」で確定し、次のセッション開始まで固定する。開いているセッションのうち最も遅く開始したものがレンジの主で、London と New York が重なる時間帯に New York が開始したら New York 開始で作り直し、それ以前のレンジは失効する。どのセッションも開いていない時間帯（New York 終了から Tokyo 開始まで）にレンジは無い。
- 「開始時点」は、そのセッションが開いてから strategy が最初に評価した市場イベントの時点とする。研究リプレイは tick ごとに評価するので、開始から数秒以内である。その時点に見える（`known_at` が評価時刻以下の）直近 `range_lookback_bars`（24）本の 1h 足の最高値・最安値がレンジで、H = 高値 − 安値、中央 = (高値 + 安値) / 2。
- 24 本の最古の足の確定観測時刻（`known_at`）がセッション開始の `range_stale_hours`（72）時間より前なら欠測とみなし、そのセッションは対象外にする。24 本が揃わない場合も対象外。
- レンジ確定後に 1h 足がレンジ外（終値が高値より上、または安値より下）で確定したら、そのレンジでの新規建玉を終える（`range_invalidated`）。次のセッション開始で作り直すまで新規は出さない。終値が端ちょうど、またはヒゲだけが外側の足では失効しない。

### 2. レジーム（横ばい）判定

レンジ確定と同じ時点で判定し、セッション中は固定する。

- 傾き: 1h の EMA(`ema_period` = 20) の現在値と `range_slope_lookback`（6）本前の値の差の絶対値が、ATR(`atr_period` = 14, 1h) × `range_slope_max_atr`（0.5）以下。
- 幅: H が ATR(14, 1h) × `range_width_min_atr`（1.5）以上。
- どちらかを満たさなければそのセッションは対象外。

EMA の定義を固定する。直近 26 本（`ema_period + range_slope_lookback`）の 1h 終値に対し、最初の 20 本の単純平均を初期値として `trading.indicators.ema.ema_series` で計算する。「6 本前の値」はこの初期値に等しい。ATR(14, 1h) は `IndicatorService.atr`（Wilder 法、直近最大 200 本）で、レンジ確定時点の値を使う。

### 3. トリガー

- 買い: 5m 確定足の安値がレンジ下端を割る（breach）。その足を含めて `reentry_max_bars`（6）本以内に 5m 終値がレンジ内（下端以上・上端以下）へ戻ったら成立。成立足の直前から遡って「終値 < 下端」が連続する本数を run とし、run = 0 なら成立足自身の安値が下端を割っていること（breach と戻りが同じ足）、run ≥ 1 なら run 本前が breach 足である。run + 1 が 6 を超える戻り（breach から 7 本目以降）は不成立。反転極値 = breach 足から成立足までの最安値。
- 「6 本」は観測した確定足の本数で、暦の 30 分は名目である（`BarBuilder` は tick の無い区間の空足を補完しない）。breach 足はレンジ確定後に確定した足に限る。
- 成立時の約定予定価格は最新 quote で、買いは ask、売りは bid。買いは ask が 下端 + `entry_band_fraction`（0.2）× H 以下であること。満たさなければ見送る。
- 売りは上下対称（高値が上端を上抜けて終値が戻る。bid が 上端 − 0.2 × H 以上）。同じ足で両方が成立し得る形では買いを先に評価する。
- スプレッド gate は scalp と同じ `SpreadGate.from_params`（`spread_gate.max_spread_to_atr` 0.5 × ATR(14, 5m)、USDJPY の `absolute_max_spread_pips` 1.5）。通らなければ見送る。
- 同じレンジ・同じ方向は 1 回だけ。「1 回」は signal 生成 1 回で、`setup_id` にレンジの識別子（セッションと開始時刻）を入れ、方向ごとに 1 つ記憶する。帯や RR で見送った試行は記憶を消費せず、同じレンジ内で改めて breach → 戻りが起きれば再評価する。Risk や執行で拒否された signal は再試行しない（既存戦略と同じ）。

### 4. 損切り・利確・RR

- 買いの損切り = 反転極値 − ATR(14, 5m) × `stop_buffer_atr`（0.25）。売りは 反転極値 + 同じ幅。約定予定価格が損切りより不利な側にあれば（買いで ask ≤ 損切り）見送る。
- 利確 = レンジ中央（セッション中は固定）。`take_profit_enabled` が true のとき、約定予定価格から中央までの距離を 0.1 pip に丸めて `take_profit_distance_pips` に載せ（ADR-038）、Portfolio が処理時の entry 価格から broker 側の利確価格へ戻す。丸めは USDJPY の 1 point 以内で、Portfolio が signal を保留した場合は処理時 quote の変動分だけ中央からずれ得る。研究ではこの差を許容し、絶対価格の固定は要求しない。
- RR: (中央 − ask) ≥ `min_reward_to_risk`（1.5）× (ask − 損切り) を満たさなければ見送る。売りは対称。判定は丸め前の価格差で行い、`take_profit_enabled` が false でも同じ判定を行う（利確を載せないだけ）。
- conviction は固定 0.5、`expected_edge_r` は既定 1。どちらも仮説の一部ではない。研究では USDJPY の数量上限が最小ロット（1,000 通貨）に等しいので sizing に効かない。

### 5. 出口と保有期限

- 出口の優先順位は (1) broker 側の損切り・利確（`ProtectionSpec`）、(2) 時間切れ決済（`horizon_exit_enabled` true、`expected_horizon_seconds` 3600。ADR-036）、(3) セッション閉鎖中に反対向きの setup が成立したときの決済専用 signal（base の既存挙動、ADR-031）。(3) はこの戦略ではセッション外にレンジが無いため実質的に到達しない。セッション閉鎖そのものを契機にした決済は無い。
- 時間切れは、保有を最初に観測した snapshot から 3600 秒以上経った最初の市場イベントで決済専用 signal を出す。約定は執行 latency（normal で 150ms）と次の tick の後になる。
- ロールオーバー回避: 開いているセッションのうち最も遅く終わるものの終了まで `session_end_buffer_seconds`（3600）未満なら新規建玉を出さない（保有と逆向きの setup も signal にしない）。New York 終了 17:00 が broker の日付変更にあたるので、60 分前の新規停止と 60 分の保有期限の組で rollover を跨がない設計とする。ただし終了ちょうど 60 分前の entry は、終了までの約定完了を保証しない。跨いだ建玉は `unpriced_rollovers` に数えられ、判定規則の保留条件で捕捉する。
- 研究リプレイには再起動が無く、scenario `normal` では執行の確率的拒否も無い。Risk の拒否（建玉上限など）は起こり得る。live で期限内の決済を保証すること（再起動後の建玉、拒否時の再送。ADR-036 の残課題）は本測定の範囲外である。

### 6. 通貨強弱

候補 1 では使わない。feature（`ctx.features`）を読まない。

## 事前登録した判定規則

### データと実行条件

| 項目 | 値 |
|---|---|
| 期間 | 2024-08-01T00:00:00+00:00 〜 2026-08-29T00:00:00+00:00（broker ラベル、終端は排他。土曜 00:00 境界） |
| tick | H4 / H5 と同じ保存 tick 集合（H4 の `dataset_hash=385b6bb1378fafa69096702b8eaae294e797341ed0032fafcb71a69483cc8fe5`、98,806,999 本。2026-01-23 から 2026-04-08 までの 74 日欠損を含む）。run の `manifest.json` の値が違えば、その差を結果に記す |
| 環境 | `--env backtest`（ADR-037 により口座水準の損失停止なし）、symbol USDJPY、scenario normal、seed 42 |
| warmup | 戦略の宣言値（26 本の 1h 足 = 26 時間 × 7/5 + 2 日 ≒ 3.5 日）。期間開始の 2024-08-01T00:00 broker は 2024-07-31 21:00 UTC でどのセッションも開いていないので、最初のレンジは Tokyo 開始で作られる |
| 腕 | 4 本を並列に流す（`feature_dataset_hash` を揃えるため）。A = 全規則 / A−利確 = `take_profit_enabled=false` / A−時間切れ = `horizon_exit_enabled=false` / A−帯 = `entry_band_fraction=1.0` |

腕の違いはパラメータ上書きだけで、コード・データ・期間・seed は共通である。

### 主判定（H6）

腕 A の**約定あたり純損益**（執行コスト込み、carry は未値付け）の CI90（`ablation_compare.arm_summary`、bootstrap `BOOTSTRAP_SAMPLES` 回、seed 42、約定を独立で交換可能な標本とみなす i.i.d. 区間）で決める。上から順に評価する。

| 条件 | 判定 |
|---|---|
| 腕 A の約定が 50 件未満 | 判定不能（標本不足） |
| CI90 下限 > 0 | H6 支持 |
| CI90 上限 < 0 | H6 棄却 |
| それ以外 | 判定不能（差が検出できない） |

issue #164 の block bootstrap（日単位）の区間は参考値として併記するが、判定には使わない。

### 副判定（要素ごと）

A を「有効側」、A−利確 / A−時間切れ / A−帯をそれぞれ「無効側」として、`ablation_compare.judge` の既存規則をそのまま当てる。上から順に評価する。

| 条件 | 判定 |
|---|---|
| 有効側の平均が高く、差の CI90 下限が 0 より大きい | 有効のまま維持（寄与あり） |
| 無効側の約定数が 2 倍以上で、平均が有効側以上 | 無効にする（絞るだけで質が上がらない） |
| どちらかの腕の約定が 10 件未満 | 判定不能（標本不足） |
| それ以外 | 判定不能（差が検出できない） |

無効側が期末に建玉を残して `verify_comparable` が拒否した場合（H4 の時間切れなし腕と同じ形）は、その対を「評価不能」として記録する。

利確と時間切れの対は比較 CLI で流す。帯の対は現在の CLI では流せない。`verify_comparable` が真偽値パラメータの腕（with 側 True / without 側 False）しか受け付けず、A−帯は `entry_band_fraction` が 1.0 だからである。run の前に、CLI が非真偽値の腕（比較対象パラメータの with / without の値を指定する形）を受け付けるようハーネスを広げる作業を別 PR で行い、帯の対も CLI で流すことを推奨する。それが間に合わない場合は、`ablation_compare.load_run` で両腕を読み、CLI の `verify_comparable` が行う検査をすべて手で当ててから `arm_summary` / `difference_interval` / `judge` を seed 42 で直接呼ぶ。検査は次のとおりで、1 つでも満たさなければその対は評価不能とする: `COMPARABLE_FIELDS` の全項目一致、`git_commit` が記録されていること、`resolved_parameters` と `param_overrides` が `entry_band_fraction` 以外で一致、`open_positions_at_end` と `pending_commands_at_end` が 0、`trades.csv` の `entry_at` がすべて期間内、末尾の空白（`period_to` − 最終 `entry_at`）が期間の 10%（`TRAILING_BLACKOUT_MAX_RATIO`）以下。規則は CLI と同一である。

### 保留条件

いずれかの腕で `unpriced_rollovers` > 0 なら、その腕を含む判定を保留する（issue #157 の条件、issue #60）。跨いだ建玉の swap を値付けするまで採否を出さない。

### 報告項目

腕ごとに次を記録する。約定数、純損益、約定あたり期待値と CI90（i.i.d.。block 区間は参考値）、最大ドローダウンと live の高水準ドローダウン停止 3.00% との比較、保有時間、決済内訳（`trades.csv` の `reason` 列で `PROTECTION_CLOSE:STOP_LOSS` / `PROTECTION_CLOSE:TAKE_PROFIT` / `CLOSE` に分ける。`CLOSE` はシステム決済で、run の成果物には signal の reason code が残らないため、時間切れと反転 setup による決済を直接は区別できない。時間切れ決済が有効な腕では、`exit_at − entry_at` が 3600 秒以上の `CLOSE` を時間切れ、未満を反転 setup による決済とみなす近似で分け、近似であることを明記する）、月別の約定分布、到達範囲（`summary.json` の `coverage`）、リスク棄却のコード別内訳。

## 測定手順

VPS で YAML 由来の collector（policy / intervention）を先に流し切り、`fx-macro` だけを止めたうえで（tick / bar / account / shadow のタスクは止めない）、4 腕を並列に流す。長時間の run は RDP を切断すると落ちるため、タスクスケジューラ経由で起動する（H4 と同じ）。

```
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy range_edge_reversal --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --out reports/h6_a
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy range_edge_reversal --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --param take_profit_enabled=false --out reports/h6_no_tp
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy range_edge_reversal --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --param horizon_exit_enabled=false --out reports/h6_no_horizon
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy range_edge_reversal --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --param entry_band_fraction=1.0 --out reports/h6_no_band
python -m trading.backtest.ablation_compare --with reports/h6_a/<run_id> --without reports/h6_no_tp/<run_id> --param take_profit_enabled --seed 42
python -m trading.backtest.ablation_compare --with reports/h6_a/<run_id> --without reports/h6_no_horizon/<run_id> --param horizon_exit_enabled --seed 42
```

`research` は戦略の宣言 warmup ぶんの tick を `--from` の前から読む。頭出しが足りず `ensure_period_covered` が拒否したら、`--warmup-days` で縮めて通さず、保存 tick の範囲を確認する。本番の 2 年より先に 2 週間程度の smoke run で、signal が出ること・決済内訳に利確と時間切れが現れることを確かめてから流す。

## まだ測っていないこと

- run を 1 本も流していない。約定数・損益・決済内訳・到達範囲は未知で、H6 の判定は無い。
- 同じ tick 集合には 74 日の欠損（2026-01-23〜04-08）があり、研究ハーネスは期間途中の欠損を検出しない。H4 と同じく、この 2 か月は評価が走らない。
- CI90 は i.i.d. bootstrap で、同じセッションに固まる約定の自己相関を考慮しない（H4 と同じ限界）。block 区間（issue #164）は参考値にとどめる。
- research seed は 1 組（42）で、執行の感応度は測らない。
- 帯の対（A と A−帯）は現在の比較 CLI では流せない（真偽値パラメータの腕のみ対応）。run 前にハーネスを広げるか、上記の検査を手で当てる。
- run の成果物には signal の reason code が残らないので、システム決済 `CLOSE` のうち時間切れと反転 setup の内訳は保有時間による近似になる。
- 4 腕は約定集合そのものが違い得る。件数差をそのまま「フィルターで落とした取引」の成績として読まない（issue #157 の検証方針）。
- レンジ確定を「開始後に最初に評価した市場イベント」とする解釈、EMA の初期値の定義、「6 本」を観測本数とする解釈、利確を距離で載せる経路の丸め・遅延は、いずれも結果を見る前にここで固定したもので、run 後に変えない。
- live での保証（再起動をまたぐ建玉、拒否時の再送、NY 終了までの約定完了）は本測定の範囲外で、昇格判断とは別に扱う。
