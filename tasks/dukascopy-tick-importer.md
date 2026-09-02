# Dukascopy 歴史 tick インポーター

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットもしない**（コミットと PR は別担当）。

## 目的・背景

研究（介入イベントスタディ）は `market_ticks` の USD/JPY 系列を単一系列として読む。
MT5 由来の tick は 2024-07-23 以降しかなく、2022 年介入 3 件・2024 年介入 4 件が
カバーできない。また 2026-01-23〜2026-04-08 に収集欠損がある。
Dukascopy の無償歴史 tick（bi5）でこの 2 区間を埋める。

対象範囲（PR 本文・運用で使う値。コードにはハードコードしない）:
- USDJPY 2022-01-01〜2024-07-22
- USDJPY 2026-01-23〜2026-04-08

## 要件（全文）

- 新規 CLI: `python -m trading.data.market.dukascopy --env demo --symbol USDJPY --since <UTC> --until <UTC>`
  （既存 collector の CLI 慣習に合わせる。下記「参考実装」参照）
- データ源: `https://datafeed.dukascopy.com/datafeed/<SYMBOL>/<YYYY>/<MM>/<DD>/<HH>h_ticks.bi5`。
  **URL の月は 0 始まり**（7 月 → `06`）。LZMA 圧縮、1 レコード 20 バイト big-endian:
  `(msec offset in hour: u32, ask point: u32, bid point: u32, ask_vol: f32, bid_vol: f32)`。
  USDJPY の point スケールは 1/1000。スケールは通貨ペア依存なのでハードコードせず
  モジュール定数テーブル `{symbol: Decimal}` を経由する（InstrumentSpec は broker 由来の
  必須フィールドが多く、ここでは構築できないため定数テーブルを採用）
- 保存: `market_ticks` へ `insert_many(..., source="DUKASCOPY", ingestion_run=<プロセスごと uuid4>)`。
  **スキーマ変更なし。migrations も storage も変更しない**
- **ソース混在の禁止（最重要）**: 研究コードは symbol の全 tick を単一系列として畳むため、
  同一期間に MT5 tick と Dukascopy tick が混ざると二重カウントになる。取り込み前に対象
  範囲の既存 tick 有無を確認し、**既存 tick がある時間帯はスキップする**。境界
  （2024-07-22→23、2026-01-23/04-08 前後）の挙動をテストで固定する
- 冪等・再開可能: 既存データ確認でスキップし、途中でプロセスを殺しても再実行で継続できる
- 進捗行（日次）を **stderr** へ出す。週末・休場の 404 / 空ボディは正常（エラーにしない）。
  transient エラーはリトライ、リトライしても失敗する時間帯はログして先へ進む
- ダウンロードは逐次 + 軽い間隔（並列取得しない）
- テスト: bi5 デコード（合成フィクスチャ）、スケーリングと Decimal 化、重複範囲スキップ、
  再開スキップ、URL 生成（0 始まり月）。**fetch は注入可能にし、ネットワークテストは書かない**

## 外部仕様の実測結果（2026-09-02、このリポジトリの Mac で実測済み）

計画立案時に実 URL を叩いて確認した事実。実装はこの実測に合わせる:

- `https://datafeed.dukascopy.com/datafeed/USDJPY/2024/06/11/12h_ticks.bi5`
  （= 2024-07-11 12:00 UTC。**URL の月 06 が 7 月**）→ HTTP 200、94,924 バイト
- 先頭バイト `5d 00 00 40 00 ...` = LZMA alone フォーマット。Python 標準の
  `lzma.decompress()` がそのまま解ける（フォーマット自動判別）
- 解凍後 413,540 バイト、20 で割り切れ、20,677 レコード。`struct.unpack(">IIIff", ...)` で
  - レコード 0: `ms=107, ask=161541, bid=161538, ask_vol=1.2, bid_vol=1.2`
  - 最終レコード: `ms=3599992, ask=158659, bid=158638`
  - 1/1000 スケールで ask=161.541 / bid=161.538 → 当時の USD/JPY 実勢（161 円台、
    同時間帯の米 CPI→介入で 158 円台へ急落）と整合。**ask が先、bid が後**の並びで確定
- 週末（土曜）`.../2024/06/13/12h_ticks.bi5` → **HTTP 200 + 0 バイト**（空ボディが正常応答）
- 存在しない日付 `.../2030/00/01/12h_ticks.bi5` → **HTTP 404**（週末・休場も 404 の場合がある
  との報告があるため、404 と空ボディの両方を「tick なし、正常」と扱う）
