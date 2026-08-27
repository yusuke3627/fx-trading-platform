# ADR-019: macro 系列を factor 入力へ写す規約（M3 3/3）

**Status:** Accepted (2026-08-27)

## Context

ADR-018 の `normalize_series` は「1 factor につき 1 本の観測列」を受け取り、
その通貨自身の履歴に対する robust z を返す。一方 `macro/registry.py` の
canonical 系列は、公表元がそのまま出す形で揃っていない。

- 米 CPI は `index`（水準）だが、英 CPI とユーロ圏 HICP は `percent`（前年同月比）
- 失業率は「上がるほど通貨が弱い」。政策金利・利回りは逆
- リポジトリが返すのは vintage 連鎖で、1 観測期間に複数行が並ぶ

この差を吸収せずに繋ぐと `base - quote` の減算が意味を失う。本 ADR は
`data/factor_series.py` が担う写像の規約を決める。

## Decision

### 1. 水準系列は前年同期比へ落としてから渡す

`SeriesTransform` は `LEVEL` と `YEAR_OVER_YEAR` の 2 つ。

正規化は「直近値が自分の履歴のどこにいるか」を測るので、単調に伸びる系列を
そのまま入れると直近値が常に窓の最大側に来る。**動きが無くても強気、
ディスインフレ局面でも強気**という、符号が反転しないスコアになる。

前年同期比は「同じ観測期間ラベルの 1 年前」と組む。相手が欠測、または相手が
ゼロの期間は点を落とす — 補間すると、公表されていない値を観測として数える
ことになる。

### 2. 符号を「値が大きいほど通貨が強い」へ揃える

`FactorInput.sign` で反転する（失業率は `-1`）。揃えなければ、`base - quote`
の減算で GROWTH だけ逆向きに効く。

### 3. 1 観測期間につき初報 1 点へ畳む

vintage 連鎖から、各観測期間の**最初に届いた行**だけを採る。

採らなかった代替案は「now 時点で見えている最新 vintage」。PIT としては
どちらも正しいが、後者は古い期間の改定が known_at 最新の点になり、
`normalize_series` がそれを「直近値」として z を取る。2 年前の期間への改定が
現在の方向感として読まれることになる。

読み出し窓は `known_at` で切られるので、窓が開く前に初報が出た期間には改定
しか残らない。年次ベンチマーク改定のように古い期間へ遡る改定は、そのまま
では「その期間の唯一の vintage」として初報の顔をして入ってくる。**観測期間
が窓の開始より古い行は落とす**（`known_at` の窓と観測期間の窓を両方掛ける）。

### 4. 同一 known_at の並びは観測期間順に固定する

forward collector は初回収集で全履歴へ同じ取得時刻を `known_at` として付ける
（BOE / ONS / ECB / Eurostat）。Postgres は同時刻の行を UUID 順で返し、
`normalize_series` は `known_at` で安定ソートするので同時刻の並びは供給側の
順序がそのまま残る。並べ替えずに渡すと、**UUID 順の最後に来た任意の過去期間
が「直近値」になる**。初回収集の直後から次の公表まで（月次系列なら約 1 か月）
方向スコアが過去の値に基づくことになるため、供給側で `(known_at, 観測期間)`
順に固定する。

### 5. factor あたり系列は 1 本

複数系列を 1 factor へ合成すると、単位の違う値を 1 つの分布へ混ぜることに
なり、median / MAD が意味を失う。合成が要るなら `FactorSeriesSource` の
契約変更（factor ごとに複数列を返す）とセットで別途決める。

### 6. 埋まっていない組み合わせは空列を返す

`DEFAULT_FACTOR_INPUTS` に無い `(currency, factor)` は空列。CurrencyState 側で
合成から外れ、coverage の減点になる（ADR-018 §2）。現時点の欠測は 3 つ。

| 欠測 | 理由 |
| --- | --- |
| JPY の全 factor | macro 系列を持たない。BOJ 政策スコアと介入リスクが担う |
| USD の POLICY | FOMC 声明スコア（`EventRepository` 側） |
| GBP / EUR の RATES | OIS / イールドカーブ proxy の収集待ち（#59） |

この結果、USD と GBP / EUR は「5 factor 中 3 つ、しかも中身の違う 3 つ」から
合成される。confidence がその非対称を表すので score は歪めないが、非対称
自体は proxy collector と政策スコア供給元の合成で解消する。

### 7. 読み出し幅は正規化 window から導く

`(window + 12 + 前年同期比なら 1 年分)` を系列の頻度で年へ直す。定数で
持たない理由は、window を広げたときに lookback が追随せず、静かに
min_observations 未満へ落ちて factor が丸ごと欠測になるため。

`MacroFactorSeries` には `CurrencyStateService` と同じ `NormalizationConfig`
を渡す。

## スコープ外

- GBP / EUR の rates proxy collector（BOE OIS curve / ECB YC）
- 政策スコアを factor として供給する経路（USD / JPY の POLICY）
- USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）
- strategy への `CurrencyStateService` / `CurrencyRegimeSnapshot` 配線
