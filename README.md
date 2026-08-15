# fx-trading-platform

個人・自己資金の FX アルゴリズム取引プラットフォーム。初期対象は USD/JPY
（OANDA 東京サーバー / MT5 経由執行）、構造は通貨ペア非依存。

- 設計は **v1.3 で凍結**: [docs/SYSTEM_SPEC.md](docs/SYSTEM_SPEC.md)。以後の
  変更は [docs/adr/](docs/adr/) に ADR として追加
- 構成規約: [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)
- 相場スナップショット: [docs/research/](docs/research/)（設計に固定しない）

## 原則

Architecture は完成形、Infrastructure は最小、Data は無料から、Capital は最小から。

- Strategy は Broker を知らない（`StrategyContext` に執行系は存在しない）
- Exit は裸の反対売買にならない（fresh position select + ticket 参照）
- `UNKNOWN` コマンドは再送せず Reconciliation でのみ解決
- Backtest は `known_at <= replay_clock.now()` のデータしか見えない
- 最小ロットが Risk 許容量を超えるなら取引しない（REJECT）

## レイアウト

```
src/trading/
├── domain/        # Event / Position / Intent / Order / Fill / Account / Risk
├── data/          # Collectors (market / macro / cftc / jquants / news) + providers
├── indicators/    # 共通 Indicator 層（ATR / EMA / VWAP / momentum / …）
├── intelligence/  # Feature / Regime / Fundamental / Intervention / LLM境界
├── strategy/      # base + scalp / intraday / swing（時間軸分類・全て RESEARCH_ONLY）
├── portfolio/     # Virtual ledger / pro-rata allocation / manager
├── risk/          # Pre-trade engine / limits(JST・24h・HWM) / event risk / kill switch
├── oms/           # State machine / claim(SKIP LOCKED) / reconciliation / service
├── execution/mt5/ # adapter / mapper / preflight
├── backtest/      # clock / replay(look-ahead 防止) / simulator / costs
└── storage/       # repository protocols / postgres
config/            # base + backtest / demo / shadow / micro_live / production
migrations/        # 0001_initial.sql
tests/             # unit / integration / broker / replay / failure
```

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # Windows 実行ホストでは '.[dev,db,mt5]'
pytest
```

DB スキーマ適用（PostgreSQL、DSN は環境変数 `TRADING_DB_DSN`）:

```bash
psql "$TRADING_DB_DSN" -f migrations/0001_initial.sql
```

## MT5 Demo Preflight（Windows + MT5 ターミナル）

Demo 接続初日に Broker 挙動を機械検証する:

```bash
python -m trading.execution.mt5.preflight --env demo --symbol USDJPY
# 実際に最小サイズの建玉サイクル（OPEN→SL/TP→部分REDUCE→CLOSE→履歴確認）まで:
python -m trading.execution.mt5.preflight --env demo --symbol USDJPY --trade-cycle
```

- Account mode が config と不一致なら `EXECUTION_DISABLED`
- trade cycle は Demo 口座でのみ実行可

## 次のマイルストーン

1. Vertical Slice: Tick → Test Strategy → Intent → Portfolio → Risk → OMS →
   MT5 Demo → Fill → Reconciliation → Audit
2. Production Gate（SYSTEM_SPEC のチェックリスト）を1項目ずつ実測で埋める
3. Backtest → Shadow → 1,000 units Micro Live
