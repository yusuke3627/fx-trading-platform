# ADR-020: GBP / EUR の RATES factor を公式カーブの 2 年点で賄う（M3 3/3）

**Status:** Accepted (2026-08-28)

## Context

#59 の policy-path 調達評価で、meeting-dated futures（ICE MPC Dated SONIA /
ICE・Eurex ECB Dated €STR）は上場が 2025 年以降で履歴が 1.5 年に満たず、
有料 entitlement を結んでも strict OOS 評価に足る深度が当面得られないと
判定した。M3 では無料の公式 proxy を使う。

ADR-019 で `MacroFactorSeries` は入ったが、GBP / EUR の RATES は供給元が
無く欠測のままだった。本 ADR はその供給元を決める。

## Decision

### 1. GBP は BOE の OIS spot カーブ、EUR は ECB の AAA ソブリンカーブ

| 通貨 | 系列 | 出所 |
| --- | --- | --- |
| GBP | `uk_ois_2y` | BOE yield curves（OIS spot、"4. spot curve" シート） |
| EUR | `ea_yield_curve_2y` | ECB Data Portal `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` |

ECB は OIS カーブを公表しないため、EUR は AAA ソブリンカーブになる。GBP の
OIS には無い信用・流動性プレミアムが乗るぶん、proxy としては GBP 側より
遠い。どちらも forward collection なので `known_at` は取得時刻、分類は
`PIT_UNVERIFIED`（ADR-015）。

### 2. 年限は 3 通貨とも 2 年で揃える

`base - quote` は同じ年限どうしでしか金利差にならない。既存の
`us_treasury_2y_yield` に合わせて 2 年点を採る。

**3 つとも同じ種類の道具である**ことが重要で、USD だけが特別扱いなのでは
ない。`us_treasury_2y_yield` もレジストリのコメントどおり「Fed の織り込み」
ではなくカーブ上の 1 点であり、期間・信用・成長・インフレの各プレミアムが
混ざっている。したがって GBP / EUR にだけ confidence の減点を掛ける理由は
無く、掛ければ逆に非対称を作る。

構成の違い（OIS か、ソブリンか）が減算へ漏れる分は、正規化が通貨ごとに
自分の履歴で z を取るため、**定常なプレミアムの差は打ち消える**。残るのは
プレミアム自体が動いた分で、それは実際に金利観の変化でもある。

`rates_score` の live 昇格 Gate は ADR-015 §5 のまま閉じている。本 ADR は
研究・backtest 用の入力を決めるもので、Gate を開けるものではない。将来
どれか 1 通貨にだけ真の meeting-path 系列を入れる場合は、そこで初めて
factor 単位の confidence 減点が必要になる。

### 3. BOE は配布ファイル 2 本を両方読む

BOE はイールドカーブ推計を zip 入り Excel でしか配布しない（統計ページに
CSV 経路は無い — 実測 2026-08-27）。配布は 2 本に割れており、履歴アーカイブ
（`oisddata.zip`）は前月末で終わり、当月ぶんは `latest-yield-curve-data.zip`
に載る（実測: アーカイブは 2026-07-31 まで、当月ファイルが 2026-08-03 以降）。
片方だけでは窓の途中に欠測が空くため両方を読み、日付で重ねて当月ファイルを
優先する。

アーカイブは年代で分割されている（`_2016 to 2024` / `_2025 to present` 等）
ので、要求年に重なる member だけを開く。当月 zip には GLC（名目・実質・
インフレ）カーブも同梱されるため、OIS の member 名で絞る — 取り違えると
通貨間の減算に別種のカーブが混ざる。

### 4. workbook 自体は raw event へ保存しない

他の collector は応答全文を events へ archive するが、アーカイブ zip だけで
11MB あり、日次で積むと桁が合わない。抽出した 2 年点の系列と、配布ファイル
の SHA-256 を残す。出所は辿れるが、parse バグを原本へ当て直す用途は
digest 照合までに留まる。

各観測は「その日を実際に載せていた配布ファイル」の URI と payload hash を
持つ。アーカイブ由来の日付に当月ファイルの hash を付けると出所が嘘になる。

### 5. openpyxl を依存に追加する

Excel を読む唯一の経路。xlsx を自前で解くのは共有文字列・スタイル・日付
シリアルを再実装することになる。

## 再評価トリガー

- M6 で GBP / EUR が live 昇格候補になったとき
- meeting-dated futures の履歴が 3 年を超えたとき（#59 の判定条件）

## スコープ外

- 政策スコアを factor として供給する経路（USD / JPY の POLICY）
- GBP / EUR の POLICY がステップ系列で欠測する問題（#84）
- USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）
- strategy への `CurrencyStateService` / `CurrencyRegimeSnapshot` 配線
