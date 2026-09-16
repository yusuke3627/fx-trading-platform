# issue #173: Eurostat HICP の dataset 差し替えに追従する

GitHub issue: #173（`gh issue view 173` で全文を読める）
ブランチ: `fix/issue-173-eurostat-hicp-dataset`

## 目的

ユーロ圏 HICP（`ea_hicp_headline_yoy_nsa`）が 2025-12 で止まり、2026 年の観測が 1 件も無い。
原因は Eurostat 側の dataset 差し替え。`src/trading/data/macro/eurostat.py` の `SERIES` が指す
`prc_hicp_manr` は配信終了（API の `updated` は 2026-02-06、期間は 1997-01〜2025-12）で、
全 geo が同じ古い値を返すため collector は例外を出さず、毎日「新しい vintage 無し」として
何も保存しない状態が続いていた。後継 dataset `prc_hicp_minr`（ECOICOP ver.2）へ切り替える。

## 実測（2026-09-17、Claude が確認済み。再取得は不要）

新 dataset の応答（`geo=EA21&geo=EA20&geo=EA&coicop18=TOTAL&unit=RCH_A&sinceTimePeriod=2024`）:

- 次元は `freq`(1) / `unit`(1) / `coicop18`(1) / `geo`(3) / `time`(32)
- 3 geo とも 2024-01〜2026-08 の 32 件。2026-08 は EA20 / EA21 が 3.2、EA が 3.3
- `_best_geo_values` の「geo と time 以外は size 1」の検査はこのフィルタで通る

新旧の重なり期間（EA20、2025-01〜2025-12）の比較:

- 旧 `prc_hicp_manr`（`coicop=CP00`）と新 `prc_hicp_minr`（`coicop18=TOTAL`）の前年比は
  12 か月すべて一致（最大差 0.0 ポイント）。値は 2.5 / 2.3 / 2.2 / 2.2 / 1.9 / 2.0 / 2.0 / 2.0 /
  2.2 / 2.1 / 2.1 / 2.0

| | 旧 `prc_hicp_manr` | 新 `prc_hicp_minr` |
| --- | --- | --- |
| 分類の次元名 | `coicop` | `coicop18` |
| 総合のコード | `CP00` | `TOTAL` |
| 単位 | `unit=RCH_A` | 同じ |
| 応答する geo | EA / EA20 / EA19 | EA / EA20 / EA21 |
| 期間 | 1997-01〜2025-12 で終端 | 2026-08 まで配信中 |

## 変更範囲

### 1. `src/trading/data/macro/eurostat.py`

- `SERIES[EA_HICP_HEADLINE_YOY_NSA]` を `("prc_hicp_minr", {"coicop18": "TOTAL", "unit": "RCH_A"})` に変える
- モジュール docstring の「HICP は EA20 のみ（実測 2026-08-26）」は新 dataset では誤り。
  「HICP（`prc_hicp_minr`）は EA / EA20 / EA21 の 3 つとも応答（実測 2026-09-17）」のように
  実態へ直す。docstring の主旨（geo コードが拡大のたびに切り替わり、dataset ごとに移行タイミングが
  違うので全候補 geo を並べて要求する）は維持する
- `GEO_CANDIDATES` と `_best_geo_values` は変えない
- 他の 2 系列（`EA_UNEMPLOYMENT_RATE_SA` / `EA_REAL_GDP_GROWTH_QOQ_SCA`）は触らない

### 2. `src/trading/data/macro/registry.py`

`UK_CPI_HEADLINE_YOY_NSA` の直上のコメント（現在は「指数は基準改定で系列が切れる（HICP は 2026-01 の
2025=100 移行で全 geo の指数系列が 2025-12 終端 — 実測 2026-08-26）が、前年比は基準に依存しない」）を
実態に合わせて直す。前年比の系列（`prc_hicp_manr`）も同じく 2025-12 で終端していた。切れたのは
基準改定そのものではなく dataset の差し替え（ECOICOP ver.1 → ver.2）。

書き換え後のコメントに含める内容:

- 前年比を正本にする判断は維持する（値が基準に依存しないのは事実。ADR-015）
- ソース側の dataset 差し替えは前年比でも系列を切るので、差し替えのときは移行先を追う必要がある
  （HICP は 2026-01 の ECOICOP ver.2 移行で `prc_hicp_manr` が 2025-12 終端 — 実測 2026-09-17）
