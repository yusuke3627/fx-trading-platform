# FX Multi-Currency System Design v2.1

**対象**: USDJPY / GBPUSD / EURUSD / GBPJPY  
**口座**: OANDA証券 / MT5 / JPY口座 / HEDGING mode  
**既存アーキテクチャ**: Collectors → PIT Event Store → Fundamental / Regime → Strategy → Portfolio → Risk → OMS → MT5  
**基準仕様**: `docs/SYSTEM_SPEC.md` v1.3 は凍結維持。変更は本文改訂ではなく `docs/adr/` に ADR を追加する。  
**位置づけ**: USDJPY専用BOTを、4ペアに対応する「Currency-first / Portfolio-first」なFX運用プラットフォームへ拡張する統合設計書。

**v2.1 review resolution**: v2.0レビューで指摘されたGBP/EURデータ調達、cross-pair event propagation、Scalping RESEARCH_ONLY、swap/carry cost、score normalization、phase order、conditional conversion stress、Money型、staleness API、governanceを反映した。

---

# 1. Executive Summary

本対応の目的は、単純に取引対象を4ペアへ増やすことではない。

最重要目的は、現在の

```text
「USDJPYは上がるか / 下がるか」
```

という Pair-first なシステムを、

```text
「USD / JPY / GBP / EUR の各通貨がどれだけ強いか」
        ↓
「その差として各通貨ペアを評価する」
```

Currency-first なシステムへ進化させることである。

方向性の基本式は以下とする。

```text
PairDirectionalScore(base/quote)
    = CurrencyDirectionalScore(base)
    - CurrencyDirectionalScore(quote)

USDJPY = USD - JPY
GBPUSD = GBP - USD
EURUSD = EUR - USD
GBPJPY = GBP - JPY
```

ただし、Event Risk / Intervention Risk / Liquidity Risk は単純な加減算スコアに混ぜない。
Directional State と Risk/Gating State を分離する。

最終的な本番採用方針は以下とする。

> **システムは4ペア対応にする。採用するペアはデータに決めさせる。**

導入順序は原則として、

```text
USDJPY
  ↓
EURUSD
  ↓
GBPUSD
  ↓
GBPJPY
```

とする。

GBPJPY は `GBPJPY ≒ GBPUSD × USDJPY` という三角関係を持つため、新しい通貨ファクターを追加するものではない。
したがって最後に追加し、単体収益ではなく「ポートフォリオ全体のrisk-adjusted returnへの限界寄与」で採否を決める。

---

# 2. Scope / Non-Goals

## 2.1 Scope

本設計で扱う。

- JPY口座における正しいpip value / loss conversion
- InstrumentSpecのbase / quote currency拡張
- Conversion quoteの鮮度・staleness・fail-close
- USD / JPY / GBP / EUR のcurrency state
- GBP/EUR向け公式Fundamental Collectorとmarket-implied policy-path data procurement
- Pair stateの導出
- Currency-scoped Regime / Event Risk / Intervention Risk
- 1戦略×4ペアの設定方式
- ペア別パラメータ
- spread / volatilityの正規化
- strategy × pair × session
- 通貨別exposure
- structural triangle risk
- correlation cluster
- per-symbol / portfolio position limits
- Portfolio Arbitrator
- 同時シグナルの優先順位
- OANDA MT5 execution capability discovery
- minimum lot と risk allowance
- backtest / walk-forward / OOS / Monte Carlo / shadow / demo / live gate
- ADR
- migration
- failure modes / tests
- 将来の日本株BOTへのアーキテクチャ転用

## 2.2 Non-Goals

本設計では以下を固定しない。

- 実口座残高
- GBPUSD / EURUSD / GBPJPY の実測 filling mode
- 4ペアすべてを本番取引すること
- 利益が出ることを保証する具体的なPF / Sharpe閾値
- broker minimum lotに合わせたrisk appetiteの引き上げ
- FXと株式を無理に同一Instrumentクラスへ統合すること

---

# 3. 壊してはいけない不変条件

以下は `docs/SYSTEM_SPEC.md v1.3` および既存テストの不変条件として維持する。

1. Strategy / LLM 層から Broker・OMS・DB に到達できない。
   - `StrategyContext` は read-only service のみ。

2. Strategy 内で `datetime.now()` を直接呼ばない。
   - `Clock` 注入。

3. LONG / SHORT（Position）と BUY / SELL（Order）を混同しない。

4. Exit は裸の反対売買にしない。
   - fresh position select
   - ticket参照
   - HEDGING modeの意味論を維持

5. Backtest は `known_at <= replay_clock.now()` のデータしか見ない。

6. 最小ロットがリスク許容量を超えたら取引しない。
   - `MINIMUM_BROKER_SIZE_EXCEEDS_RISK`

7. 金額・数量・価格は `Decimal`。
   - indicator計算のみfloat可。

8. 通貨ペア・pip size・時間足をstrategy codeにハードコードしない。

9. Broker固有のfilling mode / volume constraint / server timeをstrategy codeへ持ち込まない。

10. Riskを下げるExit / Closeは、entry用のmarket-data freshness障害を理由に不必要に禁止しない。
    - ただしticket/position整合性等の既存安全条件は維持する。

---

# 4. 既にマルチペア対応済みの部分

以下は作り直さない。

- `StrategyConfig.instruments` は list
- `Strategy._new_setup` は `(symbol, direction)` key でdedupe
- `MarketDataService / InMemoryMarketData` はsymbol keyでticks / bars / InstrumentSpecを保持
- `ReplayEngine` はknown_at順で複数pairのeventを1 timelineへmerge可能
- `PortfolioManager.desired_net_exposure(symbol)` はsymbol単位で集計
- `FeatureStore.get(name, symbol=None)` はsymbol次元 + global fallbackを持つ

本対応はこれらを利用して拡張する。

---

# 5. 主要設計判断

| 論点 | Decision |
|---|---|
| 1戦略×4ペアか | **1つの戦略実装を4ペアへ適用**。pair別cloneは作らない。設定のみpair override可能にする |
| JPY換算責務 | **Portfolio / Riskが共用するAccountCurrencyConversionService** |
| Instrument base/quote | `InstrumentSpec` に `base_currency` / `quote_currency` を追加 |
| GBPUSD/EURUSDのJPY換算 | USDJPY等のfresh market quoteを使う |
| 換算レートstale | **新規・増し玉 fail-close**。既存positionのreduce/exitは許可 |
| Fundamental state | **currency-level primary model** |
| Pair state | directionalは `base - quote`、risk stateは両legのscope合成 |
| Event risk | affected currenciesでscope |
| max_open_positions | **per-symbol と portfolio-global の両方**を持つ |
| 通貨リスク | stop-risk + currency factor exposure + cluster risk の多層制約 |
| 相関 | structural exposureをhard、dynamic correlationは補助 |
| 同時signal | Portfolio Arbitratorが限界効用順に選択 |
| 重複factor | 初期は **strongest signalを優先し残りreject** |
| spread | normalized gateをprimary、pair別absolute pipsをsafety ceiling |
| session | pair / strategy別matrix |
| rollout | **USDJPY → EURUSD → GBPUSD → GBPJPY**。USD汎化検証を優先し、最も流動性の高いmajorを先に使う |
| minimum lot | broker都合でrisk%を上げない |
| 本番採用 | platform-enabledとtrading-enabledを分離 |

---

# 6. Target Architecture

```text
Collectors
   │
   ↓
PIT Event Store
   │
   ├────────────────────────────────┐
   ↓                                ↓
Market / Broker Facts           Intelligence
   │                                │
   │                       Currency State Engine
   │                       USD / JPY / GBP / EUR
   │                                │
   │                       Pair State Projection
   │                                │
   └──────────────┬─────────────────┘
                  ↓
              Strategy
        raw candidate signals
                  │
                  ↓
          Portfolio Arbitrator
       ┌──────────┼───────────┐
       ↓          ↓           ↓
 Currency     Structural   Dynamic
 Exposure     Clusters     Correlation
       │          │           │
       └──────────┼───────────┘
                  ↓
              Risk Engine
       ┌──────────┼───────────┐
       ↓          ↓           ↓
 Account      Event /      Min Lot /
 Currency     Session      Broker Rules
 Conversion
                  │
                  ↓
                 OMS
                  │
                  ↓
              MT5 Adapter
                  │
                  ↓
                OANDA
```

Strategyはbroker / OMS / DBを知らない。
Broker factsはMarketData / InstrumentSpec / read-only capability serviceを通じて提供される。

---

# 7. Instrument Domain Model

