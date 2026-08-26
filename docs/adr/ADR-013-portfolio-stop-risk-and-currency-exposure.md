# ADR-013: portfolio stop-risk 予算と通貨別 net exposure 上限

**Status:** Accepted (2026-08-27)

## Decision

pre-trade risk に portfolio 層の2制約を追加する（設計書 v2.1 §21 Layer 2–3）。

- `portfolio_stop_risk_budget_pct`（既定 0.10% of equity）: 全 open position の
  stop 到達時損失（口座通貨）+ 候補の stop-risk が予算以内であること。
  per-trade の 0.05% は維持し、「4 本 × 0.05% = 0.20% の自動許可」はしない
- `max_currency_net_exposure_pct`（既定 300% of equity）: position を通貨 leg に
  分解し口座通貨 mark した通貨別 net exposure が、候補追加後も上限以内で
  あること。EURUSD SHORT / GBPUSD SHORT / USDJPY LONG が共通の USD LONG に
  合算されて検出される

leg の口座通貨 mark は「そのペア自身の quote 通貨建て価値（base leg = U×P）を
quote → 口座通貨へ換算」で行う。対象 4 ペアの quote は USD / JPY のみなので、
承認済み conversion path だけで全通貨の mark が成立する。既存 book の評価は
`MONITORING`（stale は haircut）、候補 leg は sizing と同じ保守側換算。

triangle 重複（GBPJPY ≒ GBPUSD × USDJPY）は独立の制約にしない: 通貨 leg
分解では直接ペアと合成ペアが同一の通貨 net に写るため（テストで固定）、
Layer 3 の通貨上限が構造的重複をそのまま検出する。pair 名ベースの cluster を
別に持つと分解と二重管理になる（設計書 §22.2 の「pair 名で cluster を決めず
leg 分解から判定する」を制約の実装にも適用）。

direction 条件付き conversion stress は `conversion_stress_adverse_pct`
（既定 0 = 無効）として sizing の換算に接続する。historical conditional
quantile からの推定に置き換わるまでの決定論的 floor。

## Rationale

per-trade / per-symbol の制約だけでは「pair は違うが factor が同じ」リスクの
積み上がりが見えない。通貨 leg への分解は FX の構造（すべての pair は 2 通貨の
比）から導かれる安定した関係で、rolling correlation より前に置く hard
constraint に適する（dynamic correlation は補助として Arbitrator #64 で扱う）。

数値は仮置きで、Monte Carlo / demo 実測で校正する。stop-risk 予算 0.10% は
per-trade 上限 2 本分。通貨 net 上限 300% は leverage 上限ではなく「同方向
factor の積み上げ」を止める水準 — リスクベース sizing の 1 position は
notional で equity の 0.5〜3 倍になり得るため、% は equity 比で大きく見える。

## Consequences

- `PreTradeContext.portfolio_risk`（`PortfolioRiskSnapshot`）を provider が
  供給する。shadow は仮想 book（通常空・stop 情報なし）、backtest は
  simulator の position（stop 込み）から組み立てる
- in-flight（pending）entry の stop-risk は合計に含まれない逐次近似。同時
  signal の裁定と greedy 再評価は Portfolio Arbitrator（#64）が引き取る
- reject code: `PORTFOLIO_RISK_LIMIT` / `CURRENCY_EXPOSURE_LIMIT`
