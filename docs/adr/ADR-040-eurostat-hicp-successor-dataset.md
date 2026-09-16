# ADR-040: ユーロ圏 HICP を後継 dataset `prc_hicp_minr` から取る

**Status:** Accepted (2026-09-17)

## Context

Eurostat が `prc_hicp_manr`（HICP - monthly data, annual rate of change）の配信を終了した。
statistics API の `updated` は 2026-02-06 で、返る期間は 1997-01 〜 2025-12 で打ち切られている
（Data Browser の表示も "(1997-2025)"）。

dissemination API は配信終了後も古い値を返し続けるため、collector は例外を出さない。
`EurostatCollector` は毎日同じ 2025-12 までの値を取得し、新しい vintage が無いものとして
何も保存しない状態が続いていた。結果として `ea_hicp_headline_yoy_nsa` は 2026 年の観測を
1 件も持たない（Mac の収集 DB で実測、2026-09-17）。EA の 5 系列の 1 つが丸ごと欠測しており、
#59 の調達 Gate が求める系列の網羅を満たせない。

後継は `prc_hicp_minr`（Harmonised index of consumer prices (HICP) - ECOICOP ver.2 -
indices and rates of change, monthly data）。2026-09-17 時点で `updated` は 2026-09-01、
2026-08 まで配信している。ECOICOP が ver.1 から ver.2 になったことで次元が変わっており、
分類の次元名は `coicop` から `coicop18`、総合のコードは `CP00` から `TOTAL` になる。

[SYSTEM_SPEC §5.1](../SYSTEM_SPEC.md#s5-1) の系列表は v2.0 発行時点の `prc_hicp_manr` / CP00 を
載せている。同文書は v2.0 で凍結されており、発行後の変更は本文改訂ではなく ADR に記録する運用
のため、本 ADR で移行を記録する。

## Decision

1. **`ea_hicp_headline_yoy_nsa` の取得元を `prc_hicp_minr` にする。** フィルタは
   `coicop18=TOTAL` / `unit=RCH_A`。geo 候補（EA21 / EA20 / EA）と「期間ごとに最新構成を採る」
   規則は変えない ―― 新 dataset は 3 つとも応答する。

2. **SYSTEM_SPEC §5.1 の系列表の当該行は本 ADR が置き換える。** 本文は改訂しない。

3. **外部ソースが識別子を付け替えた場合は、以後も本文改訂ではなく ADR で移行先を記録する。**
   ソース選定を ADR で決めた前例は [ADR-020](ADR-020-gbp-eur-rates-proxy.md)（GBP / EUR の金利系列）。

4. **canonical 系列名と PIT 分類は変えない。** `ea_hicp_headline_yoy_nsa` のまま、
   forward collection なので `PIT_UNVERIFIED`（[ADR-015](ADR-015-gbp-eur-official-data-acquisition.md)）。

## Consequences

- 重なる期間（2025-01 〜 2025-12、EA20）の前年比は新旧 12 か月すべて一致し、最大差 0.0 ポイント
  （実測 2026-09-17）。ECOICOP ver.1 から ver.2 への分類変更による段差は観測されていない。
  前年比を正本にする判断（基準改定に依存しない）は維持される。
- 重なる期間の値は同値のため、リポジトリの同値スキップにより重複行は増えない。
  次回の日次収集で 2026-01 〜 2026-08 が新規保存される。
- SYSTEM_SPEC §5.1 を読む者は、取得元の現況を知るために本 ADR まで辿る必要がある。
- **配信終了を自動で検知する仕組みは無い。** dissemination API が古い値を返し続ける限り、
  同じ見逃しは他の系列でも起こりうる。検知は #174 で追跡する。