## 7.1 InstrumentSpecへbase / quoteを追加

別テーブルより `InstrumentSpec` への追加を推奨する。

理由:

- FX instrumentとしてbase/quoteは不変のidentity属性
- pip value、exposure decomposition、conversion path、pair-state projectionすべてで必要
- symbol文字列のparseに依存しない
- brokerのsymbol aliasにも耐えられる

例:

```python
@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str

    base_currency: Currency
    quote_currency: Currency

    pip_size: Decimal
    price_tick_size: Decimal

    contract_size: Decimal
    volume_min_lots: Decimal
    volume_step_lots: Decimal
    volume_max_lots: Decimal | None

    filling_modes: frozenset[FillingMode]

    broker_symbol: str
    known_at: datetime
```

`base_currency` / `quote_currency` はstrategy codeからsymbol parseしない。

## 7.2 Broker capability

USDJPYで実測済み:

- `filling_mode = 2`（IOC）
- FOKは拒否
- `volume_min = 0.01 lot`
- `volume_step = 0.01`
- `contract_size = 100000`
- 0.01 lot = 1,000 base currency units
- server time offset = UTC+3（現行実測）

ただし GBPUSD / EURUSD / GBPJPY は未実測。

したがって、

> **filling_mode等をペア横断でハードコードしてはいけない。**

MT5 / brokerから取得したInstrumentSpecをtruth sourceとする。

---

# 8. JPY口座のAccount Currency Conversion

## 8.1 現行バグ

現行:

```python
loss_per_unit = signal.stop_distance_pips * sizing.pip_size
```

これはprice distanceをquote currency/base unitとして計算している。

USDJPY / GBPJPYではquote=JPYなので、結果がそのままJPY/base unitとなる。

GBPUSD / EURUSDではquote=USDなので、結果はUSD/base unitである。

JPY risk budgetと直接比較すると通貨次元が一致せず、USDJPY≈150なら約150倍級のover-sizeを許容し得る。

本対応の最優先Safety Fixとする。

---

# 9. 正しいSizing Formula

FX pairを `BASE/QUOTE` とする。

```text
stop_price_distance
    = stop_distance_pips × pip_size

loss_quote_per_base_unit
    = stop_price_distance
```

base unitsを `U` とすると、

```text
raw_stop_loss_quote
    = |U| × stop_price_distance
```

account currencyがJPYなので、

```text
stop_loss_jpy
    = raw_stop_loss_quote
    × conversion_rate(QUOTE → JPY)
```

したがって1 base unit当たりのriskは、

```text
risk_per_unit_jpy
    = stop_price_distance
    × R(QUOTE → JPY)
```

実運用ではさらに、

```text
effective_risk_per_unit_jpy
    =
      stop_price_distance
    + execution_slippage_buffer_price
    + gap/event_buffer_price
```

をquote currency単位で評価し、JPYへ変換する。

commission等がある場合はJPY換算後に加算する。

### USDJPY

```text
QUOTE = JPY
R(JPY → JPY) = 1
```

### GBPJPY

```text
QUOTE = JPY
R(JPY → JPY) = 1
```

### GBPUSD

```text
QUOTE = USD
R(USD → JPY) = fresh USDJPY conversion quote
```

### EURUSD

同様。

---

# 10. AccountCurrencyConversionService

責務はStrategyではなくPortfolio / Risk共通domain serviceへ置く。

**Decision: risk domainではMoney型を徹底し、呼び出し側が生のconversion rateへ触れない。**
通貨次元バグを命名規約ではなく型で表現不能にする。

```python
@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: Currency

@dataclass(frozen=True)
class ConversionTrace:
    path: tuple[str, ...]
    source_known_at: tuple[datetime, ...]
    max_leg_age_ms: int
    purpose: ConversionPurpose

@dataclass(frozen=True)
class ConversionResult:
    money: Money
    trace: ConversionTrace

class AccountCurrencyConversionService(Protocol):
    def convert(
        self,
        money: Money,
        to_currency: Currency,
        now: datetime,
        purpose: ConversionPurpose,
        stress: ConversionStress | None = None,
    ) -> ConversionResult:
        ...
```

`is_stale` をfrozen DTOへ保存しない。stalenessは必ず `convert(..., now=...)` の使用時にserviceが評価し、staleならrisk-increasing purposeでは結果を返さず失敗する。
監査用途のrate/path/ageは `ConversionTrace` に記録するが、risk計算側は `Money` のみを使用する。

## 10.1 Conversion path

当前4ペアではSizing上もっとも重要なのは:

```text
USD → JPY
```

であり、USDJPYを使用する。

将来のfactor exposureのmark-to-JPYでは:

```text
EUR → USD → JPY
GBP → USD → JPY
```

等を利用可能にする。

Conversion Serviceはcurrency graphを使える設計にするが、当初はapproved pathのみ許容してよい。
無制限な自動path探索は価格sourceの混在・staleness増加を招くため避ける。

## 10.2 Conservative conversion

Risk purposeでは「現在価値の平均」ではなく、riskを過小評価しないrateを採用する。

例:

- direct quoteではbid/askのうちloss valuationが大きくなる側
- conversion slippage bufferを加える
- multi-legでは各legの保守的rateを使う

正確なbroker PnL conversionとの差はdemoで計測し、bufferを校正する。

---

# 11. Conversion Quote Staleness Policy

GBPUSD / EURUSD のSizingはUSDJPY quote鮮度に依存する。

この依存は明示的に扱う。

## 11.1 新規 / Risk Increasing

以下はfail-close。

- conversion quoteが存在しない
- quoteの `known_at` がfuture
- freshness threshold超過
- conversion pathに1 legでもstale
- source timestamp異常
- price <= 0
- conversion source integrity failure

結果:

```text
CONVERSION_RATE_UNAVAILABLE
CONVERSION_RATE_STALE
→ NEW ENTRY / SIZE INCREASE REJECT
```

## 11.2 既存Position

新規を止めても既存positionのrisk monitoringは止めない。

stale時:

- last-good conversionを保持
- stale haircut / adverse bufferを加えてrisk estimate
- alert
- new / increaseは禁止
- reduce / closeは許可

Exitをconversion quote欠損で不要に止めない。

## 11.3 Backtest PIT

Conversion quoteも必ず、

```text
known_at <= replay_clock.now()
```

を満たす。

EURUSDのhistorical sizingで未来のUSDJPY closeを使うことは禁止。

---

# 12. Fundamental / Regime: Currency-first Model

## 12.0 Fundamental Data Acquisition Preconditions

CurrencyState(GBP/EUR)を実装する前に、**データ供給を独立した設計・調達マイルストーンとして確立する**。
現行の日米CollectorだけでGBP/EUR stateを作ってはならない。

### GBP source plan

| Feature family | Primary official source | Notes |
|---|---|---|
| Policy / Bank Rate / MPC | Bank of England | MPC decisions, Bank Rate history, minutes / communication |
| Inflation / labour / wages / GDP / retail | ONS API | Open API。adapter versioningとraw payload保存必須 |
| Rates / curve | Bank of England Database | official rates / yield-curve data |
| Market-implied BOE path | ICE MPC Dated SONIA futures / SONIA futures | **data entitlement・historical depth・latencyを調達評価** |

### EUR source plan

| Feature family | Primary official source | Notes |
|---|---|---|
| Policy / ECB communication | ECB | monetary-policy decisions / communication |
| ECB statistical data / rates | ECB Data Portal SDMX API | machine-readable official source |
| HICP / GDP / labour等 | Eurostat SDMX API | revisions / PIT handlingに注意 |
| Market-implied ECB path | ICE ECB Dated €STR futures / €STR futures | **data entitlement・historical depth・latencyを調達評価** |

### Policy-path procurement gate

CME FedWatch相当の「会合別の市場織り込み」はGBP/EURで無料APIが自明ではないため、以下を比較して採用可否を決める。

- API / feed availability
- live / delayed latency
- historical depth
- exchange timestamp / known_at再現性
- meeting-date contract mapping
- redistribution / storage license
- cost
- outage behavior

ICEはMPC Dated SONIA futuresおよびECB Dated €STR futuresを提供しており、原資産としては適切だが、**画面で見えることとBOTが履歴・リアルタイムで合法的かつ再現可能に取得できることは別問題**である。調達Gate通過までは `rates_score` のlive promotionを禁止する。

代替としてofficial policy rate / yield-curve featuresを使うことは可能だが、market-implied meeting pathと同一featureとはみなさず、coverage不足としてconfidenceを下げる。

### PIT / revision policy

