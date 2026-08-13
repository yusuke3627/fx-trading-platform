# Project Structure Convention

Repository 正式名称: `fx-trading-platform`。初期取引対象は USD/JPY のみだが、
将来の銘柄・Execution Provider 追加のため名称・構造を特定通貨ペアへ固定しない。

## Strategy の分類原則

Strategy は運用時間軸で分類する: `strategy/{scalp,intraday,swing}/`。

- 分類は**使用する単一 Timeframe を意味しない**。Intraday が 1h(regime) /
  15m(setup) / 5m(entry) を同時に使うことを正式に許可する
- Timeframe 単位のファイル分割（`1m.py`, `5m.py`…）は禁止
- 分割単位は **Trading Strategy そのもの**。1ファイル = 1 Strategy Definition
  = 1 canonical `strategy_id`

## Multi-Timeframe

Timeframe はコード構造ではなく Strategy Configuration（YAML の
`strategies.<id>.timeframes`）で管理し、コードは
`context.config.timeframes.<role>` を参照する。
`Intraday = 5分足` のような固定対応は持たない。

## Indicator Layer

共通 Indicator は `src/trading/indicators/` に一元化し、Strategy は
`context.indicators.*` で利用する。Strategy ごとの Indicator 重複実装は禁止
（Backtest・Live 間 / Strategy 間の計算乖離を防ぐ）。Strategy 固有変換は共通
Indicator の出力への Feature 変換として行う。

## Strategy Context

Strategy へ渡すのは `StrategyContext`
(clock / market / indicators / features / regime / portfolio / config) のみ。
MT5 Client・Broker Credential・ExecutionAdapter・OMS write interface・
Raw DB connection を渡すことは禁止（Strategy から Broker へ直接発注できない
Invariant の構造的強制）。

## Strategy Runner

初期フェーズで scalp/intraday/swing を別プロセスにしない。1つの Trading
Application 内で全 Strategy を実行し、Execution は必ず
Portfolio → Risk → OMS → Execution を通る。

## Strategy Independence

同時刻に Swing SHORT / Intraday SHORT / Scalp LONG が成立してよい（矛盾では
ない）。最終 Exposure は Portfolio Manager が集約し Risk Engine が制限する。

## Indicator / Feature / Regime / Strategy の責務

- **Indicator**: 市場価格から決定論的に計算（ATR, EMA, VWAP, …）
- **Feature**: Strategy 判断へ直接使える入力（distance_from_vwap, intervention_risk, …）
- **Regime**: 複数 Feature から成る市場環境ラベル（USD_POLICY_HAWKISH, …)
- **Strategy**: 上記を組み合わせ「Position を変更したい」という Signal を生成

## Instrument Independence

Strategy ロジックへ `USDJPY` をハードコードしない。対象銘柄は
`strategies.<id>.instruments` 設定。pip size / contract size / volume min /
step / session / stop level は `InstrumentSpec` として Broker / Market Data
Layer から取得する。

## Invariants（Structure）

```
Strategy directories are classified by trading horizon, not candle timeframe.
A strategy may consume multiple timeframes.
One strategy implementation has one canonical strategy_id.
Shared indicators must not be duplicated inside strategies.
Strategy configuration owns timeframe selection.
Strategy implementation must not know the Broker.
Strategy implementation must not determine final execution quantity.
Strategies run inside the same application during modular-monolith stages.
Conflicting strategy directions are allowed.
Portfolio Manager owns cross-strategy exposure aggregation.
Risk Manager owns final risk permission.
OMS owns broker order creation.
Instrument-specific broker specs are never hard-coded into strategies.
```
