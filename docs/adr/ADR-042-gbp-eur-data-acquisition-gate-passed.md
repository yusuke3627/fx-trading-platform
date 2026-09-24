# ADR-042: GBP/EUR 公式データの調達 Gate（M2A）を通過とする

**Status:** Accepted (2026-09-24)

## Context

#59（M2A）は、GBP/EUR の CurrencyState を production で使う前提として、次の 3 つを Gate に置いた。

1. 必要な系列が揃っている
2. PIT の出所を検証できる
3. 市場織り込み（policy-path）のデータ源の決定が記録されている

[SYSTEM_SPEC §5.1](../SYSTEM_SPEC.md#s5-1) は、2026-08-26 の時点で euro area HICP が 2025-12 までしか取れず、
EUR のインフレが「production 昇格前に解消すべき gate として残っている」と書いている。同じ節は、
この過去の確認結果を現在の供給停止や gate 通過の証拠に置き換えないとも定めている。そのため、通過を
判断するには現在の実測が要る。SYSTEM_SPEC は v2.0 で凍結されているので、判定は本文を改訂せず ADR に記録する。

判定日までに、Gate に関わる変更が 2 つ入っている。

- [ADR-040](ADR-040-eurostat-hicp-successor-dataset.md): HICP の取得元を後継の `prc_hicp_minr` に切り替えた。
- #174（PR #183）: 配信が終わった系列を最新期間の古さで検知し、日次の収集を失敗させるようにした。

## 判定に使った実測（2026-09-24）

Mac の収集用 DB の `macro_observations` を集計した。直近の日次収集（2026-09-24 07:30 JST）では、
BOE・ONS・ECB・Eurostat のいずれも古さの検査で失敗していない。

| 系列 | 最新の観測期間 | 行数 | 最初の `known_at` |
| --- | --- | ---: | --- |
| `uk_bank_rate` | 2026-09-22 | 436 | 2026-08-27 |
| `uk_ois_2y` | 2026-09-22 | 436 | 2026-08-28 |
| `uk_cpi_headline_yoy_nsa` | 2026-08 | 20 | 2026-08-27 |
| `uk_unemployment_rate_sa` | 2026-06（5〜7 月の平均） | 18 | 2026-08-27 |
| `uk_real_gdp_growth_qoq_sa` | 2026Q2 | 6 | 2026-08-27 |
| `ea_deposit_facility_rate` | 2026-09-23 | 631 | 2026-08-27 |
| `ea_yield_curve_2y` | 2026-09-22 | 440 | 2026-08-28 |
| `ea_hicp_headline_yoy_nsa` | 2026-08 | 21 | 2026-08-27 |
| `ea_unemployment_rate_sa` | 2026-07 | 22 | 2026-08-27 |
| `ea_real_gdp_growth_qoq_sca` | 2026Q2 | 8 | 2026-08-27 |

ONS の失業率は 3 か月の移動平均に中央の月の名前を付ける（`"2026 MAY"` は 4〜6 月）。そのため
`2026-06` は 2026-09-15 に公表された 5〜7 月の値である。

## Decision

1. **M2A の調達 Gate は 3 つの条件をすべて満たしたので、通過とする。**
   - **系列の網羅**: §5.1 の 8 系列と §5.2 の 2 本のカーブのすべてに、2026 年の観測が入っている。
     §5.1 に残っていた EUR のインフレの欠けは、ADR-040 以後の収集で 2026-01〜2026-08 が保存され、
     解消した。§5.1 の「gate として残っている」という記述と、[§10.2](../SYSTEM_SPEC.md#s10-2) の
     「#59 の GBP/EUR データ調達は残る」という記述は、本 ADR が置き換える。本文は改訂しない。
     §10.2 の同じ文にある #57・#60・#66 の扱いは本 ADR では変えない。
   - **PIT の出所**: 上の 10 系列の 2,038 行すべてに `source_uri` と `payload_hash` がある。
     `known_at` はどれも収集開始（2026-08-27、カーブは 2026-08-28）以降である。収集開始前の履歴にも
     取得時刻が付いていて、公表時刻へ遡った `known_at` はない。§5.1 の `PIT_UNVERIFIED` の規則どおり
     保存されている。
   - **policy-path のデータ源**: [§5.2](../SYSTEM_SPEC.md#s5-2)（元は ADR-020）で決定済みである。
     研究と backtest の RATES 入力には公式の 2 年カーブを使う。meeting-dated futures は、履歴が
     1.5 年に満たず strict OOS に要る深度がないため調達しない。GBP / EUR が M6 の live 昇格候補に
     なったとき、または futures の履歴が 3 年を超えたときに再評価する。

2. **通過が許すのは、GBP/EUR の CurrencyState を M3 の production の入力として使うことまでである。**
   次の制限は解けない。
   - GBP / EUR の `rates_score` を live に上げる gate は、§5.2 のとおり閉じたままとする。
   - 非 USDJPY のペアを live に上げるには、[§10.2](../SYSTEM_SPEC.md#s10-2) の gate（OOS、Monte Carlo、
     shadow、demo forward、small live）を別に通る必要がある。
   - GBP / EUR の POLICY factor は供給されないままである。[§5.5](../SYSTEM_SPEC.md#s5-5) のとおり、POLICY の
     供給元は FED と BOJ の声明スコアだけで、BOE / ECB の声明採点は M6 以降の判断として採用していない。
     これは M2A の調達とは別の判断で、本 ADR の通過によって変わらない。

3. **GBP/EUR の factor の strict OOS と PIT 評価には、収集開始以降の期間しか使えない。**
   10 系列はすべて `PIT_UNVERIFIED` で、判定日の時点で使える forward の期間は 1 か月に満たない。
   GBP/EUR の factor に依存する判断を検証するには、forward のデータが貯まるのを待つ必要がある。
   EURUSD などの価格だけを使う検証は、この制約を受けない。

## Consequences

- #59 を閉じられる。M2A のために残った作業はない。
- 次に取得元が識別子を付け替えたり配信を終えたりしたときは、#174 の検知が日次の収集を止める。
  移行先は ADR-040 と同じく ADR に記録し、本 ADR の判定が引き続き成り立つかをそのとき確かめ直す。
- EURUSD の昇格（M7）で GBP/EUR の factor を使う戦略を評価する場合は、決定 3 のとおり、使える
  検証期間が収集開始以降に限られる。M7 の計画では、この期間の短さを前提にする。
