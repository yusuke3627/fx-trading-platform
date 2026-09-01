# USD perturbation 感度検証（設計書 v2.1 §12.2、#61 最終項目）

実施: 2026-09-01 / ハーネス: `trading.backtest.usd_perturbation_study` /
データ: Mac の PIT DB（研究用）

## 問い

USD は 4 ペア中 3 ペアの leg に現れる。USD の score が ±0.25 / 0.5 / 1.0σ
誤っていたとき、ペアの方向（`PairState.directional_score` の符号）は
どれだけ反転するか。過剰に伝播するなら USD factor cap / confidence haircut
（設計書 §12.2）を入れる。

## 方法

- 日次グリッド（UTC 00:00、366 サンプル、2025-09-01..2026-09-01）で
  `StoredFeatureSource.currency_snapshot(t)`。replay と同じ PIT 経路
  （`known_at <= t`）で読む。価格は使わないので tick の有無は無関係
- σ = 窓内の USD `directional_score` 系列の母標準偏差。摂動は集約 score へ
  `clamp(score + k·σ, [-1, 1])` で与え、`project_pair` で基線と摂動後を
  射影して厳密な符号反転（+→− / −→+）だけを数える。乱数なし（§34.5A、
  単体テストで決定性を固定）
- factor 別 z への摂動は採らない。射影が消費する量は集約後の score だけで、
  伝播の測定としては同じ問いに答え、サービス内部へ到達せずに済む

## 結果

σ(USD directional_score) = **0.228761**（366 日）

| ペア | 両leg日数 | \|score\| 中央値 / p10 | −1.0σ | −0.5σ | −0.25σ | +0.25σ | +0.5σ | +1.0σ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USDJPY | 366 | 0.849 / 0.316 | **7.1%** (26) | 0% | 0% | 0% | 0% | 0.3% (1) |
| GBPUSD | 5 | 0.017 / 0.017 | 20% | 20% | 20% | 80% | 80% | 80% |
| EURUSD | 5 | 0.091 / 0.091 | 0% | 0% | 0% | 0% | 80% | 100% |

## 読み

**USDJPY: USD 1σ 誤差に対して方向は頑健。** flip は基線 |score| が shift
（1σ = 0.229）を下回る日にしか起きず、その日数が 7.1%。±0.5σ 以下で flip
ゼロなのは p10（0.316）が 0.5σ shift（0.114）を上回るため — 余裕の分布が
そのまま感度を説明する。−側と＋側の非対称（26 日 vs 1 日）は、窓内の
USDJPY 基線がほぼ常に正（USD 優位）で、負の USD ショックだけがゼロ交差へ
向かうため。

**GBPUSD / EURUSD: サンプルではなく構造の観察。** 両 leg が揃うのは直近
5 日だけ — GBP/EUR 観測は forward collection で `known_at` = 取得時刻の
ため、収集開始（2026-08 下旬）より過去の日には PIT 上存在しない（正しい
挙動）。leg の factor が薄く（GBP/EUR は rates proxy 中心、ADR-020）基線が
0 近傍に座るので、小さな摂動でも flip する。これは「GBP/EUR ペアの方向感は
現時点で薄い証拠の上にある」という ADR-020 / #57 で既知の事実の再確認で
あり、USD 側の過剰伝播ではない。

## 判断

- **USD factor cap / confidence haircut は現時点で入れない。** フル履歴で
  測れた唯一のペア（USDJPY）で ±1σ の flip が 7.1% に留まり、過剰伝播の
  証拠がない。cap は score の情報を捨てる操作なので、証拠なしに入れない
- **GBP/EUR ペアは PIT 履歴が溜まってから再測定する。** 判定可能になる
  条件: 両 leg の日数が正規化窓相当（数か月）を超えること。#57（VPS 収集の
  多ペア化）と観測蓄積の時間経過で自然に満たされる
- 再測定はコマンド 1 本で再現できる:
  `python -m trading.backtest.usd_perturbation_study --env backtest`

## 繰り延べ（§12.2 の残り 4 指標）

accepted-trade set change / portfolio USD concentration / PnL・DD
sensitivity / arbitration ranking stability は、通貨 state を消費する
strategy・portfolio・arbitrator が存在するまで（M4 / M5）測定対象が無い。
M4 以降で消費者が入った時点で、本ハーネスの摂動列を再利用して測る
（perturbed state を `CurrencyStateStore.replace` 経由で流し込めば、
backtest 全体の摂動比較になる）。

## 制約

- confidence はこの研究では判定に使っていない（#89 の freshness 欠陥が
  未修正のため。ADR-022 の注意書きどおり、confidence を使う判定は #89 後）
- σ は測定窓依存。窓を変えると σ も flip 率も変わる — 報告には必ず σ と
  窓を併記する（ハーネスが両方出力する）