- 実測中に **TLS 接続リセット（`Recv failure: Connection reset by peer`）が 1 回発生**し、
  数秒後の再試行で成功した。transient リトライは実際に必要

## 実装

### 新規ファイル 1: `src/trading/data/market/dukascopy.py`（本体、200〜350 行目安）

モジュール構成（既存 `src/trading/data/market/collector.py` の書き方に合わせる）:

```python
"""Dukascopy 歴史 tick インポーター。（モジュール docstring に目的・Usage を書く）"""
from __future__ import annotations
# 標準ライブラリのみ: argparse, lzma, struct, sys, time, urllib.request, urllib.error,
# collections.abc, datetime, decimal, typing, uuid

DATAFEED_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD_FORMAT = ">IIIff"   # (ms offset, ask point, bid point, ask_vol, bid_vol)
RECORD_SIZE = 20
# Dukascopy の point 単位は通貨ペア依存（JPY クロスは 1/1000、多くの他ペアは 1/100000）。
# 対応ペアを増やすときはここに追記する。
POINT_SCALES: dict[str, Decimal] = {"USDJPY": Decimal("0.001")}
SOURCE_DUKASCOPY = "DUKASCOPY"
FETCH_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 5.0
REQUEST_INTERVAL_SECONDS = 0.1
```

関数・クラス:

1. `def hour_url(symbol: str, hour_start: datetime) -> str`
   - `f"{DATAFEED_URL}/{symbol}/{hour_start.year:04d}/{hour_start.month - 1:02d}/{hour_start.day:02d}/{hour_start.hour:02d}h_ticks.bi5"`
2. `def decode_bi5(payload: bytes, symbol: str, hour_start: datetime, received_at: datetime) -> list[Tick]`
   - 空 `bytes` → `[]`（週末の HTTP 200 + 空ボディ）
   - `lzma.decompress(payload)`。解凍後の長さが 20 で割り切れなければ `ValueError`
     （外部データの境界検証。これ以外の防御的検証は足さない）
   - `struct.iter_unpack(RECORD_FORMAT, data)` で
     `time = hour_start + timedelta(milliseconds=ms)`、
     `bid = Decimal(bid_point) * scale`、`ask = Decimal(ask_point) * scale`
     （scale は `POINT_SCALES[symbol]`。**float を経由しない**）
   - volume 2 フィールドは読み捨てる（`market_ticks` に volume 列はない）
   - `Tick(symbol=..., bid=..., ask=..., time=..., received_at=received_at)` を返す
3. `def default_fetch(url: str) -> bytes | None`
   - `urllib.request.urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30)`
     で読む。`urllib.error.HTTPError` の code 404 は `None` を返す（正常）。
     それ以外の例外はそのまま送出（リトライ判断は呼び出し側）
   - User-Agent は `src/trading/data/macro/http.py:19` と同じ思想で
     `"fx-trading-platform-collector/1.0"` を送る（このモジュール内に定数でよい）
4. `class DukascopyTickImporter`
   - `__init__(self, repository: MarketTickRepository, *, fetch: Callable[[str], bytes | None] = default_fetch, clock: Clock | None = None, sleep: Callable[[float], None] = time.sleep)`
   - `clock` は `trading.backtest.clock.SystemClock` を既定にする（`datetime.now()` を
     直接呼ばない。既存 collector と同じ注入パターン: `src/trading/data/market/collector.py:125-140`）
   - `ingestion_run = uuid4()` をプロセス（インスタンス）ごとに 1 つ
   - `import_range(self, symbol: str, since: datetime, until: datetime) -> tuple[int, int]`
     を公開メソッドにし、`(stored 件数, 失敗した時間帯の数)` を返す

`import_range` のループ（冪等性・ソース混在防止の要）:

- UTC 日単位で外側ループ、時間単位で内側ループ。時間 `h` の対象窓は
  `[max(h, since), min(h+1h, until))`
- **日レベルの先行チェック**: その日の対象窓に `repository.bounds_between(symbol, start, end)`
  （`src/trading/storage/repository.py:151`、実装 `src/trading/storage/postgres.py:591`）
  が `None` 以外を返したら、その日は**時間レベルのチェックに落とす**。`None` なら
  その日の全時間を無条件で取り込む（既存 tick が 1 本もない日）
