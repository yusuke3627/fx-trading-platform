# Dukascopy インポーターのサーバー障害待避（指数バックオフ + 連続失敗での一時停止）

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットもしない**（コミットと PR は別担当）。

## 背景

PR #102 の Dukascopy tick インポーター（`src/trading/data/market/dukascopy.py`）を VPS で
実行中、Dukascopy 側の一時的な応答不良（HTTP 503・接続タイムアウト）に当たった。Mac からも
同時刻に同症状を確認しておりサーバー側の障害。現行の再試行は「固定 5 秒待ち × 3 回で諦めて
次の時間帯へ」なので、障害の間は失敗しながら範囲を消費し続け、1 時間帯あたり 30 秒
タイムアウト × 3 を空費する。数時間の長時間ランでは再発が見込まれるため、障害を検知して
待避する仕組みを足す。

## 要件（全文）

1. **指数バックオフ**: 再試行の待ち時間を固定 5 秒から `RETRY_WAITS = (5.0, 15.0, 45.0)`
   （= 4 回試行、待ちは 3 回）に変更。`FETCH_ATTEMPTS` / `RETRY_WAIT_SECONDS` はこのタプルに
   置き換える（両定数は削除する。参照はモジュール内とテストだけ。下記「影響範囲」）
2. **障害検知と待避**: 全試行を使い切った時間帯が `OUTAGE_THRESHOLD = 3` 回**連続**したら
   サーバー障害とみなし、stderr に 1 行
   （例: `server outage suspected: 3 consecutive hours failed, pausing 300s`）出して
   `OUTAGE_PAUSE_SECONDS = 300` 待ち、**同じ時間帯から再開**する（前に進まない）。
   成功した時間帯（tick あり・404/空も含む）があれば連続カウントをリセット
3. **打ち切り**: 待避を `OUTAGE_MAX_PAUSES = 12` 回連続で行っても回復しなければ、
   `server outage: giving up after 12 pauses; rerun the same command after recovery` を出して
   終了コード 1 で終わる。取得済み分はそのまま残る
4. 失敗時間帯のメッセージ（`fetch failed after N attempts`）と日次進捗行はそのまま維持。
   終了コード「失敗時間帯があれば 1」の意味も変えない
5. `default_fetch` の timeout 30 秒と UA は変更しない
6. transient 扱いの例外 `(OSError, http.client.HTTPException)` に HTTP 5xx が含まれることを
   確認済み（下記「確認済みの事実」）

制約: 変更は `dukascopy.py` とそのテストのみ。定数はモジュール先頭に置き、それぞれ直上の
コメント 1 行で「なぜ 3 連続 / 300 秒 / 12 回か」を書く。実在人物名不使用。

## 確認済みの事実（2026-09-02、この worktree の Python 3.11 で実測）

- `urllib.error.HTTPError` の MRO は `HTTPError → URLError → OSError → ...`。HTTP 5xx で
  `default_fetch` が送出する `HTTPError`（404 以外は `raise` で再送出、
  `src/trading/data/market/dukascopy.py:108-111`）は既存の
  `except (OSError, http.client.HTTPException)`（同 :196）で捕捉される。**この except 節は
  変更しない**
- `socket.timeout` は `TimeoutError`（OSError 系）。接続タイムアウトも同じ節で捕捉される
- `http.client.IncompleteRead` は OSError ではなく `HTTPException` 系。既存テスト
  `tests/unit/test_dukascopy_importer.py:244-264` がこれを固定している
- `SystemExit` はこのリポジトリでは各 collector の `main()` 内でしか使っていない
  （`rg -n SystemExit src/trading/data`）。`import_range` は unit テストの対象 API なので、
  打ち切りは例外で表現し `main()` が終了コードへ変換する（下記）

## 現行コードの構造（file:line。実装前に必ず読む）

`src/trading/data/market/dukascopy.py`:

- :43-44 `FETCH_ATTEMPTS = 3` / `RETRY_WAIT_SECONDS = 5.0` — 置き換え対象
- :45 `REQUEST_INTERVAL_SECONDS = 0.1` — 維持
- :102-111 `default_fetch` — timeout=30 / UA 維持。変更しない
- :114-131 `DukascopyTickImporter.__init__` — `fetch` / `sleep` の注入経路。変更しない
- :133-251 `import_range` — 日ループ（:141）→ 時間ループ（:166）
  - :185-207 attempt ループ。最終試行で `fetch failed after N attempts` を stderr へ出し
    `fetch_failed = True; total_failed += 1`。それ以外は `self._sleep(RETRY_WAIT_SECONDS)`
  - :209 `self._sleep(REQUEST_INTERVAL_SECONDS)` — 成否に関わらず毎時間帯 1 回
  - :210-212 `fetch_failed or payload is None` → `hour_start = hour_end; continue`
    （失敗時と 404 時が同じ分岐にいる。**失敗と 404 を分ける必要がある**）
  - :251 `return total_stored, total_failed`
- :254-291 `main()` — :287 で `import_range` を呼び、:288-290 で失敗時間帯があれば
  `raise SystemExit(1)`。:276 に `raise SystemExit(f"... is not set")` のパターンあり

`tests/unit/test_dukascopy_importer.py`:

- :14-21 import（`RETRY_WAIT_SECONDS` を import している → 更新対象）
- :48-70 `FakeTickRepository`、:73-80 `FakeFetch`（URL → payload の dict）、
  :83-95 `make_importer(repository, fetch, *, sleep=...)` — sleep 注入は `sleeps.append` を渡す
- :244-264 `test_transient_fetch_errors_are_retried` — 2 回失敗 → 3 回目成功。
  `sleeps == [RETRY_WAIT_SECONDS, RETRY_WAIT_SECONDS, REQUEST_INTERVAL_SECONDS]` を更新
- :267-285 `test_exhausted_retries_are_counted_and_next_hour_continues` —
  `calls == [first_url] * 3 + [second_url]` と `"fetch failed after 3 attempts"` を更新

## 実装

### `src/trading/data/market/dukascopy.py`

#### 定数（:43-44 を置き換え。各定数の直上にコメント 1 行）

```python
# 単発の接続リセットは数秒で回復するが、サーバー側の過負荷は数十秒続くため、待ちを
# 3 倍ずつ伸ばして計 65 秒まで粘る（4 回試行、待ちは 3 回）。
RETRY_WAITS = (5.0, 15.0, 45.0)
REQUEST_INTERVAL_SECONDS = 0.1
# 1 時間帯だけの失敗はそのファイル固有の問題でもあり得るが、休場でも 404 / 空ボディが
# 正常に返る以上、3 時間帯連続で全試行が尽きるのはサーバー障害しか説明がつかない。
OUTAGE_THRESHOLD = 3
# 障害中に 30 秒タイムアウト × 4 を時間帯ごとに空費して範囲を消費するより、5 分待って
# 同じ時間帯を 1 回だけ確かめるほうが取りこぼしも帯域の浪費も少ない。
OUTAGE_PAUSE_SECONDS = 300.0
# 5 分 × 12 回 = 1 時間待っても回復しない障害は長期化しているので、無人で待ち続けるより
# 人が回復を確認してから同じコマンドで再開するほうがよい。
OUTAGE_MAX_PAUSES = 12
```

`FETCH_ATTEMPTS` と `RETRY_WAIT_SECONDS` は削除する（後方互換の別名を残さない）。

#### 例外クラス（定数の後、`known_to_broker_label` の前に追加）

```python
class ServerOutageError(RuntimeError):
    """待避を繰り返しても Dukascopy が回復しなかった。取得済みの時間帯は保存されている。"""
```

#### `import_range` の変更（:185-212 周辺）

状態変数を日ループの外（:137-139 付近、`total_failed` の隣）に 2 つ追加する:

- `consecutive_failures = 0` — 直近で全試行を使い切った時間帯の連続数
- `consecutive_pauses = 0` — 成功を挟まずに行った待避の回数

attempt ループを `RETRY_WAITS` 駆動にする（現行 :189-207 の構造を保つ）:

```python
attempts = len(RETRY_WAITS) + 1
for attempt in range(1, attempts + 1):
    try:
        payload = self._fetch(url)
        break
    # （既存コメント :193-195 はそのまま）
    except (OSError, http.client.HTTPException) as exc:
        if attempt == attempts:
            print(
                f"{hour_start:%Y-%m-%d %H:%M}: fetch failed after "
                f"{attempts} attempts: {exc}",
                file=sys.stderr,
                flush=True,
            )
            fetch_failed = True
        else:
            self._sleep(RETRY_WAITS[attempt - 1])
```

`total_failed += 1` は except 節から外し、下の分岐で「諦めて先へ進む」ときだけ数える。

:209 の `self._sleep(REQUEST_INTERVAL_SECONDS)` はそのまま（成否に関わらず 1 回）。

:210-212 を次の 3 分岐に分ける（**失敗と 404/空を分ける**）:

```python
self._sleep(REQUEST_INTERVAL_SECONDS)
if fetch_failed:
    # この時間帯を数えても閾値に届かないなら、現行どおり諦めて先へ進む。
    if consecutive_failures + 1 < OUTAGE_THRESHOLD:
        consecutive_failures += 1
        total_failed += 1
        hour_start = hour_end
        continue
    consecutive_pauses += 1
    if consecutive_pauses > OUTAGE_MAX_PAUSES:
        raise ServerOutageError(
            f"server outage: giving up after {OUTAGE_MAX_PAUSES} pauses; "
            "rerun the same command after recovery"
        )
    print(
        f"server outage suspected: {OUTAGE_THRESHOLD} consecutive hours failed, "
        f"pausing {OUTAGE_PAUSE_SECONDS:.0f}s "
        f"({consecutive_pauses}/{OUTAGE_MAX_PAUSES})",
        file=sys.stderr,
        flush=True,
    )
    self._sleep(OUTAGE_PAUSE_SECONDS)
    continue  # hour_start を進めず、同じ時間帯を取り直す
consecutive_failures = 0
consecutive_pauses = 0
if payload is None:
    hour_start = hour_end
    continue
```

意味:

- 待避に至る前の失敗時間帯（連続 1 回目・2 回目）は現行どおり `total_failed` に数えて次へ進む。
  これらは tick が入らないので、後日の再実行が埋め直す（終了コード 1 の意味は現行と同じ）
- 3 回目の連続失敗は `total_failed` に**数えず**、待避して同じ時間帯を取り直す。
  `consecutive_failures` は待避中 `OUTAGE_THRESHOLD - 1` のまま動かさないので、待避後の
  再試行が失敗すればそのまま次の待避に入る（範囲を消費しない）。待避の判定に必要なのは
  「この失敗を数えると閾値に届くか」だけなので、増やしてから戻すような操作はしない
- 成功（tick あり / 404 / 空ボディ）で両カウンタをリセットする。「成功」の判定は
  `fetch_failed` が偽であること。`payload is None`（404）も空 bytes も成功
- 待避後の再試行は `while hour_start < day_end:` の先頭から同じ時間帯をやり直す。
  `check_each_hour` の `bounds_between` 照会（:173-183）と `requested_hours += 1`（:185）も
  再実行されるが、失敗した時間帯には tick が入っていないので照会結果は変わらず、
  `requested_hours` は `== 0` としか比較されないため問題ない。この再実行を避けるための
  分岐は足さない
- 待避の時点で `fetch failed after 4 attempts` 行は既に出ている（except 節）。その直後に
  `server outage suspected: ...` 行が続く
- 打ち切りは `ServerOutageError` を送出する。`total_stored` はこの時点で `insert_many` 済み
  （時間帯ごとに 1 呼び出し 1 コミット）なので DB に残る。戻り値・件数は返さない

`sleep` 呼び出しの並び（テストの期待値の根拠）:

- 4 回失敗した時間帯 1 つ = `5.0, 15.0, 45.0, 0.1`
- それが待避を誘発したとき = 上に続けて `300.0`。待避後の再試行が 1 回で成功すれば `0.1`
- 2 回失敗して 3 回目で成功した時間帯 = `5.0, 15.0, 0.1`

#### `main()` の変更（:287 付近）

