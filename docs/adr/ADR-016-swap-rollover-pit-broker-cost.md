# ADR-016: Swap/rollover properties は PIT broker cost data（M2B）

**Status:** Accepted (2026-08-27)

## Decision

overnight carry（swap/rollover）を broker cost data として PIT 収集し、
backtest の cost model に接続する（設計書 v2.1 §33.10A / 34.5B / M2B）。

- **収集**: MT5 `symbol_info` の swap 部分（`swap_mode` / `swap_long` /
  `swap_short` / `swap_rollover3days` / 曜日別倍率）を VPS 上の
  `trading.data.swap.collector` が定期観測し、parsed 行（`swap_snapshots`、
  migration 0007）と raw payload（events、`SWAP_SNAPSHOT_RAW`）の両方を
  known_at = 取得時刻で保存する。観測は backfill できない（forward only）
- **曜日倍率の truth source**: broker が per-day フィールド
  （`swap_sunday`..`swap_saturday`）を返せばそれを使う。返さない terminal
  では `swap_rollover3days`（broker 返却の曜日）を 3 倍、週末（市場クローズ
  で rollover が発生しない）を 0、他を 1 とする。**「水曜=triple」は
  ハードコードしない**
- **rollover boundary**: broker server の日付変更。server 壁時計は ADR-014
  と同じ「NY より `broker_server_ahead_of_ny_hours` 時間先行」規約で定義し、
  boundary は NY ローカル (24 − ahead) 時（既定 17:00 NY）として DST を
  America/New_York 経由で追従する
- **backtest 計上**: replay clock が boundary を跨いだ時点で、open position
  ごとに `known_at <= boundary` の最新 snapshot から carry を計上する。
  boundary より後に知った snapshot での値付けは look-ahead であり禁止。
  position が boundary を跨いだのに snapshot が無い場合は carry を 0 と
  偽らず `unpriced_rollovers` として数える（intraday の unexpected hold
  telemetry。設計書 §34.5 の趣旨）
- **金額モデル**: `SWAP_MODE_POINTS`（quote 通貨 points/lot/泊）のみ実装。
  carry = points × 10^-digits × quantity × 倍率（quote 通貨建て、符号は
  broker 値のまま）。他の swap_mode に当たったら黙って誤額を計上せず
  `UnsupportedSwapModeError` で落とす（対応モードの追加は実測とセット）
- **telemetry**: `carry_total` / `unpriced_rollovers` を backtest metrics に
  常時出す。`execution_cost` からは carry を除外し、spread/slippage 起因の
  コストと混ざらないようにする

## 理由

- backtest が swap を無視すると overnight を跨ぐ strategy の期待値を過大
  評価する（設計書 §33.10A）。carry は「観測された broker property」から
  しか正しく出せず、市場慣行の仮定（水曜 triple 等）は broker/symbol 間で
  移ろう — broker 返却値だけを truth source にする
- per-day 倍率フィールドは MT5 terminal のビルドによって公開されない。
  外部 API 境界なので存在チェックで読み、無い場合の fallback も broker
  返却値（`swap_rollover3days`）から導く
- boundary を独立の設定にせず ADR-014 の server-clock 規約を共有すること
  で、「replay の時刻再構成」と「rollover の位置」が別々の仮定を持って
  ずれることを防ぐ

## 受信順 replay と遅着 tick の扱い

受信順 replay（ADR-014）では broker ラベルの古い tick が後から届く。carry
は boundary 到達時に計上するが確定ではなく、ticket 単位の計上記録を持ち、

- rollover より前の broker 時刻の決済が後から届けば按分でリバース
- server midnight より前のラベルで建った遅着建玉には遡ってチャージ
- unpriced と数えた boundary 越えも同様に取り消す
- 訂正が金額を動かしたら同 instant の保存済み snapshot を置き換える

**既知の制約**: 計上と訂正の間の instant に記録済みの経路依存の集計
（high_water_mark / max_drawdown / equity_curve）は遡って再計算しない。
正確な再計算には broker 時間軸での全経路 replay が必要になる。残差は
取り消した carry 1 件分（1 泊分 swap、equity の概ね 0.001〜0.01%）が
上限で、方向は常に保守側 — 正の carry の取り消しで HWM が高止まりして
も halt は早まる側にしか働かず、負の carry の取り消しで max_drawdown が
過大でも報告が悪化する側に倒れる。逆に不完全な部分再計算は HWM の過小
評価（halt が緩む側）を作り得るため採らない。

## 影響

- 既存 backtest（swap snapshot 無し）は carry 0 のままで、metrics に
  `carry_total=0` と `unpriced_rollovers` が加わる（ENGINE_VERSION 0.5.0）
- research replay は `swap_snapshots` テーブルの可視範囲を自動でロードする。
  VPS で collector の常駐タスク（6h 間隔 or `--once` 日次）を登録するまで
  はテーブルが空で、従来と同じ結果になる