- **時間レベルのチェック**: 時間窓に既存 tick があればその時間はスキップ（fetch しない）。
  なければ fetch → decode → `[since, until)` で tick をフィルタ → **その時間分を 1 回の
  `insert_many` 呼び出しで書く**。`PostgresMarketTickRepository.insert_many`
  （`src/trading/storage/postgres.py:358`）は executemany + commit が 1 呼び出し 1 コミット
  なので、時間単位 1 呼び出しにすることで途中で殺されても「時間まるごと入った / 入らない」
  のどちらかになり、再実行の既存チェックがそのまま再開点になる。**チャンク分割はしない**
  （分割すると原子性が壊れて再開が二重になる）
- `received_at` は**時間帯の fetch ごと**に `clock.now()` を取る（MT5 backfill が window ごとに
  stamp するのと同じ: `src/trading/data/market/collector.py:186`。run 開始時刻を全 tick に
  使い回さない — 数時間走る import で reception time が数時間ずれる）
- fetch のリトライ: 例外は `FETCH_ATTEMPTS` 回まで `RETRY_WAIT_SECONDS` 待って再試行
  （`sleep` 注入を使う）。使い切ったら stderr に 1 行ログして**その時間帯は諦めて先へ**
  （失敗カウントを増やす）。失敗した時間帯は tick が入らないままなので、後日の再実行が
  自動的に埋め直す
- 各リクエストの間に `sleep(REQUEST_INTERVAL_SECONDS)`（逐次・軽い間隔）
- 進捗: 日ごとに 1 行、**stderr** へ
  `print(f"{day:%Y-%m-%d}: {fetched} ticks, +{stored} new", file=sys.stderr, flush=True)`
  （書式は MT5 backfill の進捗行 `src/trading/data/market/collector.py:198-201` に合わせる。
  stderr に出すのは PR #83 = commit 3e043cc と同じ理由: stdout をリダイレクトしても
  進捗が端末に残る）。日全体をスキップした場合も
  `f"{day:%Y-%m-%d}: skip (existing ticks)"` のように分かる行を出す

`main()`（`src/trading/data/market/collector.py:215-257` の main を踏襲）:

- argparse: `--env`(default "demo") / `--symbol`(default None → `config.market.primary_instruments[0]`) /
  `--since` / `--until`（両方必須、型は `trading.data.cli.aware_utc`
  = `src/trading/data/cli.py:9`。naive を拒否して UTC へ正規化する既存ヘルパー）
- `--since >= --until` は `parser.error`。symbol が `POINT_SCALES` になければ `parser.error`
  （境界での検証はここまで。内部関数に防御分岐を足さない）
- `config = load_config(args.env)`、DSN は `os.environ.get(config.storage.dsn_env)`、
  未設定なら `SystemExit`（collector.py:235-237 と同一パターン）
- psycopg import は main 内に遅延 import（collector.py:239-241 と同じ理由: db extra なしで
  モジュールを unit テスト可能に保つ）
- 実行後: stdout に合計を出す（例 `imported 1234567 ticks`）。失敗時間帯が 1 つでもあれば
  その旨も出して **exit code 1**（オペレーターが日次ログを読まなくても失敗に気付ける。
  再実行すれば失敗分だけ埋まる）
- `if __name__ == "__main__": main()` を付ける（`python -m trading.data.market.dukascopy` 用）

### 新規ファイル 2: `tests/unit/test_dukascopy_importer.py`

書き方は `tests/unit/test_tick_collector.py` に合わせる（FakeMT5/FakeTickRepository の要領。
`FixedClock` は `tests/support.py:27` にある）。ネットワークには一切出ない。

Fake:

- `FakeTickRepository`: `insert_many(ticks, *, source, ingestion_run)` は受け取った tick を
  蓄積して件数を返し、`bounds_between(symbol, start, end)` は**蓄積済み + 事前投入済み** tick
  から `[start, end)` の (最初, 最後) を返す（なければ `None`）。事前投入で「既存 MT5 tick」
  「取り込み済み Dukascopy tick」を表現する
- fetch の fake: `dict[str, bytes | None]`（URL → ペイロード）を包む callable。呼ばれた URL を
  記録する。未登録 URL は `None`（404 相当）を返す
- bi5 合成フィクスチャ: `struct.pack(">IIIff", ms, ask_point, bid_point, 1.0, 1.0)` を連結し
  `lzma.compress(data, format=lzma.FORMAT_ALONE)` で圧縮（実ファイルは alone フォーマット）

テストケース（最低限これを全部。価格は実勢に似た架空値でよい。実在人物・団体名は使わない）:

1. **URL 生成**: 2024-07-11 12:00 UTC →
   `https://datafeed.dukascopy.com/datafeed/USDJPY/2024/06/11/12h_ticks.bi5`（月 0 始まり）。
   1 月境界（2026-01-23 → `/2026/00/23/`）も 1 本