```python
try:
    stored, failed = importer.import_range(symbol, args.since, args.until)
except ServerOutageError as exc:
    raise SystemExit(str(exc)) from exc
```

`SystemExit(str)` はメッセージを stderr に出して終了コード 1 になる（:276 と同じ用法）。
:288-291 の「失敗時間帯があれば exit 1」はそのまま。

#### モジュール docstring（:1-12）

:4 の文の後に 1 文足す:
「連続で失敗が続いたときはサーバー障害とみなして一定時間待避し、同じ時間帯から取り直す。」

### `tests/unit/test_dukascopy_importer.py`

fetch と sleep は既存の注入経路（`make_importer(..., sleep=sleeps.append)`、fetch は callable）
を使う。実時間もネットワークも使わない。URL は `hour_url(SYMBOL, T0 + timedelta(hours=n))`。
`T0 = 2024-07-11 12:00 UTC`（:25）。成功 payload は `bi5_payload((0, 150004, 150001))`。

**共通の fake fetch**（このファイル内に 1 つ追加。既存 `FakeFetch` は dict 固定なので別に作る）:
URL ごとに「例外を投げる残り回数」を持ち、残りがある間は `URLError("service unavailable")`
を投げて減らし、尽きたら payload（または `None`）を返す。呼ばれた URL を `calls` に記録する。
「常に失敗」は十分大きい残り回数（例 `10_000`）で表す。

```python
class FlakyFetch:
    def __init__(self, failures_left: dict[str, int], payloads: Mapping[str, bytes | None]) -> None
    def __call__(self, url: str) -> bytes | None
    calls: list[str]
```

#### 既存テストの更新（緩めない）

1. import（:14-21）: `RETRY_WAIT_SECONDS` を外し `OUTAGE_MAX_PAUSES`, `OUTAGE_PAUSE_SECONDS`,
   `RETRY_WAITS`, `ServerOutageError` を足す（使うものだけ）
2. `test_transient_fetch_errors_are_retried`（:244-264）: 期待 sleep を
   `[RETRY_WAITS[0], RETRY_WAITS[1], REQUEST_INTERVAL_SECONDS]` に。`calls` の 3 回はそのまま
3. `test_exhausted_retries_are_counted_and_next_hour_continues`（:267-285）:
   `calls == [first_url] * 4 + [second_url]`、メッセージは `"fetch failed after 4 attempts"`。
   戻り値 `(1, 1)` はそのまま。**バックオフの検証をこのテストに足す**: `make_importer` に
   `sleep=sleeps.append` を渡し、
   `sleeps == [*RETRY_WAITS, REQUEST_INTERVAL_SECONDS, REQUEST_INTERVAL_SECONDS]`
   （5 → 15 → 45 の順で待ち、4 回目の失敗で次の時間帯へ進む）を assert する。
   同じシナリオの別テストは作らない

#### 追加テスト（すべて `import_range` 直接呼び出し）

1. **連続 3 時間帯の失敗で待避し同じ時間帯を取り直す。成功で連続カウントがリセット**
   （`test_three_consecutive_failed_hours_pause_and_retry_same_hour`、`capsys` 使用）:
   h0・h1 は常に失敗、h2 は 4 回失敗した後に成功（`failures_left = 4`）、h3・h4 は常に失敗。
   範囲 5 時間。期待:
   - `calls == [u0]*4 + [u1]*4 + [u2]*4 + [u2] + [u3]*4 + [u4]*4`
     （u2 の 5 回目 = 待避後の再試行。h3・h4 は失敗 2 連続で待避に届かない = リセットの証拠）
   - `sleeps == [*RETRY_WAITS, I, *RETRY_WAITS, I, *RETRY_WAITS, I, OUTAGE_PAUSE_SECONDS, I, *RETRY_WAITS, I, *RETRY_WAITS, I]`
     （`I = REQUEST_INTERVAL_SECONDS`）。特に `sleeps.count(OUTAGE_PAUSE_SECONDS) == 1`
   - 戻り値 `(1, 4)`（h2 の 1 tick が保存、失敗時間帯は h0・h1・h3・h4）
   - stderr に `"server outage suspected: 3 consecutive hours failed, pausing 300s"` を含む