全official collectorは最低限以下をraw保存する。

```text
source_id
published_at
collected_at
known_at
release_id
revision_id / vintage_id if available
raw_payload_hash
raw_payload
```

Eurostatのdissemination APIは原則として最新観測値を返し、更新前の値をAPIから取り戻せないため、**現在から先のPITは自前snapshotで構築できても、過去revisionを単純backfillしてPITと称してはならない**。historical vintageが取得できないseriesは `PIT_UNVERIFIED` として厳格なOOS/PIT評価から除外する。

ONS APIはopenだがbetaであるため、breaking changeを前提にadapter contract testとraw snapshotを持つ。

## 12.1 CurrencyState

```python
@dataclass(frozen=True)
class CurrencyState:
    currency: Currency

    directional_score: Decimal

    policy_score: Decimal
    growth_score: Decimal
    inflation_score: Decimal
    rates_score: Decimal
    risk_sentiment_score: Decimal

    confidence: Decimal

    regimes: frozenset[RegimeLabel]
    intervention_risk: InterventionRiskState

    known_at: datetime
```

対象:

```text
USD
JPY
GBP
EUR
```

## 12.2 PairState

```python
@dataclass(frozen=True)
class PairState:
    symbol: str
    base: CurrencyState
    quote: CurrencyState

    directional_score: Decimal
    confidence: Decimal

    event_risk: PairEventRisk
    intervention_risk: PairInterventionRisk
```

Directional:

```text
directional_score
= base.directional_score - quote.directional_score
```

Confidenceは単純平均ではなく、両legのfreshness / coverage / conflictを考慮する。

## 12.2A Cross-Currency Score Normalization

`base - quote` が意味を持つ条件として、4通貨のdirectional scoreを同一尺度へ校正する。
USDだけデータ量が多いことを理由に振れ幅・confidenceが機械的に大きくならないようにする。

### Score semantics

最終 `directional_score` は「strategy horizonにおける予想通貨returnを、その通貨自身のriskで標準化した共通risk-unit」の近似として扱う。
raw feature scoreを直接通貨間で減算しない。

初期推奨pipeline:

```text
raw_currency_score
  ↓
PIT rolling robust normalization
  z = (x - rolling_median) / robust_scale(MAD)
  ↓
clip to configured range
  ↓
bounded transform (e.g. tanh)
  ↓
walk-forward calibration to forward risk-normalized return
  ↓
comparable directional_score
```

- rolling statisticsはtraining window / known_at内だけで計算する
- normalization parameterを全期間でfitしない
- confidenceはscore magnitudeと別変数にする
- source coverage不足でscore自体を膨らませずconfidenceを下げる

### Pair projection

```text
pair_score = normalized_base_score - normalized_quote_score
```

### USD-specific sensitivity

USDは4ペア中3ペアに現れるため、通常のfactor validationに加え、USD scoreへ±0.25σ / ±0.5σ / ±1.0σ相当のperturbationを与え、以下を測る。

- pair direction flip rate
- accepted-trade set change
- portfolio USD concentration
- PnL / DD sensitivity
- arbitration ranking stability

USD model errorがportfolio全体へ過剰伝播する場合はUSD factor cap / confidence haircutを導入する。

## 12.3 Risk Stateは単純減算しない

以下はdirectional scoreと分離。

- central bank event risk
- intervention risk
- data release freeze
- liquidity state
- execution degradation
- conversion quote risk

たとえばJPY介入リスクが高い場合、

```text
USDJPY
GBPJPY
```

の両方へ適用する。

ただしGBPJPYは通常ボラが大きいため、同一介入stateでもposition sizing / entry gateの影響度はpair-specific risk ruleで調整可能にする。

---

# 13. Regime Service

現行のglobal:

```python
RegimeService.active()
```

だけでは4通貨に不足する。

推奨:

```python
class CurrencyRegimeService(Protocol):
    def active(
        self,
        currency: Currency,
        now: datetime,
    ) -> frozenset[RegimeLabel]:
        ...
```

またはsnapshot:

```python
@dataclass(frozen=True)
class CurrencyRegimeSnapshot:
    by_currency: Mapping[Currency, frozenset[RegimeLabel]]
    global_regimes: frozenset[RegimeLabel]
    known_at: datetime
```

Global regimeは残す。

例:

- GLOBAL_RISK_OFF
- GLOBAL_LIQUIDITY_STRESS

Currency regime:

- USD_POLICY_HAWKISH
- JPY_POLICY_HAWKISH
- GBP_POLICY_HAWKISH
- EUR_POLICY_HAWKISH

Strategyにはread-only snapshotを渡す。

---

# 14. Event Risk Calendar

現行:

```python
EventRiskCalendar.mode_for(horizon, now)
```

ではscopeがない。

Eventにaffected currenciesを持たせる。

```python
@dataclass(frozen=True)
class RiskEvent:
    event_id: str
    scheduled_at: datetime
    affected_currencies: frozenset[Currency]
    severity: EventSeverity
    event_type: EventType
    known_at: datetime
```

例:

```text
FOMC        → {USD}
BOJ         → {JPY}
BOE         → {GBP}
ECB         → {EUR}
US NFP      → {USD}
UK CPI      → {GBP}
Euro HICP   → {EUR}
```

pair mode:

```python
mode_for_instrument(
    instrument_spec,
    horizon,
    now,
)
```

内部では:

```text
affected_currencies
∩ {base_currency, quote_currency}
```

が空ならpairを止めない。

したがってECBだけを理由にUSDJPYは停止しない。

## 14.1A Cross-Pair Event Propagation

`affected_currencies ∩ {base, quote}` だけでは不十分。
例: FOMC={USD} と GBPJPY={GBP,JPY} はdirect intersectionが空だが、`GBPJPY ≒ GBPUSD × USDJPY` でありFOMCはGBPJPYへ重大なvolatilityを伝播し得る。

**v2.1 initial safety decision:**

```text
propagation_policy = GLOBAL_CRITICAL
```

に指定されたイベントは、direct legに関係なく**全4ペアのnew / risk-increasing entryをhard gate**する。初期対象には少なくともFOMC policy decisionを含める。年間の限定的な機会損失よりfalse-negative event entryを避ける。

Event modelを拡張する。

```python
class EventPropagationPolicy(Enum):
    DIRECT_LEGS = "DIRECT_LEGS"
    GLOBAL_CRITICAL = "GLOBAL_CRITICAL"
    DEPENDENCY_GRAPH = "DEPENDENCY_GRAPH"

@dataclass(frozen=True)
class RiskEvent:
    ...
    affected_currencies: frozenset[Currency]
    propagation_policy: EventPropagationPolicy
```

次段階では `DEPENDENCY_GRAPH` を実装し、synthetic / structural dependencyからevent sensitivityを導出する。初期4ペアでは例として:

```text
USDJPY dependencies = {USD, JPY}
EURUSD dependencies = {EUR, USD}
GBPUSD dependencies = {GBP, USD}
GBPJPY dependencies = {GBP, JPY, USD}  # synthetic bridge dependency
```

ただしdependency graphは無制限に伝播させず、各instrumentについて承認済みrisk dependencyとしてversion管理する。

Event gate判定順:

```text
GLOBAL_CRITICAL? → all-pair hard gate
else DEPENDENCY_GRAPH? → dependency intersection
else → direct base/quote intersection
```

## 14.1 Dual Central Bank Cluster

既存の `dual_central_bank_cluster` 思想は維持する。

ただしglobal clusterにしない。

pair-local clusterとする。

例:

```text
USDJPY → FOMC + BOJ
GBPUSD → BOE + FOMC
EURUSD → ECB + FOMC
GBPJPY → BOE + BOJ
```

連続する関連イベントのみでcluster stateを作る。

4中銀すべてを1 global clusterへ入れることは禁止。

---

# 15. Strategy Architecture: 1 Strategy × 4 Pairs

## Decision

「ペアごとにほぼ同じStrategyクラスを4本作る」は採用しない。

```text
1 Strategy Implementation
        ×
4 Instruments
```

とする。

理由:

- Fundamental modelをcurrency-firstに統一できる
- 同じedgeの汎化を検証できる
- pair-specific code driftを防げる
- 既存 `StrategyConfig.instruments` list を活用できる

ただしStrategy stateはsymbolごとに分離する。

既存 `_new_setup(symbol, direction)` を活用する。

---

# 16. Per-Instrument Parameter Override

現行のflat `StrategyConfig.parameters` は拡張する。

例:

```yaml
strategy:
  id: core_fx
  instruments:
    - USDJPY
    - GBPUSD
    - EURUSD
    - GBPJPY

  parameters:
    defaults:
      timeframe: M5
      min_signal_confidence: "0.65"

      spread_gate:
        max_spread_to_atr: "..."
        max_spread_percentile: "..."

    instruments:
      USDJPY:
        session_profile: usdjpy_core
        absolute_max_spread_pips: "..."

      GBPUSD:
        session_profile: london_ny_major
        absolute_max_spread_pips: "..."

      EURUSD:
        session_profile: london_ny_major
        absolute_max_spread_pips: "..."

      GBPJPY:
        session_profile: gbpjpy_cross
        absolute_max_spread_pips: "..."
```

Strategyはconfig dictを直接探索せず、resolverを使用する。

```python
class StrategyParameterResolver(Protocol):
    def resolve(
        self,
        strategy_id: str,
        symbol: str,
    ) -> ResolvedStrategyParameters:
        ...
```

---

# 17. Pips閾値の扱い

`max_spread_pips: 2.0` のようなUSDJPY前提の固定値を4ペア共通にしない。

## Primary Gate

無次元・相対表現を優先する。

候補:

```text
spread / ATR
spread percentile
spread / recent median spread
execution cost / expected move
realized volatility normalized stop
```

## Secondary Safety Ceiling

broker異常や極端なspread拡大を止めるため、

```text
absolute_max_spread_pips
```

はpair別に保持してよい。

したがって:

> normalized gate = primary  
> pair-specific absolute pips = hard safety ceiling

とする。

pip自体はRisk / Execution domainでは引き続き必要であり、廃止しない。

---

# 18. Session Model

Broker server time（UTC+3）とmarket sessionを分離する。

Strategyはserver timeで判断しない。

Session Serviceは `Clock` からUTC instantを受け取り、market-local timezoneへ変換する。

推奨timezone:

```text
Tokyo    → Asia/Tokyo
London   → Europe/London
New York → America/New_York
```

London / New YorkのDSTをtimezone databaseで吸収する。

MT5 server UTC+3はadapter側timestamp normalizationの責務。

---

# 19. Strategy × Pair × Session Matrix

初期policy。
実測spread / slippage / expectancyで更新する。

Legend:

```text
◎ Preferred
○ Allowed
△ Shadow / conditional
× New entry disabled by default
```

## 19.1 Core Directional / Fundamental Strategy

| Pair | Tokyo | London | New York |
|---|---:|---:|---:|
| USDJPY | ○ | ○ | ◎ |
| GBPUSD | × / △ | ◎ | ◎ |
| EURUSD | × / △ | ◎ | ◎ |
| GBPJPY | △ | ◎ | ○ |

## 19.2 Short-Horizon / Scalping — RESEARCH_ONLY

**SYSTEM_SPEC v1.3の既存決定を維持し、ScalpingはRESEARCH_ONLYである。**
以下のmatrixはshadow / research計算を許可する時間帯の仮説であり、live `trading_enabled=true` を意味しない。Scalpingの本番昇格は本設計のscope外で、将来解除する場合は専用ADRとSYSTEM_SPEC更新が必要。

| Pair | Tokyo | London | New York |
|---|---:|---:|---:|
| USDJPY | Research | Research | Research |
| GBPUSD | Off | Research | Research |
| EURUSD | Off | Research | Research |
| GBPJPY | Off | Research | Research |

GBPJPY Tokyo scalpはresearchでも初期disable。
spread / fill / slippage / expectancyはshadow dataとして収集するが、本設計だけを根拠にliveへ昇格させない。

## 19.3 Event-Driven

Event-driven strategyはsession tableだけでhard rejectしない。

条件:

- eventがpair legにscopeされている
- market liquidity gateを満たす
- spread / slippage gateを満たす
- Event Risk policy上entry可
- execution rate limit内

---

# 20. Currency Exposure Model

Pair単位のpositionだけでは不十分。

FX positionをcurrency legsへ分解する。

Pair = `BASE/QUOTE`  
signed base unitsを `U` とする。

```text
LONG  → U > 0
SHORT → U < 0
```

spot price = `P`（QUOTE per BASE）

currency exposure:

```text
BASE exposure  += U
QUOTE exposure += -U × P
```

例:

```text
EURUSD SHORT
→ EUR exposure negative
→ USD exposure positive

GBPUSD SHORT
→ GBP exposure negative
→ USD exposure positive

USDJPY LONG
→ USD exposure positive
→ JPY exposure negative
```

したがって3本は「独立3trade」ではなく共通USD LONG factorを持つ。

---

# 21. Exposureの評価単位

1種類のlimitだけにしない。

以下の多層risk modelを採用する。

## Layer 1 — Per-Trade Stop Risk

JPY account currencyで計算。

```text
trade_stop_risk_jpy
trade_stop_risk_pct_equity
```

最も基本となるhard constraint。

## Layer 2 — Portfolio Open Stop Risk

全open positionのstop-risk合計。

```text
sum(open_stop_risk_jpy)
<= portfolio_stop_risk_budget_jpy
```

初期pilotでは、現行 `max_risk_per_trade_pct = 0.05%` を維持したまま、
portfolio上限を別configとして追加する。

数値はMonte Carlo / demoで校正する。
4本×0.05%=0.20%を自動的に許可する設計にはしない。

## Layer 3 — Currency Net Exposure

各currencyのsigned exposureをJPY markする。

```text
net_currency_exposure_jpy[currency]
gross_currency_exposure_jpy[currency]
```

これによりUSD LONG集中等を検出する。

## Layer 4 — Currency / Joint Conversion Stress Loss

notionalだけではriskの大きさを表しづらいため、currency shockを与えたportfolio PnLを計算する。

```text
currency_stress_loss_account[currency]
```

shock sizeはrolling volatility / historical stress / configured floorから生成する。

さらにEURUSD / GBPUSDでは、損失がUSD建てで発生し、そのJPY換算rate自体が同じmarket moveと相関し得るため、**position directionに条件付けたjoint conversion stress**を入れる。

例:

```text
LONG EURUSDがstopへ到達するhistorical scenarios
  ↓
同じhorizonのUSDJPY change distributionを抽出
  ↓
lossのJPY換算を悪化させる側のconditional quantileを採用
```

SHORTでは相関方向が異なり得るためLONG/SHORT別に校正する。
「常にUSDJPY上昇」と固定仮定しない。

Sizing用のconversion stressは、該当pair・direction・stop horizonのhistorical conditional distributionからP95等のadverse quantileを推定し、sample不足時はdeterministic conservative floorへfallbackする。

Backtestのrealized PnLはexit時点で実際にPITで利用可能なconversion rateを使用し、entry時固定rateで確定しない。

これをequity比でcapする。

## Layer 5 — Structural / Correlation Cluster

後述。

---

# 22. Structural Risk Clusters

Dynamic correlationだけに頼らない。
FXには構造上の関係がある。

## 22.1 JPY Factor

```text
USDJPY
GBPJPY
```

## 22.2 USD Factor

directionによって:

```text
USDJPY LONG
GBPUSD SHORT
EURUSD SHORT
```

等が同じUSD LONGになる。

pair名だけでclusterを決めず、currency leg decompositionから判定する。

## 22.3 Triangle

```text
GBPJPY ≒ GBPUSD × USDJPY
```

GBPUSD + USDJPY と GBPJPY は重複riskになりやすい。

## 22.4 EURUSD / GBPUSD

共通USD legに加え、risk-on/off等でreturn correlationが高まる局面がある。

---

# 23. Dynamic Correlation

Dynamic correlationは補助として使う。

理由:

- correlationはregimeで変化
- structural currency exposureは経済的により安定した関係
- 過去相関だけでhard blockするとfalse positive/negativeが起きる

方針:

```text
Structural exposure / triangle
→ hard constraint

Rolling correlation / covariance
→ redundancy penalty / risk multiplier
```

極端なcorrelation stateのみhard overrideを許可する。

---

# 24. Position Limits

現行global `max_open_positions = 1` は分解する。

```yaml
risk:
  max_open_positions_per_symbol: 1
  max_open_positions_portfolio: 3   # pilot default; configurable
```

`3` は4ペアplatformに対する初期conservative defaultであり、不変値ではない。

Portfolio Arbitratorと検証結果により変更可能。

HEDGING modeであっても、同一strategy/systemの同一symbolについて無制限に複数ticketを許可しない。

複数strategyが将来必要ならstrategy-idを含む別limitを追加する。

---

# 25. Portfolio Arbitrator

Strategyが直接発注順を決めない。

