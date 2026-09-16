# issue #174: forward collector で配信終了した系列を早期に検知する

## issue 本文（転記）

**タイトル**: forward collector で配信終了した系列を早期に検知する（最新期間の古さで警告・失敗）

### 背景

#173 で分かったとおり、Eurostat の dissemination API は dataset の配信が終了しても古い値を返し続ける。collector（`src/trading/data/macro/eurostat.py`）は応答が空でない限り例外を出さないため、毎日同じ古い値を取得して「新しい vintage 無し」として何も保存しない状態が、旧 dataset の最終更新（2026-02）から 2026-09 の発見まで約 7 か月気付かれずに続いた。

### 期待する挙動

収集した最新期間が取得時点から見て一定以上古ければ、警告または失敗として検知する。

### 検討が必要な点

- 系列ごとの公表ラグ（月次は翌月、四半期は 2 か月など）を `src/trading/data/macro/registry.py` の `IndicatorSpec` に持たせる必要がある
- ラグを超えた「古さ」の閾値と、警告で止めるか失敗にするかの扱い
- Eurostat 以外の forward collector（BOE / ONS / ECB / JGB）にも同じ問題があるか

### 参照

- #173（発端。HICP の dataset 差し替え）
- `src/trading/data/macro/eurostat.py`、`src/trading/data/macro/registry.py`

---

## 設計方針

### 1. 検知の置き場所: collector CLI の収集後（全ソース共通）

すべての forward / vintage collector は `CollectionBatch`（`observations: tuple[EconomicObservation, ...]`）を
返し、`src/trading/data/macro/collector.py` の `main()` がソースを問わず同じループで
回収・保存している。したがって **バッチを保存し終えた直後の 1 か所**で全 8 ソース
（alfred / bls / bea / census / boe / boe_ois / ons / ecb / eurostat / jgb）に検知が効く。
各 collector をコピー修正する必要はない。

新モジュール `src/trading/data/macro/freshness.py` に純粋関数として置き、
`collector.py` から呼ぶ。`registry.py` は「どんな指標があるか」のデータ、
`freshness.py` は「どれだけ古かったら異常か」の方針、と役割を分ける。

### 2. 閾値は frequency から導く（1 系列だけ実測で override が必要）

`IndicatorSpec.frequency`（daily / monthly / quarterly）ごとの既定値:

| frequency | 既定の上限 | 根拠（Mac 収集 DB の実測、2026-09-17 時点） |
| --- | --- | --- |
| daily | 21 日 | 正常な日次系列の最大空白は jp_jgb_2y_yield の **11 日**（2019 年 GW の 10 連休、2015 年以降）。営業日系列（uk_ois_2y / uk_bank_rate / ea_yield_curve_2y）は復活祭で最大 5 日、us_treasury_2y_yield は 4 日。公表ラグ 1 営業日を足して最悪 12 日 → 21 日で 9 日の余裕 |
| monthly | 75 日 | 月次の最悪ケースは us_retail_sales_advance_sa と uk_cpi_headline_yoy_nsa（翌月中旬公表）で **48 日**、ea_unemployment_rate_sa（翌々月頭公表）で最大 **62 日** → 75 日で 13 日の余裕 |
| quarterly | 180 日 | 四半期の最悪ケースは ea_real_gdp_growth_qoq_sca（次の四半期の推計が載るまで最大 **約 150 日**）、uk_real_gdp_growth_qoq_sa（約 137 日）、us_real_gdp_growth_saar（約 121 日） → 180 日 |

**override が必要な系列は `uk_unemployment_rate_sa` の 1 本だけ**（実測で判明）。
ONS の失業率は LFS ローリング 3 か月平均で、ONS 自身が窓の**中央月**をラベルにする
（"2026 MAY" = APR-JUN 平均）ため、ラベルが実データより約 1 か月遅れる。
DB の実測では期間 `2026-06` が初めて現れたのが 2026-09-15 で、その直前
（2026-09-14）の最新期間は `2026-05` = **106 日前**。月次既定の 75 日では偽陽性になる。
→ `IndicatorSpec` に `max_staleness_days: int | None = None` を追加し、
この系列だけ **130 日**（実測 106 日 + 約 3 週の余裕）を指定する。
他の 17 系列は frequency 由来の既定値で足りる（下表）。

### 3. registry 全系列の実測と閾値判定（Mac 収集 DB、2026-09-17 時点）

`age_days` = 今日 − 最新 observation_period の期間終了日。

| series | freq | 最新期間 | age | 上限 | 判定 |
| --- | --- | --- | --- | --- | --- |
| ea_hicp_headline_yoy_nsa | monthly | 2025-12 | **260** | 75 | **STALE（検知される = #173 の状態）** |
| ea_real_gdp_growth_qoq_sca | quarterly | 2026Q2 | 79 | 180 | OK |
| us_real_gdp_growth_saar | quarterly | 2026Q2 | 79 | 180 | OK |
| uk_real_gdp_growth_qoq_sa | quarterly | 2026Q2 | 79 | 180 | OK |
| uk_unemployment_rate_sa | monthly | 2026-06 | 79 | **130（override）** | OK（既定 75 なら偽陽性） |
| us_retail_sales_advance_sa | monthly | 2026-07 | 48 | 75 | OK |
| ea_unemployment_rate_sa | monthly | 2026-07 | 48 | 75 | OK |
| uk_cpi_headline_yoy_nsa | monthly | 2026-07 | 48 | 75 | OK |
| us_cpi_core_sa | monthly | 2026-08 | 17 | 75 | OK |
| us_cpi_headline_sa | monthly | 2026-08 | 17 | 75 | OK |
| us_nonfarm_payrolls_sa | monthly | 2026-08 | 17 | 75 | OK |
| us_unemployment_rate_sa | monthly | 2026-08 | 17 | 75 | OK |
| jp_jgb_2y_yield | daily | 2026-09-11 | 6 | 21 | OK |
| us_treasury_2y_yield | daily | 2026-09-14 | 3 | 21 | OK |
| uk_ois_2y | daily | 2026-09-14 | 3 | 21 | OK |
| uk_bank_rate | daily | 2026-09-14 | 3 | 21 | OK |
| ea_yield_curve_2y | daily | 2026-09-14 | 3 | 21 | OK |
| ea_deposit_facility_rate | daily | 2026-09-15 | 2 | 21 | OK |