2. **途中の 404 で連続カウントがリセットされ待避しない**
   （`test_not_found_hour_resets_consecutive_failures`）:
   h0・h1 常に失敗、h2 は `None`（404）、h3・h4 常に失敗。範囲 5 時間。期待:
   `OUTAGE_PAUSE_SECONDS not in sleeps`、戻り値 `(0, 4)`、
   `calls == [u0]*4 + [u1]*4 + [u2] + [u3]*4 + [u4]*4`、stderr に `"server outage"` を含まない
3. **待避 12 回で打ち切り。取得済み時間帯は保存されている**
   （`test_gives_up_after_max_pauses_and_keeps_stored_hours`）:
   h0 成功、h1・h2・h3 常に失敗。範囲 4 時間。期待:
   - `pytest.raises(ServerOutageError, match="giving up after 12 pauses")`
   - `sleeps.count(OUTAGE_PAUSE_SECONDS) == OUTAGE_MAX_PAUSES`
   - `calls.count(u3) == 4 * (OUTAGE_MAX_PAUSES + 1)`（初回 + 待避 12 回分の再試行）、
     `calls[:9] == [u0] + [u1]*4 + [u2]*4`
   - `len(repository.ticks) == 1` かつ `repository.calls` が 1 件（h0 の分が残っている）

`main()` の `ServerOutageError → SystemExit` 変換はテストしない（`load_config` と DSN が要る。
2 行の変換で、`SystemExit(str)` の終了コード 1 は Python の仕様）。

## 影響範囲（確認済み）

`rg -n "FETCH_ATTEMPTS|RETRY_WAIT_SECONDS" src config tests docs` のヒットは
`src/trading/data/market/dukascopy.py` と `tests/unit/test_dukascopy_importer.py` だけ。
config YAML・migrations・storage・docs は触らない。

## やらないこと

- `default_fetch`（timeout / UA / 404 の扱い）と `except (OSError, http.client.HTTPException)`
  の変更
- `import_range` の戻り値の型変更（`tuple[int, int]` のまま）
- 待避の秒数・回数を CLI 引数や config で可変にすること
- 待避中の再試行間隔を別途伸ばすこと（待避は常に 300 秒固定）
- 他の collector（`src/trading/data/**/collector.py`）への同種の仕組みの展開
- `tests/unit/test_dukascopy_importer.py` 以外のテストの変更
- ログ出力の logging モジュールへの置き換え（既存どおり `print(..., file=sys.stderr)`）

## 完了条件（実行可能コマンド）

worktree ルート（このファイルがある `tasks/` の親）で:

1. `.venv/bin/ruff check .` が指摘ゼロ
2. `.venv/bin/pytest tests/unit/test_dukascopy_importer.py -q` が全件 green
3. `.venv/bin/pytest -q` が既存含め green。変更前の baseline はこの worktree で
   `907 passed, 1 skipped`（skip は broker テストで正常）。追加テスト 3 本分だけ増える
4. `rg -n "FETCH_ATTEMPTS|RETRY_WAIT_SECONDS" src tests` がヒット 0

## 規約（プロジェクトルールからの転記。必ず守る）

- 検証は**システム境界のみ**（外部 HTTP 応答）。内部関数間に防御的分岐・フォールバックを
  足さない。到達し得ないケースのエラーハンドリングを書かない
- `datetime.now()` を直接呼ばない（既存の `Clock` 注入のまま）。`time.sleep` も既存の
  `sleep` 注入経由でのみ呼ぶ
- 引数・共有オブジェクトを破壊しない
- コメントは日本語優先。WHAT ではなく制約・理由（WHY）だけを書く。「レビュー指摘対応」
  「〜のために追加」のようなコミット文脈依存コメントを書かない。AI レビューの引用を残さない
- 後方互換の別名・シム（`FETCH_ATTEMPTS = len(RETRY_WAITS) + 1` のような残置）を作らない
- テストデータに実在の人物・団体名を使わない
- ruff: line-length 100 / py311（`pyproject.toml`）。型注釈を付ける
- **コミットしない**。`tmp/` 配下と `.env` に触れない