Strategyは raw candidate を生成する。

```python
@dataclass(frozen=True)
class CandidateSignal:
    strategy_id: str
    symbol: str
    position_direction: PositionDirection

    expected_edge_r: Decimal
    confidence: Decimal

    stop_distance_pips: Decimal

    generated_at: datetime
    expires_at: datetime
```

Portfolio Arbitrator:

```python
class PortfolioArbitrator(Protocol):
    def select(
        self,
        candidates: Sequence[CandidateSignal],
        portfolio: PortfolioSnapshot,
        now: datetime,
    ) -> ArbitrationResult:
        ...
```

---

# 26. 同時Signal Arbitration

4ペア同時signal時は以下の順。

## Step 1 — Candidate Validity

- expiredでない
- trading_enabled
- data fresh
- strategy invariant pass

## Step 2 — Pair Gate

- pair session
- spread
- liquidity
- event risk
- instrument capability

## Step 3 — Account Currency Risk

- conversion rate fresh
- minimum lot feasible
- stop-risk in JPY

## Step 4 — Existing Portfolio

- per-symbol limit
- global open position limit
- portfolio stop-risk

## Step 5 — Currency / Structural Risk

- currency net exposure
- currency stress loss
- triangle overlap
- structural cluster cap

## Step 6 — Dynamic Redundancy

- rolling correlation
- covariance / marginal risk contribution

## Step 7 — Rank

候補の「単独score」ではなく、現在portfolioへ追加した場合の限界効用で並べる。

概念:

```text
priority
=
  expected_edge
× confidence
× liquidity_quality
- expected_execution_cost
- event_penalty
- redundancy_penalty
- marginal_portfolio_risk_penalty
```

係数はbacktestで固定し、LLMがliveで自由に変更しない。

## Step 8 — Greedy Re-evaluation

最上位candidateを1つacceptするたび、portfolio exposureを更新して残りを再評価する。

同じfactorのcandidateを4本まとめてacceptしない。

---

# 27. 重複Signalの初期Policy

初期は:

> **strongest signal wins**

とする。

例:

```text
EURUSD SHORT
GBPUSD SHORT
USDJPY LONG
```

が同時に強いUSD LONGを示す場合、

- expected edge
- confidence
- spread / slippage
- session liquidity
- marginal risk
- event proximity

を比較し、最も効率の良い1本を優先。

残りは:

```text
REJECTED_REDUNDANT_FACTOR_EXPOSURE
```

またはshadow記録。

Risk budget splitは、

- OOS
- portfolio backtest
- Monte Carlo

で複数保有がrisk-adjusted returnを改善すると確認できた場合のみ有効化する。

---

# 28. Pre-Trade Risk Check Order

推奨順序:

```text
1. Instrument exists / capability fresh
2. trading_enabled
3. signal freshness
4. market data freshness
5. event / intervention hard gate
6. session / liquidity gate
7. account conversion quote freshness
8. calculate JPY risk per unit
9. broker minimum / step quantization
10. MINIMUM_BROKER_SIZE_EXCEEDS_RISK
11. per-symbol position limit
12. portfolio position limit
13. portfolio stop-risk limit
14. currency exposure limits
15. structural / triangle limits
16. dynamic correlation penalty / limit
17. margin / broker constraints
18. final re-price / re-validation
19. OMS
```

Exit / risk reducing orderはentry flowとは別priority queueを持つ。

---

# 29. Minimum Lot / Risk Budget

既存:

```text
max_risk_per_trade_pct = 0.05%
```

最小ロット 1,000 units と仮定した概算:

| Pair | 仮Stop | 1,000 units損失 | 0.05%で必要Equity |
|---|---:|---:|---:|
| USDJPY | 10 pips | 100 JPY | 200,000 JPY |
| EURUSD | 15 pips | 約225 JPY | 約450,000 JPY |
| GBPUSD | 20 pips | 約300 JPY | 約600,000 JPY |
| GBPJPY | 30 pips | 300 JPY | 600,000 JPY |

※ USDJPY=150JPYを仮定した説明用概算。live sizingではfresh conversion quoteを使用する。

## Decision Rule

```text
risk_budget_jpy
= equity_jpy × allowed_risk_pct

loss_min_size_jpy
= effective_stop_loss_per_unit_jpy
× broker_min_units
```

```text
if loss_min_size_jpy > risk_budget_jpy:
    reject(MINIMUM_BROKER_SIZE_EXCEEDS_RISK)
```

対応優先順位:

1. trading_enabledをfalseのままにする
2. strategy上合理的ならstop設計を改善
3. 十分なOOS / Monte Carlo根拠がある場合のみrisk_pctを再設計

禁止:

> 「最小ロットを建てたいからrisk%を上げる」

Broker constraintがrisk appetiteを決めてはいけない。

---

# 30. Platform-enabled / Trading-enabled

4ペア対応実装と本番許可を分離する。

```yaml
instruments:
  USDJPY:
    platform_enabled: true
    trading_enabled: true

  GBPUSD:
    platform_enabled: true
    trading_enabled: false

  EURUSD:
    platform_enabled: true
    trading_enabled: false

  GBPJPY:
    platform_enabled: true
    trading_enabled: false
```

`trading_enabled=false` でも以下は動かす。

- collection
- features
- currency state
- pair state
- raw signal
- hypothetical sizing
- arbitration simulation
- rejection reason
- backtest
- shadow execution record

---

# 31. Rollout Priority

## Phase 1 — USDJPY

現行strategy / execution / riskの基準線を確立。

## Phase 2 — EURUSD

**v2.1ではEURUSDを2番目へ変更する。**

主目的が「USD分析の汎化検証」である以上、cost / liquidity noiseが最も小さいmajor pairを先に使う。BIS 2025ではEUR/USDは世界FX取引の21.2%、USD/GBPは7.6%であり、EURUSDのほうがより強い汎化テスト市場である。

目的:

- USDをquote側から検証
- EUR factor追加
- London / NYでUSDJPYとは異なるmicrostructureを検証
- 最初のnon-JPY pairとしてJPY conversion / Money / staleness設計を実証

## Phase 3 — GBPUSD

EURUSDでnon-JPY sizingとUSD factor汎化を確認した後に追加する。

目的:

- GBP factor追加
- 英国固有riskを含む、よりノイジーなmajorで汎化を再検証
- EURUSDとの共通USD factor / correlation arbitrationを実戦検証
- 後続GBPJPYに必要なGBP intelligenceを成熟させる

## Phase 4 — GBPJPY

最後。

理由:

- 新currency factorを追加しない
- triangle overlap
- volatility
- minimum lot risk
- JPY intervention exposure
- USD系critical eventのsynthetic propagationも扱う必要がある

採用基準は単体PFではなくportfolioへの限界寄与。

---

# 32. OANDA / MT5 Execution Design

## 32.1 Filling Mode

USDJPYでIOCのみ実測。

他pair:

```text
InstrumentSpec.filling_modes
```

から取得。

発注時:

```text
preferred mode
∩ broker supported modes
```

で決定する。

FOK等をstrategy/configの共通固定値にしない。

## 32.2 Rate Limits

実測制約として設計に織り込む:

- 同一symbol: 最大5 requests/sec
- market new entry: 1/sec

Rate limiterはOMS / adapter側。

## 32.3 Priority Queue

優先順位:

```text
1. Emergency / forced risk reduction
2. Position close / reduce
3. Protective order repair
4. Accepted new entries
5. Non-critical amendments / telemetry
```

4ペア同時new entryはPortfolio Arbitratorの順位を維持しつつ1秒間隔でqueueする。

## 32.4 Queue中のRevalidation

queue待機中に:

- signal expiry
- price move
- spread expansion
- event mode change
- conversion staleness
- portfolio exposure change

が起きる可能性がある。

したがってsend直前にpre-trade riskを再実行する。

---

# 33. Failure Modes

最低限以下を明示的に扱う。

## 33.1 Currency Dimension Bug

USD lossをJPY risk budgetと比較。

対策:
- Money / Currency型
- conversion service
- unit tests

## 33.2 Stale Conversion

USDJPY tick停止中にEURUSD/GBPUSD entry。

対策:
- fail-close for risk-increasing
- last-good + haircut for monitoring
- exit許可

## 33.3 Future Data Leak

backtestでfuture USDJPY conversion。

対策:
- known_at invariant

## 33.4 Correlated Over-Positioning

複数pairで同方向USD exposure。

対策:
- currency decomposition
- arbitrator

## 33.5 Triangle Duplication