`ea_hicp_headline_yoy_nsa` の 260 日は #173 の旧 dataset（prc_hicp_manr）を掴んでいた
当時の DB 状態。#175 で後継 dataset（prc_hicp_minr / ECOICOP v2 TOTAL）へ切り替え済みで、
API を直接叩くと最新期間は **2026-08**（age 17 日）＝ この閾値では偽陽性にならない
（実測 2026-09-17）。**旧状態なら 75 日超で検知され、実際の発見（約 7 か月後）より
5 か月以上早く気付けた**ことになる。

### 4. 警告か失敗か → **失敗（SystemExit）**にする

判断理由:

- `collector.py` は既に境界の異常（DSN 未設定、Eurostat の空応答、ECB の dataSets 欠落など）を
  `SystemExit` / `ValueError` で落とす方針。配信終了は同じ「データ源の契約が壊れた」側の事象で、
  警告に落とすと既存の失敗の扱いと不整合になる
- `scripts/collect_daily.sh` は `for source_name in ...` で **1 ソースの失敗が他ソースを止めない**
  作りになっており（`failed=1` を立てて最後に `exit 1`）、1 系列の古さで日次 cron 全体が
  止まることはない。失敗にしても収集は続く
- 検知は**全バッチを保存し終えた後**に行うので、その回に取得できた観測は失われない
- #173 の失敗モードは「ログに何も出ないまま 7 か月」。ログ行を 1 本足すだけの警告は
  同じく誰も読まないおそれがある。non-zero exit なら cron の失敗として残る

### 5. 実装詳細

**`src/trading/data/macro/registry.py`**

- `IndicatorSpec` に `max_staleness_days: int | None = None` を追加（frequency 由来の既定値の override）。
  コメントで「ONS ローリング 3 か月ラベルのように公表ラグが frequency から導けない系列だけ指定する」旨と実測値を残す
- `UK_UNEMPLOYMENT_RATE_SA` の spec に `max_staleness_days=130` を指定する
- 他の spec は変更しない（系列の追加・削除はしない）

**`src/trading/data/macro/freshness.py`（新規）**

- `STALENESS_LIMIT_DAYS: dict[str, int] = {"daily": 21, "monthly": 75, "quarterly": 180}`
- `staleness_limit_days(spec: IndicatorSpec) -> int`: `spec.max_staleness_days` があればそれ、無ければ frequency の既定値
- `period_end(period: str) -> date`: `registry.period_from_date` の逆。
  `"2026-09-14"` → その日、`"2026-07"` → 月末、`"2026Q2"` → 四半期末
- `merge_latest_periods(latest: Mapping[str, str], observations: Iterable[EconomicObservation]) -> dict[str, str]`:
  series -> 最新 observation_period を新しい dict で返す（引数を破壊しない）。比較は `period_end` で行う
- `StaleSeries`（frozen pydantic model）: `series` / `latest_period` / `age_days` / `limit_days`
- `stale_series(latest: Mapping[str, str], now: datetime) -> tuple[StaleSeries, ...]`:
  `age_days = (now.date() - period_end(period)).days` が上限を**超える**ものを series 名順で返す

**`src/trading/data/macro/collector.py`**

- `main()` のバッチループで `latest = merge_latest_periods(latest, batch.observations)` を積む
- ループ後、既存の `print(f"{args.source}: parsed ... stored ...")` を先に出してから
  `stale_series(latest, clock.now())` を評価し、空でなければ系列ごとに
  「最新期間 / 経過日数 / 上限」を並べた `SystemExit` を送出する（メッセージは既存に合わせて英語）

### 6. テスト（`tests/unit/test_series_freshness.py` 新規、外部 API を叩かない）

1. 新鮮な系列は検知されない（daily / monthly / quarterly を 1 本ずつ、上限**ちょうど**の age で通ること＝境界値）
2. 上限を 1 日超えた系列が frequency ごとに検知される（daily / monthly / quarterly 各 1 本）
3. `uk_unemployment_rate_sa` は 106 日（実測の正常値）で検知されず、同じ age の他の月次系列は検知される（override の効果）
4. `merge_latest_periods` が複数バッチ・順不同の観測から最新期間を選び、引数の dict を破壊しない
5. `period_end` が daily / monthly / quarterly の期間文字列を正しい終了日に変換する

既存テスト（`tests/unit/test_gbp_eur_collectors.py` など）の `FakeTransport` / `FixedClock` の流儀に合わせ、
実在の人物・団体名は使わない。

### 7. やらないこと

- registry の系列追加・削除、新しい collector の追加
- `migrations/` の変更（このチェックは収集時の判定だけで、永続化しない）
- `docs/SYSTEM_SPEC.md` の本文改訂（v2.0 凍結）
- 周辺リファクタ・無関係な整形
- VPS 上での実行や検証（別ジョブが走行中のため一切触らない）

## 完了条件

- `.venv/bin/ruff check .` 無指摘
- `.venv/bin/pytest tests/unit tests/replay tests/failure` green
