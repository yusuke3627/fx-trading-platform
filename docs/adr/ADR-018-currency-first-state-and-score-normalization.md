# ADR-018: Currency-first state とスコア正規化（M3 2/3）

**Status:** Accepted (2026-08-27)

## Decision

方向感を通貨単位で持ち、ペアはその差として導く（設計書 v2.1 §12–13 /
34.5A）。

### 1. スコア正規化は PIT rolling robust

`intelligence/normalization.py`:

```text
raw → rolling median / MAD の z → clip(±clip_sigma) → tanh → [-1, 1]
```

- **統計は `known_at <= now` の window 内だけで計算する。** 全期間で fit
  したパラメータを過去へ適用するのは look-ahead
- 観測数不足（`min_observations` 未満）と MAD = 0（散らばり無し）は
  **None を返す**。0 に潰すと「中立という観測がある」ことになり、観測が
  無いことと区別できなくなる
- clip + tanh で有界化し、1 通貨のデータ異常が portfolio の方向感を
  支配しないようにする。MAD ベースなので単位や振れ幅が違う系列
  （% と bp）も同じ相対位置なら同じスコアになり、`base - quote` の
  前提が成立する
- **walk-forward calibration to forward risk-normalized return は範囲外**。
  forward return の紐付けは research 側の校正で、本モジュールは「比較
  可能な尺度へ載せる」までを担う

### 2. CurrencyState は score と confidence を分離する

- `directional_score` は利用可能な factor の加重平均。欠測 factor は
  合成から外すだけで残りを薄めない — 薄めると「データが少ないほど中立に
  見える」ことになり、方向感の欠如と観測の欠如が混ざる
- `confidence = coverage × freshness`（score magnitude と独立）。coverage
  不足で **score を膨らませず confidence を下げる**（設計書 §12.2A）
- 個別 factor score は `Decimal | None`。欠測は None で持つ

### 3. PairState の confidence は弱い leg + 打ち消し減点

```text
directional_score = base.directional_score - quote.directional_score
confidence = min(base.conf, quote.conf) × (1 − cancellation × penalty)
cancellation = (|b| + |q| − |b − q|) / (|b| + |q|)
```

単純平均にしないのは、片 leg が観測不足ならペアの差もその不確かさを
引き継ぐため。加えて両 leg が同方向に強いときの差（双方 hawkish で net が
小さい）は大きな値どうしの引き算で、同じ絶対値の差でも相対的な不確かさが
大きい。異符号なら `cancellation = 0` で減点は掛からない。

`known_at` は両 leg の古い方 — ペアは遅れている leg の分しか語れない。

### 4. PairState に event risk を載せない

設計書 §12.2 の `PairState` は `event_risk` フィールドを持つが、通貨
scope 付きの event gate は `EventRiskCalendar.mode_for_instrument`
（ADR-017）が既に担っている。同じ判断を二箇所に持たせると、片方だけ
更新されたときに「gate は通るが state は halt」の不整合が起きる。
directional state と risk state の分離（設計書 §12.3）にも沿う。

### 5. Regime は通貨別と global の二層

`CurrencyRegimeSnapshot(by_currency, global_regimes, known_at)`。
"USD が hawkish" は通貨の性質、"global risk-off" は全通貨に同時に掛かる
状態で、片方へ畳むと 4 通貨で意味が壊れる。`active(currency)` は
`by_currency[currency] | global_regimes`。

`RegimeLabel.RISK_OFF` は `GLOBAL_RISK_OFF` へ改名（設計書 §13 の命名。
消費箇所は無く、挙動への影響なし）。

**通貨別ルールは供給されている feature を持つ通貨だけ定義する**（現状
USD / JPY）。GBP / EUR の policy score は M2A（#59）の Gate 待ちで、
ルールだけ置いても永久に発火しない死んだ分岐になる。

## スコープ外（M3 3/3）

- `FactorSeriesSource` の PIT リポジトリ実装（現状はメモリ供給の
  `MappingFactorSeries` のみ。既存 `StoredFeatureSource` は「現在値」の
  store で、正規化に要る観測列を返さない）
- GBP / EUR の rates proxy collector（BOE OIS curve / ECB YC、#59 の
  policy-path 判定に基づく）
- USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）
- strategy への `CurrencyStateService` / `CurrencyRegimeSnapshot` 配線
