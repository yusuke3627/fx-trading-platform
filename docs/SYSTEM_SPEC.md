# SYSTEM_SPEC — FX Trading Platform

**Status:** FINAL / Architecture Frozen (v1.3)
**運用主体:** 個人・自己資金 / **Broker:** OANDA証券 東京サーバー / **Execution:** MT5
**初期対象:** USD/JPY（構造は通貨ペア非依存）

この文書は凍結済み。仕様変更は本文改訂ではなく `docs/adr/` に ADR を追加して行う。
今後の成果物はコード・Migration・Test・Backtest Result・Incident Record・Research Note。
相場観・研究仮説は `docs/research/` に置き、本文へは固定しない。

## アーキテクチャ

Windows 1台上のモジュラーモノリス。全 Strategy（scalp / intraday / swing）は同一
アプリケーション内の Strategy Runner で動き、Execution は必ず共通経路を通る。

```
Collectors → Point-in-Time Event Store → Fundamental/Regime Engine
→ Strategy Layer (scalp / intraday / swing)
→ Portfolio Manager → Risk Engine → OMS → Execution Gateway → MT5 → OANDA
```

## 確定済みの主要決定

| 論点 | 決定 |
|---|---|
| Broker-side SL/TP 約定 | `PROTECTION_FILL` として正式な Fill 経路 |
| Netting 変換 | Portfolio Manager が Target Net Exposure、OMS は差分のみ発注 |
| Exit | 裸の反対売買禁止。Fresh position select + 状態確認必須 |
| Position Lifecycle | `OPEN / INCREASE / REDUCE / CLOSE` |
| 方向の分離 | `LONG/SHORT`（Position）と `BUY/SELL`（Order）を完全分離 |
| SL | Micro Live 以降の新規建玉は Broker-side SL 必須 |
| 無保護建玉 | `OPEN_UNPROTECTED` = CRITICAL → Repair 失敗なら Close + HALT |
| Netting/Hedging | 起動時に MT5 `ACCOUNT_MARGIN_MODE` から機械判定。config 不一致は `EXECUTION_DISABLED` |
| Command Recovery | `CLAIMED`（副作用なし）と `SUBMITTING`(副作用あり得る) を分離 |
| Exactly Once | Idempotency + State Machine + Reconciliation で近似。再送禁止 |
| Risk Day | JST 日次 + Rolling 24h + HWM Drawdown の3系統 |
| 介入データ | 推定額と公式額は別カラム。検証状態遷移はイベントとして追記 |
| Source Registry | `events` 上の View（二重管理しない） |
| virtual_positions | 履歴Snapshot。現在値 = MAX(as_of)、同時刻タイは挿入順（`seq`）で新しい方 |
| Scalping | `RESEARCH_ONLY` から開始 |
| Event Bus | 初期は使わない |
| LLM | 初期 OFF。使う場合も Structured Event 生成まで。発注権限なし |
| Parameter Threshold | YAML でバージョン管理（本文へ埋め込まない） |

## OMS State Machine

```
CREATED → RISK_APPROVED → READY → CLAIMED → SUBMITTING
        → ACKNOWLEDGED → PARTIAL_FILL → FILLED
異常: REJECTED / CANCELLED / EXPIRED / UNKNOWN
```

- `CLAIMED` + lease 失効 + broker request 未開始 → `READY` へ回収可
- `SUBMITTING` で死んだら `UNKNOWN`。**再送禁止**、Reconciliation でのみ解決
- Claim は PostgreSQL `FOR UPDATE SKIP LOCKED`

## Fill 分類

```
Broker Deal
  → 既知の execution command? YES → COMMAND_FILL
  → 自システムの position?    NO  → UNTRACKED_FILL（CRITICAL・新規リスク停止）
  → 理由が SL/TP/SO?          YES → PROTECTION_FILL
  → その他                          → RECONCILIATION_REQUIRED
```

KPI `untracked_fill = 0` の "tracked" は Command-origin + Protection-origin。

## Risk

初期 Risk Config は「初心者が Micro Live に移る際の上限」であり最適化対象ではない。
`config/base.yaml` 参照（trading_enabled: false、1,000 units、SL 必須、
daily 0.75% / rolling24h 1.00% / HWM 3.00%）。

- 最小ロット > Risk 許容量 → `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` で REJECT
- Kill Switch: `HALT_NEW_ORDER / CLOSE_ONLY / EMERGENCY`。EMERGENCY は
  「無条件全成行決済」ではなく Freeze + Reconcile + Evaluate executable exit
- 連続する中銀イベントは `DUAL_CENTRAL_BANK_CLUSTER` 等の独立 Risk State

## Backtest

- Strategy コードは Replay / Live で共有。`datetime.now()` 直接呼び出し禁止（Clock 注入）
- 可視性: `known_at <= replay_clock.now()`
- Execution Simulator は Bid/Ask/Spread/Latency/Slippage/Partial/Reject/Gap/
  Protection Fill を含み、固定スプレッドを持たない。Stress: spread x2/x5/x10、
  fat-tail slippage、reject burst、stop-through

## Production Gate（Micro Live 最低条件）

```
[ ] Account mode automatically verified     [ ] Broker-side SL verified
[ ] Position OPEN verified                  [ ] Broker-side TP verified
[ ] Partial REDUCE verified                 [ ] PROTECTION_FILL verified
[ ] CLOSE verified                          [ ] Protection/System Exit race tested
[ ] UNKNOWN recovery tested                 [ ] Duplicate command tested
[ ] Claim worker crash tested               [ ] Restart reconciliation tested
[ ] account_snapshots verified              [ ] Daily JST risk tested
[ ] Rolling 24h risk tested                 [ ] HWM drawdown tested
[ ] Tick persistence verified               [ ] Replay deterministic
[ ] Shadow result available
```

## 不変条件（コード上の Invariant Test 対象）

```
A Strategy cannot call Broker.
An LLM cannot call Broker.
Every live position must have broker-side SL.
Every broker position must map to internal ownership.
Every command-origin fill must map to a command.
Every broker-side SL/TP fill must map to a known position.
Unknown external fills must halt new risk.
UNKNOWN commands are never blindly retried.
SUBMITTING commands are never reclaimed as READY.
Exit is never a naked opposite market order.
Hedging exit must reference the target position.
Netting order quantity is broker-target delta, not raw strategy quantity.
A system exit cannot reverse a position that broker protection already closed.
Minimum broker size may never override risk limits.
Backtest cannot access information with known_at in the future.
Strategy code is shared between replay and live.
Every trade must be reproducible from stored input, config and code version.
```

## 完成の定義

Platform Operational Completion = 利益ではなく、以下の成立:
Same input → Same decision / No duplicate position / No unprotected live
position / No unexplained broker position or fill / No blind retry / No
accidental reversal / No look-ahead / Every risk decision auditable / Every
trade attributable / Restart is safe。

その後 Backtest → Out-of-Sample → Walk Forward → Shadow → Micro Live で
初めて Strategy Edge を判定する。有料データ・インフラ増強は Edge が正当化する
Upgrade であり、Architecture の完成条件ではない（Data Upgrade Gate:
期待増分価値 > 2 × データコスト）。