GBPUSD + USDJPY + GBPJPY。

対策:
- structural cluster

## 33.6 Global Event Overblocking

ECBでUSDJPY停止。

対策:
- affected currencies

## 33.7 Underblocking JPY Intervention

GBPJPYへJPY interventionを適用し忘れる。

対策:
- currency-level intervention state

## 33.7A Cross-Pair Event Underblocking

FOMCがGBPJPYのdirect currency scopeに入らずentryを許可する。

対策:
- GLOBAL_CRITICAL all-pair gate
- 後続dependency graph

## 33.7B Currency Score Scale Drift

USDだけfeature量・分散が大きく `base-quote` を支配する。

対策:
- PIT robust normalization
- bounded transform
- walk-forward calibration
- USD perturbation sensitivity

## 33.8 Session Misclassification

broker UTC+3をLondon sessionとして扱う。

対策:
- server time / market session分離
- IANA timezone

## 33.9 DST Drift

London / NY sessionが季節で1hずれる。

対策:
- timezone database

## 33.10 Unsupported Fill Mode

USDJPYのIOC実測値を他pairへコピー。

対策:
- InstrumentSpec discovery

## 33.10A Missing Carry Cost

Overnight positionのswapをbacktestが無視し期待値を過大評価する。

対策:
- symbol swap properties PIT snapshot
- direction/day-specific rollover accounting

## 33.11 Minimum Lot Risk Violation

quantize後minimum lotがrisk budget超過。

対策:
- quantize後に再計算しreject

## 33.12 Rate-Limit Stale Entry

4signalをqueueし、最後のorderが古いpriceで送られる。

対策:
- send直前full revalidation

---

# 34. Test Plan

## 34.1 Unit — Conversion

- JPY→JPY = 1
- USD→JPY direct
- inverse conversion
- multi-leg conversion
- Decimal precision
- zero / negative rate reject
- future `known_at` reject
- stale detection
- path freshness = worst leg freshness
- conservative bid/ask rule

## 34.1A Unit — Money / Currency Dimension

- cross-currency Money.add raises
- conversion returns account-currency Money
- no risk-domain raw `_jpy: Decimal` monetary fields
- stale result is not cached as boolean state

## 34.2 Unit — Sizing

- USDJPY expected units
- GBPJPY expected units
- GBPUSD converts USD loss→JPY
- EURUSD converts USD loss→JPY
- old ~150x oversize regression test
- volume step rounding never increases risk above budget
- minimum units reject

## 34.3 Unit — Exposure

- LONG/SHORT sign
- base / quote decomposition
- EURUSD SHORT gives USD LONG exposure
- USDJPY LONG gives USD LONG exposure
- triangle exposure approximation
- gross / net exposure

## 34.4 Unit — Event Scope

- ECB direct event does not stop USDJPY unless explicitly GLOBAL_CRITICAL
- FOMC GLOBAL_CRITICAL affects USDJPY / EURUSD / GBPUSD / GBPJPY
- dependency graph mode makes FOMC reach GBPJPY synthetic bridge
- BOJ affects USDJPY / GBPJPY
- BOE affects GBPUSD / GBPJPY
- dual-bank cluster ispair-local

## 34.5 Unit — Session

- Tokyo
- London DST
- New York DST
- UTC+3 server timestamp normalization
- session result independent of broker offset

## 34.5A Unit — Score Calibration

- normalization uses PIT/training-only windows
- all currency score distributions remain comparable
- missing-source confidence haircut
- USD perturbation sensitivity deterministic

## 34.5B Unit — Carry Cost

- long/short swap direction
- weekday multiplier from broker data
- triple rollover uses returned multiplier, not hardcoded Wednesday
- historical backtest uses latest-known swap snapshot

## 34.6 Unit — Arbitrator

- input ordering does not change deterministic accepted set
- duplicate USD factor selects strongest
- risk limits recomputed after each accept
- expired candidate rejected
- correlation penalty
- triangle hard cap
- existing portfolio alters ranking

## 34.7 Unit — OMS / Rate Limit

- per-symbol 5 req/sec
- entry 1/sec
- exits prioritized
- queued entry expires
- queued entry revalidated

## 34.8 Invariant Regression

既存 `test_invariants.py` を全件維持。

追加:

- StrategyからConversion/Broker write path不可
- Decimal
- Clock
- ticketed exit
- known_at

---

# 35. Backtest Validation Plan

## 35.1 PIT Replay

4pair market data + conversion quotes + event / fundamental data + broker carry-cost snapshotsを1本のknown_at timelineへmergeする。

GBP/EUR macro seriesはrevision/vintage provenanceが検証できるものだけをstrict PIT validationへ使用する。

重要:

> EURUSD/GBPUSD sizing時に、その時点で観測可能だったUSDJPY conversion quoteを使う。

## 35.2 Cost Model

pair / direction / session別に:

- spread
- slippage
- reject
- entry delay
- **swap / rollover carry**

をモデル化する。

GBPUSD/EURUSD/GBPJPYへUSDJPYのcost assumptionsをコピーしない。

### Swap / Rollover PIT

Overnightを跨ぎ得るstrategyではswapを必須costとする。
MT5 symbol propertiesから少なくとも以下を定期snapshotしPIT Event Storeへ保存する。

```text
symbol
swap_mode
swap_long
swap_short
swap_sunday
swap_monday
swap_tuesday
swap_wednesday
swap_thursday
swap_friday
swap_saturday
known_at
```

MT5はlong/short swapと曜日別のrollover multiplierをsymbol propertyとして提供するため、「水曜が必ずtriple」とハードコードしない。broker/symbolが返す曜日別倍率をtruth sourceにする。

Backtestではpositionがbroker rollover boundaryを跨いだ時点のPIT swap snapshotを使ってcarryを計上する。

Intraday strategyでもovernight禁止を明示していない限り、unexpected hold / execution failureでcarryが発生し得るためtelemetryには残す。Swing / multi-day strategyではnet expectancyの主要構成要素として扱う。

## 35.3 Pair-level Metrics

- expectancy
- PF
- Sharpe / Sortino
- max DD
- win rate
- avg R
- tail loss
- holding time
- turnover
- cost / gross edge

## 35.4 Factor-level Metrics

- USD state accuracy
- JPY state accuracy
- GBP state accuracy
- EUR state accuracy
- directional score bucket別return
- cross-currency normalized score distribution
- score→forward risk-normalized return calibration
- confidence calibration
- USD score perturbation sensitivity / direction flip rate
- currency別source coverage / missingness bias

## 35.5 Portfolio Metrics

- portfolio Sharpe
- max DD
- return / DD
- exposure concentration
- currency stress loss
- cluster usage
- marginal contribution by pair

---

# 36. Walk-Forward / OOS

最重要の問い:

> USDJPYで見つけたedgeは、USDJPY固有か、currency-level logicとして汎化するか。

例:

```text
USDJPY PF 1.55
GBPUSD PF 1.42
EURUSD PF 1.36
```

ならcurrency logicの汎化を支持。

一方:

```text
USDJPY PF 1.55
GBPUSD PF 0.92
EURUSD PF 0.88
```

ならUSDJPY固有edge / overfitを疑う。

ただしPF単独で判断せず、cost・DD・sample size・regime coverageも確認する。

---

# 37. Monte Carlo / Robustness

最低限randomize:

- trade ordering
- spread
- slippage
- entry delay
- partial / failed execution
- event timing jitter
- conversion quote latency
- direction-conditioned adverse conversion shock
- joint pair/conversion historical scenarios
- correlated loss streak
- session cost deterioration

確認:

- P95 / P99 max DD
- risk of ruin
- worst rolling N trades
- recovery duration
- minimum equity
- minimum lot feasibility
- portfolio concentration tails

---

# 38. Shadow / Demo / Live Gate

```text
Backtest
  ↓
Walk-forward
  ↓
OOS
  ↓
Monte Carlo
  ↓
Shadow
  ↓
Demo Forward
  ↓
Small Live
  ↓
Production
```

## Shadow

USDJPY execution中でも:

```text
EURUSD
GBPUSD
GBPJPY
```

のsignal / sizing / risk / arbitrationを仮想計算する。

## Demo

取得:

- actual fill mode
- spread distribution
- slippage
- reject reason
- latency
- server time
- conversion freshness behavior

## Small Live

minimum lotがrisk allowance以内の場合のみ。

---

# 39. Success Criteria

「4ペアすべてで利益」を成功条件にしない。

## Level 1 — Platform Success

- JPY conversion正しい
- exposure集計正しい
- currency event scope正しい
- 4pair PIT replay可能
- broker capability per instrument