2. **bi5 デコードとスケーリング**: 合成レコード `(107, 161541, 161538, ...)` が
   `bid == Decimal("161.538")` / `ask == Decimal("161.541")` /
   `time == hour_start + 107ms` になる。Decimal の指数まで確認（float 経由なら落ちる値、
   例えば `Decimal("161.541")` と `==` で比較すれば十分）。`received_at` は注入 clock の値
3. **空ボディ / 404 は正常**: 空 `bytes` と `None` の時間帯は insert が呼ばれず、エラーにも
   失敗カウントにもならない
4. **重複範囲スキップ（MT5 境界 2024-07-22→23）**: リポジトリに **2024-07-23 の全 24 時間帯**へ
   既存 tick を事前投入し、`since=2024-07-22 00:00, until=2024-07-24 00:00` で実行 →
   07-22 の 24 時間分だけ fetch され、07-23 は 1 本も fetch されない（fetch 記録 URL で検証）。
   二重カウントの入り口を塞ぐ最重要テスト。既存 tick が一部の時間帯だけの場合は
   テスト 6 が担う
5. **再開スキップ（冪等）**: 1 回目の実行で入った tick をそのままに同じ範囲で 2 回目を実行
   → 2 回目は fetch されず insert もされない（stored 0）
6. **部分日の再開**: ある日の 0〜11 時台だけ既存 tick を事前投入 → 12 時台以降だけ fetch
   される（日レベル → 時間レベルへのフォールバックの検証）
7. **transient リトライ**: fetch fake が 2 回 `URLError` を投げ 3 回目に成功するとき、
   tick が入り失敗カウント 0。`sleep` fake で待ち時間が呼ばれたことも見る
8. **リトライ枯渇は先へ進む**: 常に例外を投げる時間帯があっても後続の時間帯は処理され、
   戻り値の失敗カウントに載る
9. **`[since, until)` フィルタ**: `since=12:30` のとき 12 時台ファイル内の 12:00〜12:29 の
   レコードは保存されない
10. **provenance**: すべての insert が `source="DUKASCOPY"` かつ同一 `ingestion_run`
    （`tests/unit/test_tick_collector.py:268-283` と同じ観点）

### 変更しないもの

- `migrations/`・`src/trading/storage/`（`market_ticks` スキーマ・リポジトリはそのまま使う）
- `src/trading/data/market/collector.py` / `bar_service.py` / `bars.py` / `stored.py`
- Strategy / LLM / OMS / Broker 層すべて（このインポーターは Collectors 層。Strategy から
  DB へ到達する経路を作らない）
- live 経路・BarService の配線・config YAML（新しい config キーは足さない）

## 完了条件（実行可能コマンド）

worktree ルート（このファイルがある場所の親）で:

1. `.venv/bin/ruff check .` が指摘ゼロ
2. `.venv/bin/pytest tests/unit/test_dukascopy_importer.py -q` が全件 green
3. `.venv/bin/pytest -q` が既存含め green（broker テストの skip は正常）
4. `.venv/bin/python -c "from trading.data.market import dukascopy"` が db extra なしで通る
   （psycopg の遅延 import が保たれている証拠）

## 規約（プロジェクトルールからの転記。必ず守る）

- **価格・数量は Decimal**。float を経由して Decimal 化しない（`Decimal(int) * Decimal(str)` は可。
  Indicator 計算のみ float 可だがこのタスクには存在しない）
- 通貨ペア・pip/point スケールをロジック内にハードコードしない（定数テーブル経由。上記）
- `datetime.now()` を直接呼ばない。`Clock` 注入（`trading.backtest.clock`）
- 検証は**システム境界のみ**（CLI 引数、外部 HTTP 応答）。内部関数間に防御的分岐・
  フォールバックを足さない。到達し得ないケースのエラーハンドリングを書かない
- 引数・共有オブジェクトを破壊しない（`Tick` は frozen pydantic モデル）
- SQL を新たに書かない（既存リポジトリのメソッドだけを使う）
- コメントは日本語優先。WHAT ではなく制約・理由（WHY）だけを書く。「レビュー指摘対応」の
  ようなコミット文脈依存コメントを書かない
- 新規ファイルは 200〜400 行目安（上限 800）
- テストデータに実在の人物・団体名を使わない
- ruff: line-length 100 / py311（`pyproject.toml`）。型注釈を付ける
- **コミットしない**。`tmp/` 配下と `.env` に触れない
