# ADR-015: GBP/EUR 公式データ調達と PIT 方針（M2A Gate）

**Status:** Accepted (2026-08-27)

## Decision

GBP/EUR の CurrencyState（M3）に先立ち、公式ソースからの forward collection を
確立する（設計書 v2.1 §12.0 / M2A）。ソースと系列は次のとおり。

| 系列 | ソース | 経路 |
| --- | --- | --- |
| `uk_bank_rate` | BOE | IADB CSV（`IUDBEDR`、日次） |
| `uk_cpi_headline_yoy_nsa` | ONS | website timeseries JSON（`D7G7`/MM23） |
| `uk_unemployment_rate_sa` | ONS | 同上（`MGSX`/LMS、LFS ローリング3ヶ月） |
| `uk_real_gdp_growth_qoq_sa` | ONS | 同上（`IHYQ`/PN2、四半期） |
| `ea_deposit_facility_rate` | ECB Data Portal | SDMX-JSON（`FM/D.U2.EUR.4F.KR.DFR.LEV`） |
| `ea_hicp_headline_yoy_nsa` | Eurostat | statistics API（`prc_hicp_manr` CP00） |
| `ea_unemployment_rate_sa` | Eurostat | statistics API（`une_rt_m`） |
| `ea_real_gdp_growth_qoq_sca` | Eurostat | statistics API（`namq_10_gdp` B1GQ） |

全系列 API キー不要。transport は名乗る User-Agent を送る（BOE/ONS は
urllib 既定 UA を 403 で遮断する — 実測 2026-08-26）。

## 個別の決定と根拠

### 1. インフレは指数でなく前年比を正本にする

HICP は 2026-01 の基準改定（2015=100 → 2025=100）で指数系列が全 geo で
2025-12 終端になっている（Eurostat・ECB とも実測）。指数を保存すると基準
改定のたびに系列が切れるが、前年比は基準に依存しない。UK CPI も同じ理由で
`D7G7`（前年比）を採る。指数ベースの feature（momentum 等）が必要になったら
基準継ぎの vintage 管理とセットで別 ADR。

### 2. ONS は廃止済み API でなく website timeseries JSON を使う

`api.ons.gov.uk` の time-series API は 2024-11-25 に廃止済み（実測。設計書
v2.1 の「beta API 前提」は古い）。代替の
`www.ons.gov.uk/{topic}/timeseries/{cdid}/{dataset}/data` は文書化された契約
ではないため、(a) 依存フィールドのみ検査する薄いパーサ、(b) raw payload の
全量アーカイブ、(c) 契約テスト（fixture は実応答の形）で breaking change に
備える。

### 3. Eurostat の geo は候補列で吸収する

固定構成コードは拡大のたびに切り替わり（EA20 → EA21）、dataset ごとに移行
時期が違う（GDP は両方 / 失業率は EA21 のみ / HICP は EA20 のみ — 実測
2026-08-26）。collector は毎回 `EA21, EA20, EA` を並べて要求し、期間ごとに
値が実在する最新構成を採用する。全候補が空なら fail-loud（黙って 0 件保存に
しない）。ECB 側は U2（変動構成）なのでこの問題がない。

### 4. PIT 分類: `PIT_UNVERIFIED`

上記 8 系列はいずれも「最新値のみ」を返すソースで、収集開始前の履歴を真の
vintage として復元できない。`IndicatorSpec.pit_classification` に
`PIT_UNVERIFIED` を刻み、次を規範とする:

- forward snapshot（known_at = 取得時刻）だけが PIT として有効
- 過去履歴をバックフィルしても release 時刻の known_at を与えてはならない
- strict OOS / PIT 評価は、これらの系列の収集開始前の期間を除外する

US 系列は ALFRED の vintage アーカイブで履歴を裏付けられるため
`PIT_VERIFIED`。

### 5. policy-path（市場織り込み）調達 Gate は未通過

ICE MPC Dated SONIA futures / ECB Dated €STR futures の entitlement・履歴
深度・latency・license・cost の比較評価は本 ADR と別に記録する（issue #59）。
**Gate 通過まで GBP/EUR の `rates_score` の live 昇格を禁止する。** official
policy rate / yield curve での代替は可能だが同一 feature とはみなさず、
coverage 不足として confidence を下げる（設計書 §12.0）。

### 6. スコープ外（M3 へ送る）

- BOE MPC / ECB 政策理事会の会合ファクト（票割れ・声明）の採点接続と
  会合カレンダーの currency-scoped event risk への登録は M3（#61）。
  現行の `policy_meetings.yaml` は BOJ/FED の USDJPY halt 窓と結合して
  いるため、bank 追加は currency-scope の設計とセットで行う
- Eurostat flash HICP（速報）系列の追加

## Coverage 所見（Gate 判定の入力・2026-08-26 実測）

- euro area HICP: 全 geo・全経路（Eurostat 指数/前年比、ECB ICP）で
  2025-12 終端。2026 年分の機械可読な集計値の所在が未確認 → **EUR の
  inflation coverage は Gate 未達**。M3 の production 昇格前に要解消
- UK: CPI 2026-07 / 失業率 2026-05（APR-JUN） / GDP 2026-Q2 / Bank Rate
  日次 — 取得可能を確認
- EA: 失業率 2026-07 / GDP 2026-Q2 / DFR 日次 — 取得可能を確認