- 新旧で分類体系（ECOICOP v1 の `CP00` と v2 の `TOTAL`）が違うため、重なる期間に段差があり得る。
  HICP は重なる 2025-01〜2025-12 の 12 か月で新旧の値が一致（実測 2026-09-17）

コメントは 5〜6 行程度に収め、既存の日本語コメントの文体（`# 〜。` の説明文）に合わせる。

### 3. `docs/SYSTEM_SPEC.md`（編集しない）

§5.1 の系列表にある `ea_hicp_headline_yoy_nsa` の行（「Eurostat statistics API、`prc_hicp_manr` の CP00」）は
実態と食い違うが、v2.0 で凍結された文書（変更は本文改訂ではなく ADR 追加）なので本 PR では編集しない。
食い違いは PR 本文の残課題に記し、扱いはユーザー判断に委ねる。`docs/adr/ADR-015-*.md` も決定時の記録なので
変更しない。

### 4. `tests/unit/test_gbp_eur_collectors.py`（Eurostat 節）

既存の fake transport（`tests/support.py` の `FakeTransport` / `FixedClock`）と `_jsonstat` ヘルパーの流儀で、
新 dataset とフィルタを固定するテストを足す:

- `collect(EA_HICP_HEADLINE_YOY_NSA, YEARS)` の要求 URL が `.../prc_hicp_minr` で、params に
  `coicop18 == "TOTAL"` と `unit == "RCH_A"` が入ること（`transport.get_calls[0]` で確認）
- 実際の応答と同じ形（`id` が `["freq", "unit", "coicop18", "geo", "time"]`、`coicop18` の size が 1）の
  ペイロードを渡し、`_best_geo_values` が落とさずデコードできること。複数 geo を入れる場合は
  既存の flat index の規約（最後の次元が最速で回る）に従って値を置く
- `_jsonstat` は `id` が固定なので、`coicop18` 次元を挿入できるよう任意引数で拡張するか、
  この 1 件だけペイロードを直接組み立てる。既存テストの呼び出しは変えない
- 既存の `test_eurostat_takes_older_composition_when_newest_is_absent` のコメント
  「（HICP の実測形）」は旧 dataset での観測なので「（旧 `prc_hicp_manr` の実測形）」に直してよい。
  アサーションは変えない
- 他 2 系列の既存テストは無変更で通ること
- 実ネットワークを叩くテストは足さない

## やらないこと

- 他の collector（BOE / ONS / ECB / ALFRED / BLS / BEA / Census / JGB）の変更
- `GEO_CANDIDATES` の変更、`_best_geo_values` のロジック変更
- DB に入っている旧データの削除・書き換え（重なる期間は直前の観測から値が変わる場合のみ、
  新しい known_at の観測として追記する。同じ値の再取得は既存の重複排除で追加しない）
- Mac の cron / `scripts/collect_daily.sh` の変更
- ADR の追加（dataset の移行はソース側の事情で、規範的決定ではない）
- `docs/SYSTEM_SPEC.md` の編集（v2.0 凍結。実態との食い違いは PR の残課題に記す）
- 配信終了の早期検知（issue の対応 5 番目）。公表ラグの扱いを決める必要があるので別 issue にする
- 周辺リファクタ・無関係な整形

## 参照する実装と規約

- `AGENTS.md`（正本。日本語、不変条件、テストルール、レビュー観点）
- `.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- `src/trading/data/macro/eurostat.py`、`src/trading/data/macro/registry.py`
- `tests/unit/test_gbp_eur_collectors.py`、`tests/support.py`
- `docs/adr/ADR-015-gbp-eur-official-data-acquisition.md`（背景。変更しない）

## 完了条件と検証

- `.venv/bin/ruff check .` が通る
- `.venv/bin/pytest tests/unit tests/replay tests/failure` が通る（`tests/broker` は対象外、
  `tests/integration` は対象外）
- 差分が上記の変更範囲に収まっている
- コミットはしない。変更ファイル、実行した検証と結果、未確認項目を報告する

## 未確定事項

- なし（設計は確定済み）