## Level 2 — Strategy Generalization

USDJPY以外でも同一currency-level logicがpositive OOS expectancy。

## Level 3 — Portfolio Success

USDJPY単独より:

- Sharpe改善
- return / DD改善
- DD許容
- concentration上限内

GBPJPYはLevel 3への寄与で判断する。

---

# 40. Migration Plan

## M0 — Baseline Freeze

- 現行USDJPYのtest / config / performance snapshot
- SYSTEM_SPEC v1.3は変更しない

## M1 — Safety Foundation

- Currency type / strict Money type
- InstrumentSpec base / quote
- AccountCurrencyConversionService.convert(Money)
- use-time conversion staleness
- fail-close
- direction-conditioned conversion stress interface
- manager.py / risk engine sizing fix
- regression tests

**M1完了前にGBPUSD/EURUSDを発注しない。**

## M2 — Portfolio Exposure

- CurrencyExposure
- gross / net
- portfolio stop-risk
- per-symbol / global limits
- structural clusters
- triangle risk
- joint conversion stress hooks

## M2A — GBP/EUR Fundamental Data Procurement Gate

M3のlive-grade実装前提として独立Gateにする。コードscaffoldは並行開発可能だが、GBP/EUR intelligenceをproduction-readyとはみなさない。

Deliverables:

- BOE official collector
- ONS collector
- ECB official collector
- ECB Data Portal collector
- Eurostat collector
- release calendar mapping
- revision / vintage / PIT policy
- policy-path market data procurement comparison
- ICE MPC Dated SONIA / ECB Dated €STR data entitlement確認
- licensing / storage / historical depth / latency decision
- fallback / confidence downgrade policy

Gate:

```text
required source coverage >= configured threshold
PIT provenance verified
policy-path source decision recorded
→ M3 production promotion allowed
```

## M2B — Broker Carry Cost PIT Collector

- swap_mode
- swap_long / swap_short
- weekday multipliers
- rollover boundary
- raw symbol_info snapshot
- Event Store persistence

Backtest cost modelへ接続する。

## M3 — Intelligence

- CurrencyState
- normalized comparable CurrencyDirectionalScore
- CurrencyRegime
- InterventionRisk
- PairState projection
- Currency-scoped EventRisk
- GLOBAL_CRITICAL event propagation
- dependency graph scaffold

## M4 — Strategy Config

- default + per-instrument override
- normalized spread
- normalized volatility
- session profiles
- Scalping remains RESEARCH_ONLY

## M5 — Portfolio Arbitrator

- candidate model
- marginal utility
- redundancy handling
- deterministic arbitration
- rate-limit aware order plan

## M5.5 — Governance Consolidation

M5でarchitecture / invariantsが安定した時点で、**SYSTEM_SPEC v2.0統合作業を実施する**。

- v1.3 + accepted ADRs + Multi-Currency Design v2.1のnormative decisionsを統合
- superseded ADRを明示
- truth sourceをSYSTEM_SPEC v2.0へ一本化
- v2.0発行まではSYSTEM_SPEC v1.3 + accepted ADRがnormative、当設計書はimplementation designとして扱う

**2本目のpairをlive enableする前にSYSTEM_SPEC v2.0を発行する。**

## M6 — Shadow 4 Pairs

```text
USDJPY  execute
EURUSD  shadow
GBPUSD  shadow
GBPJPY  shadow
```

## M7 — EURUSD Gate

Validation通過時のみenable。最初のnon-JPY pairとしてconversion / Money / swap / policy-path dataを実証する。

## M8 — GBPUSD Gate

EURUSDとのUSD exposure / correlationも評価。

## M9 — GBPJPY Gate

portfolio marginal contributionおよびcross-pair event propagationを含めて最終判断。

---

# 41. Required ADRs

最低限以下を `docs/adr/` に追加する。

1. **ADR — FX Instrument base/quote currencies**
2. **ADR — Strict Money type in risk domain / conversion callers do not use raw rates**
3. **ADR — Account currency conversion and use-time stale quote fail-close**
4. **ADR — Direction-conditioned joint conversion stress**
5. **ADR — Currency-first fundamental model**
6. **ADR — Cross-currency directional score normalization and calibration**
7. **ADR — GBP/EUR official data acquisition and policy-path procurement gate**
8. **ADR — Currency-scoped regime and intervention risk**
9. **ADR — Event propagation: GLOBAL_CRITICAL first, dependency graph second**
10. **ADR — Currency-scoped event risk / pair-local central bank cluster**
11. **ADR — Per-instrument strategy parameter overrides**
12. **ADR — Normalized spread / volatility gates**
13. **ADR — Per-symbol and portfolio-global position limits**
14. **ADR — Currency exposure / structural triangle risk**
15. **ADR — Portfolio Arbitrator owns simultaneous-signal selection**
16. **ADR — Platform-enabled vs Trading-enabled instruments**
17. **ADR — Pair rollout order: USDJPY → EURUSD → GBPUSD → GBPJPY**
18. **ADR — Minimum broker size does not dictate risk appetite**
19. **ADR — Broker capabilities are discovered per instrument**
20. **ADR — Swap / rollover properties are PIT broker cost data**
21. **ADR — Market sessions are independent of MT5 server timezone**
22. **ADR — Risk-reducing exits remain possible during conversion-entry fail-close**
23. **ADR — SYSTEM_SPEC v2.0 consolidation before second live pair**

Scalpingはv1.3の `RESEARCH_ONLY` を変更しないため、新しい解除ADRは作らない。将来live昇格する場合のみ専用ADRを追加する。

---

# 42. Suggested Types / APIs

```python
class Currency(str, Enum):
    USD = "USD"
    JPY = "JPY"
    GBP = "GBP"
    EUR = "EUR"
```

```python
@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: Currency

    def add(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise CurrencyMismatchError
        return Money(self.amount + other.amount, self.currency)
```

**Risk domainの金額フィールドは原則 `Decimal + _jpy` ではなく `Money` を使う。**
`Decimal` のまま許可するのはratio / percentage / unit count / price / indicator値など、通貨次元を持たないか別型で明示されたものに限る。

```python
@dataclass(frozen=True)
class CurrencyExposure:
    currency: Currency
    net_units: Decimal
    gross_units: Decimal
    net_value_account: Money
    gross_value_account: Money
```

```python
@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    open_stop_risk: Money
    open_stop_risk_pct: Decimal

    currency_exposures: Mapping[Currency, CurrencyExposure]
    currency_stress_loss: Mapping[Currency, Money]

    active_clusters: tuple[str, ...]
    known_at: datetime
```

```python
@dataclass(frozen=True)
class ConversionTrace:
    path: tuple[str, ...]
    source_known_at: tuple[datetime, ...]
    max_leg_age_ms: int

@dataclass(frozen=True)
class ConversionResult:
    money: Money
    trace: ConversionTrace

class AccountCurrencyConversionService(Protocol):
    def convert(
        self,
        money: Money,
        to_currency: Currency,
        now: datetime,
        purpose: ConversionPurpose,
        stress: ConversionStress | None = None,
    ) -> ConversionResult:
        ...
```

Serviceはstaleness判定後の結果だけを返す。`ConversionQuote.is_stale` のような時間依存boolはDTOへ保存しない。

```python
class CurrencyExposureService(Protocol):
    def snapshot(
        self,
        positions: Sequence[Position],
        market: MarketSnapshot,
        now: datetime,
    ) -> PortfolioRiskSnapshot:
        ...
```

```python
class PairStateService(Protocol):
    def get(
        self,
        symbol: str,
        now: datetime,
    ) -> PairState:
        ...
```

```python
class EventRiskService(Protocol):
    def mode_for_instrument(
        self,
        instrument: InstrumentSpec,
        horizon: timedelta,
        now: datetime,
    ) -> EventRiskMode:
        ...
```

---

# 43. Observability

Multi-currency化では「なぜ取引しなかったか」が重要。

各candidateについて記録:

```text
candidate_id
strategy_id
symbol
direction
generated_at

currency scores
pair score
confidence

stop pips
risk per unit account-currency Money
desired units
quantized units

conversion path
conversion trace path
conversion source known_at
conversion max leg age
conversion stress policy

event mode
session
spread
spread normalized value

existing currency exposure
marginal currency exposure
cluster state
correlation state

arbitration score
rank

decision:
  ACCEPTED
  REJECTED_...

reject_reason
```

主要reject reason:

```text
TRADING_DISABLED
SIGNAL_EXPIRED
MARKET_DATA_STALE
CONVERSION_RATE_STALE
EVENT_RISK_BLOCK
SESSION_BLOCK
SPREAD_TOO_WIDE
MINIMUM_BROKER_SIZE_EXCEEDS_RISK
PAIR_POSITION_LIMIT
PORTFOLIO_POSITION_LIMIT
PORTFOLIO_RISK_LIMIT
CURRENCY_EXPOSURE_LIMIT
TRIANGLE_EXPOSURE_LIMIT
REDUNDANT_FACTOR_EXPOSURE
BROKER_CAPABILITY_UNAVAILABLE
EXPIRED_IN_RATE_LIMIT_QUEUE
```

---

# 44. 日本株BOTへの転用

今回のマルチカレンシー化は、将来の日本株BOTに明確な正の影響を持つ。

FXで構築する上位構造:

```text
Raw Signal
   ↓
Factor Exposure
   ↓
Correlation / Structural Risk
   ↓
Portfolio Arbitration
   ↓
Risk-adjusted Sizing
   ↓
OMS
```

は株式で:

```text
Raw Stock Signal
   ↓
Sector / Style / Macro Exposure
   ↓
Correlation / Concentration Risk
   ↓
Portfolio Arbitration
   ↓
Risk-adjusted Sizing
   ↓
OMS
```

へ転用できる。

---

# 45. FX → Equity Mapping

| FX | 日本株 |
|---|---|
| Currency Exposure | Sector / Style / Index / Macro Exposure |
| USD / JPY factor | Semiconductor / Bank / Exporter / Growth factor等 |
| Triangle Risk | 同一テーマ・親子・指数重複 |
| Pair Event Risk | 決算・適時開示・政策イベント |
| Currency State | Macro / Sector State |
| Pair State | Stock State |
| Platform-enabled | Universe-enabled |
| Trading-enabled | Live-trading eligible |
| Shadow pair | Shadow stock |
| Portfolio Arbitrator | Stock selector / allocator |
| PIT Replay | Point-in-Time equity backtest |
| Currency Stress | Sector / factor stress |

例:

```text
東京エレクトロン LONG
アドバンテスト LONG
ディスコ LONG
レーザーテック LONG
```

を4つの独立tradeとはみなさず、

```text
Semiconductor Factor LONG concentration
```

としてPortfolio層で認識する。

これはFXの:

```text
EURUSD SHORT
GBPUSD SHORT
USDJPY LONG
→ USD LONG concentration
```

と同じ設計思想である。

---

# 46. 将来のUnified Intelligence

将来:

```text
                    Data / News
                        │
                 Intelligence
                        │
          ┌─────────────┼─────────────┐
          ↓             ↓             ↓
        Macro         Sector        Company
          │             │             │
          └───────┬─────┴─────┬──────┘
                  ↓           ↓
             FX Signals   Stock Signals
                  │           │
                  └─────┬─────┘
                        ↓
                 Portfolio Engine
                        │
                 Factor Exposure
                        │
                    Risk Engine
                        │
                       OMS
```

を目標にできる。

FXで得た:

- USD state
- JPY state
- rates state
- global risk-on/off
- policy regime

は日本株の:

- exporters
- banks
- high-duration growth
- semiconductor
- domestic demand

等へのmacro inputとして利用可能。

---

# 47. 株式転用を見据えた命名方針

将来転用しやすい上位componentはFX専用名にしすぎない。

推奨:

```text
PortfolioArbitrator
RiskBudget
ExposureConstraint
FactorExposure
ValidationGate
TradingEligibility
```

FX固有:

```text
CurrencyExposureModel
FxConversionService
FxInstrumentSpec
CurrencyState
```

株固有:

```text
EquityFactorExposureModel
EquityInstrumentSpec
SectorState
CompanyEventState
```

重要:

> 将来株式をやるからといって、今すべてを無理に汎用化しない。

意味論が共通なPortfolio / Risk interfaceを一般化し、
FX固有のpip / base-quote / conversionを株式へ漏らさない。

---

# 48. Stock BOT Development Orderへの影響

推奨ロードマップ:

```text
USDJPY completion
        ↓
Multi-currency safety foundation
        ↓
GBPUSD / EURUSD generalization test
        ↓
Portfolio Arbitrator / Factor Exposure成熟
        ↓
Japanese Equity BOT
```

これにより日本株BOT着手時点ですでに:

- multi-asset signal arbitration
- exposure limits
- correlation handling
- shadow mode
- validation gate
- PIT discipline
- event scoping
- portfolio risk

が存在する。

したがって株BOTは「別BOTをゼロから作る」のではなく、

> 同一Portfolio Platformへ新しいinstrument / factor modelを追加する

形に近づけられる。

---

# 48A. Public Source Verification Notes (v2.1)

設計レビュー時点で確認した一次資料:

- ONS Developer Hub: public HTTP API, no API key required; beta status
- ECB Data Portal: SDMX 2.1 REST web service
- Eurostat: public Statistics / SDMX APIs; dissemination APIの更新後は以前のobservation valueを返さない点に注意
- Bank of England Database: official Bank Rate / interest-rate / exchange-rate / yield-curve data source
- ICE: MPC Dated SONIA Futures, ECB Dated €STR Futures, SONIA / €STR STIR products
- MetaTrader 5 / MQL5 symbol properties: `SYMBOL_SWAP_LONG`, `SYMBOL_SWAP_SHORT`, weekday-specific swap multipliers, contract/volume properties
- BIS 2025 Triennial Survey: selected pair share EUR/USD 21.2%, USD/GBP 7.6%

これらは「source exists」の確認であり、market-data licensing / API entitlement / redistribution rightの確認を代替しない。

---

# 49. 最終設計原則

1. **Currency-first, Pair-second**
2. Directional stateとRisk/Gating stateを分離する
3. JPY口座ではすべてのriskをaccount currencyの`Money`へ明示変換し、risk domainで生conversion rateを扱わない
4. Conversion stalenessは使用時に判定し、stale時はrisk-increasing actionをfail-close
5. Exit / risk reductionはentry failureと分離する
6. Pair riskではなくcurrency factor riskを見る
7. Dynamic correlationよりstructural exposureを優先する
8. Strategyはraw candidateまで。Portfolioが同時signalを裁定する
9. 1 Strategy implementation × 4 pairs。pair cloneを作らない
10. normalized thresholdsをprimaryにする
11. broker capabilityとswap/rollover costをpairごとに実測 / discoveryしPIT保存する
12. minimum lotのためにrisk appetiteを上げない
13. platform capabilityとlive trading eligibilityを分離する
14. 4pairすべてを本番化することを成功条件にしない
15. 最終採用はportfolio marginal contributionで決める
16. SYSTEM_SPEC v1.3は凍結し、変更判断はADRへ残す
17. 上位Portfolio / Factor設計は将来の日本株BOTへ転用可能にする
18. GBP/EUR intelligenceはofficial-source/PIT/procurement Gateを通過するまでlive promotionしない
19. critical eventはdirect legだけでなくcross-pair propagationを安全側に扱う
20. currency scoreはPIT正規化・校正し、USD誤差感度を別途検証する
21. 2本目のlive pair前にSYSTEM_SPEC v2.0へnormative truthを統合する

---

# 50. Final State

完成形:

```text
                 Macro / News / Events
                           │
                           ↓
                Currency Intelligence
                USD / JPY / GBP / EUR
                           │
                           ↓
                   Pair Projection
                           │
        ┌──────────────────┼──────────────────┐
        ↓                  ↓                  ↓
      USDJPY      GBPUSD / EURUSD          GBPJPY
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ↓
                    Raw Candidates
                           │
                           ↓
                 Portfolio Arbitrator
            ┌──────────────┼──────────────┐
            ↓              ↓              ↓
       Stop Risk      Factor Exposure    Clusters
            │              │              │
            └──────────────┼──────────────┘
                           ↓
                       Risk Engine
            ┌──────────────┼──────────────┐
            ↓              ↓              ↓
     JPY Conversion    Event/Session   Broker Rules
            │              │              │
            └──────────────┼──────────────┘
                           ↓
                          OMS
                           │
                           ↓
                         MT5
                           │
                           ↓
                         OANDA
```

本プロジェクトの最終的な価値は、4ペアへ増やすことそのものではない。

**USDJPY専用BOTを、通貨ファクター・相関・ポートフォリオ全体を理解する取引プラットフォームへ進化させること**にある。

その上位設計は、将来の日本株BOTにおけるSector / Factor Exposure・Portfolio Arbitration・Validation Gateへ直接つながる。
