# ADR-021: POLICY factor は中銀声明スコアの尺度をそのまま使う（M3 3/3）

**Status:** Accepted (2026-08-28)

## Context

ADR-018 は全 factor を `normalize_series`（通貨ごとの rolling robust z）に
通してから合成すると決めた。`base - quote` の減算が意味を持つのは両辺が
同一尺度に載っているときだけ、というのがその理由。

POLICY factor の供給元を決めるにあたって、この規則が **中銀の声明スコアには
当てはまらない**ことが分かった。スコアは `data/policy/scoring.py` の配点表
（利上げ +2 / 利下げ -2 / 反対票 ±0.5 …）で採り、**同じ配点表を BOJ にも FED
にも適用している**。つまり最初から通貨横断で校正されている。

## Decision

### 1. 既に共通尺度に載っている factor は正規化せず、上限で割る

`CurrencyScoreConfig.bounded_factors` に載る factor は
`normalization.bounded_score` を通す。POLICY の上限は 2.0（`SCORE_MAX`）。

正規化を掛けると **中銀間の乖離が消える**。全会合で利上げしている中銀と全
会合で利下げしている中銀を考えると:

- スコアが一定なら MAD = 0 で `normalize_series` は None を返す（語れない）
- ばらつきがあっても、直近値は「その中銀にしては普通」の z ≈ 0 へ潰れる

どちらの場合も `USD_policy - JPY_policy ≈ 0` になるが、実態は最大の政策
乖離である。通貨ごとの正規化は「自分の履歴に対する相対位置」を測る道具で、
**絶対的な意味が定義済みの尺度に使うと情報を壊す**。

観測数の下限も置かない。1 回の声明はそれ自体で完結した stance の読み取りで、
分布を語るための標本ではない。会合は年 8 回しかないので、
`min_observations = 20` を課すと 2.5 年ぶん貯まるまで POLICY は永久に欠測
になる（実データは現在 FED / BOJ とも 17 回、いずれも下限未満）。

上限は factor の性質であって通貨ごとの設定ではない。POLICY は「中銀声明
スコア」と定義されるので、どの通貨でも同じ上限が掛かる。

### 2. POLICY の供給元は中銀声明スコアだけ

`PolicyScoreFactorSeries` が `EventRepository` から読む。採点が定義されて
いるのは FED / BOJ だけ（`scoring.EVENT_TYPES`）なので、**GBP / EUR の POLICY
は欠測**になる。

政策金利の水準（`uk_bank_rate` / `ea_deposit_facility_rate`）を代わりに置く
案は採らない（#84）。理由は 2 つ。

- 日次のステップ系列なので、据え置き期間は窓が同値で埋まり `MAD = 0` に
  なる。ほぼ常時欠測で、たまに値が出るのは利上げ直後の 1 日前後だけ
- 金利パスの情報は RATES の 2 年点（ADR-020）が既に持っている

**発火しない分岐を置くより、欠測を欠測として持つほうが正しい。** coverage の
減点として confidence に現れ、GBP / EUR の状態が USD / JPY より不確かだという
事実がそのまま出る。BOE / ECB の声明採点を足すのは M6 以降の判断。

系列自体の収集は続ける（レジストリにも collector にも残す）。研究用途と、
将来の BOE / ECB 採点の材料になる。

### 3. 供給元は `ChainedFactorSeries` で束ねる

factor によって出所が違う（macro リポジトリと中銀声明）ので、最初に観測を
返した供給元を採る。各供給元は互いに素な `(currency, factor)` を担当する
前提で、重なりは設定の誤りとして先勝ちに倒す。

## 現時点の factor 充足

| | USD | JPY | GBP | EUR |
| --- | --- | --- | --- | --- |
| POLICY | FOMC 声明スコア | BOJ 声明スコア | — | — |
| GROWTH | 失業率 | — | 失業率 | 失業率 |
| INFLATION | CPI 前年比 | — | CPI 前年比 | HICP 前年比 |
| RATES | 米 2Y 利回り | — | 英 OIS 2Y | ユーロ圏 AAA 2Y |
| RISK_SENTIMENT | — | — | — | — |

JPY は macro 系列を持たない（声明スコアと介入リスクが担う）。
RISK_SENTIMENT は全通貨で未供給。

## スコープ外

- BOE / ECB の声明採点（GBP / EUR の POLICY を埋める）
- RISK_SENTIMENT の供給元
- confidence の freshness が公表間隔に追随しない件（#89）
- USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）
- strategy への `CurrencyStateService` / `CurrencyRegimeSnapshot` 配線
