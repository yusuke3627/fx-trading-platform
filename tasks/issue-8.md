# issue #8 実装計画: USD/JPY Tick 自前収集 collector（MT5 ポーリング + PIT 保存）

- issue: https://github.com/yusuke3627/fx-trading-platform/issues/8
- branch: `feat/issue-8-tick-collector`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-8-tick-collector`
- ベースライン: `origin/main` (3b8fc1c) で `pytest -q` = 206 passed / 1 skipped、`ruff check .` clean

## 依存（着手前提）

**issue #7（`market_ticks` schema + Tick リポジトリ protocol）のマージが完了するまで実装を開始しない。**
#7 が `migrations/0002_market_data.sql` と `storage/repository.py` / `storage/postgres.py` を専有するため、
#8 は **マイグレーションを一切追加しない**（`migrations/` に触れない）。

実装開始時の手順:

1. `git fetch origin main && git merge origin/main`（`tasks/APPROVAL.md` の指定どおり merge。rebase しない）
2. `storage/repository.py` の Tick リポジトリ protocol を読み、本計画の「#7 依存の吸収点」節と差分を突き合わせる
3. 差分があれば本ファイルを更新してから実装に入る（protocol 名・シグネチャは #7 側が正）

計画は承認済み（`tasks/APPROVAL.md`）。実装開始の合図は親（main）から SendMessage で届く。
`tasks/APPROVAL.md` はコミットに含めない。

---

## 設計方針

### 全体像

```
MT5 terminal ──(symbol_info_tick / copy_ticks_range)──> TickCollector
                                                            │
                                              raw → domain Tick 変換
                                              received_at = clock.now()
                                                            │
                              MarketTickRepository.insert_many (#7)
                                   source / ingestion_run を渡す
                                                            │
                                          INSERT ... ON CONFLICT DO NOTHING
                                                            ▼
                                                      market_ticks
```

collector は **SQL を書かない**。書き込みは #7 の Tick リポジトリ protocol 経由のみ。
`ON CONFLICT DO NOTHING` は storage 側（#7）の責務で、collector は重複を送っても壊れない前提に立つ。

### 時刻の分離（PIT の核）

| 値 | 出所 | 用途 |
| --- | --- | --- |
| `event_time` | MT5 raw tick の `time_msc`（ミリ秒 epoch） | broker 側のクオート時刻 |
| `received_at` | 注入した `Clock.now()`（本番は `SystemClock` = UTC） | **可視時刻**。`known_at ≒ received_at` 規約 |

- domain の `Tick`（`src/trading/domain/market.py`）は `time`（broker 時刻）と `received_at` を既に持ち、
  `known_time` プロパティが `received_at or time` を返す。collector は既存モデルをそのまま使い、**domain を変更しない**。
- DB カラム名 `event_time` への対応付けは #7 のリポジトリ実装が行う（`Tick.time` → `event_time`）。
- **`time` ではなく `time_msc` を使う。** 秒精度の `time` だと同一秒内の複数クオートが
  `UNIQUE(symbol, event_time, bid, ask)` で潰れ、同一秒内の bid/ask 往復（scalp 研究の主対象）が欠落する。
  変換は `datetime.fromtimestamp(time_msc / 1000, tz=UTC)`。
- **MT5 のサーバー時刻オフセットは補正しない。** `execution/mt5/mapper.py` の `_utc()` が deal 時刻に対して
  取っている規約（epoch をそのまま UTC 扱い）と揃える。ここで独自補正を入れると fills と ticks で
  時間軸がずれる。補正が必要と判明した場合は ADR + 別 issue で全時刻系をまとめて扱う。
- **実装時の変更**: 当初は「run 開始時に `received_at - event_time` を 1 回 stdout に出す」と計画したが、
  実装では入れなかった。全行に `event_time` と `received_at` の両方が保存されるので、
  オフセットは `SELECT received_at - event_time FROM market_ticks ...` で、しかも 1 件ではなく
  多数の行から測れる。起動時に 1 件だけ print するコードはこれより劣る。
  この規約は `_utc_from_msc` の docstring に残した。

### ターミナル接続とシンボル選択（見落とし注意）

ティック取得の前に、MT5 モジュールに対して以下を必ず実行する。

1. `mt5.initialize()` — 失敗（falsy）なら `MT5ConnectionError`
2. `mt5.symbol_select(symbol, True)` — Market Watch に載っていないシンボルは
   `symbol_info_tick` が None／ゼロ値を返すため、選択は省略できない。失敗なら `MT5ConnectionError`
3. 終了時に `mt5.shutdown()`

**これらは `MT5ExecutionAdapter` 経由では呼ばない。** adapter は `order_send` を持つ発注面であり、
data 層の collector がそのオブジェクトを保持すると「収集プロセスから発注できる」構造になる。
collector は注入された mt5 module のメソッドを直接叩く（`initialize` / `symbol_select` /
`symbol_info_tick` / `copy_ticks_range` / `last_error` / `shutdown` の 6 つだけ）。

接続とシンボル選択は `TickCollector.connect(symbol)` に置き、`run()` / `backfill()` の前に
`main()` から 1 回呼ぶ。`poll_once` 自体は接続処理を持たない（テストで 1 周期だけを回せるようにする）。

### 取得の 2 経路

**1. ポーリング（定常運用）** — `symbol_info_tick(symbol)`

- 1 周期 = 1 回の `symbol_info_tick` 呼び出し。返り値が `None` なら `MT5ConnectionError` を送出する
  （`adapter.py` と同じ規約: 取得失敗を「ティックなし」と混同しない）。
- 同一クオートを取り続けるため、プロセス内で直前に書いた `(event_time, bid, ask)` を保持し、
  一致したら書き込みをスキップする。DB 側の `ON CONFLICT DO NOTHING` が最終防壁で、
  プロセス内スキップは DB への無駄打ちを抑えるためだけのもの。

**2. バックフィル（欠損補修）** — `copy_ticks_range(symbol, date_from, date_to, COPY_TICKS_ALL)`

- 切断・再起動で空いた期間を後から埋める経路。返り値が `None` なら `MT5ConnectionError`。
  空配列（該当ティックなし）は正常として 0 件で返す。
- `date_from` / `date_to` は tz-aware UTC の `datetime` を渡す（MT5 は naive datetime を
  ローカル時刻として解釈するため必ず aware で渡す）。
- バックフィル分の `received_at` は「その行を DB に書いた時刻」= `clock.now()`。broker 時刻に遡らせない
  （遅延受信した価格を過去に遡って可視化しないという SYSTEM_SPEC の可視性規約）。

### 2 経路で raw の形が違う（実装の罠）

**`symbol_info_tick` と `copy_ticks_range` は返す型が違い、フィールドの読み方も違う。**

| 経路 | 返り値 | フィールドアクセス |
| --- | --- | --- |
| `symbol_info_tick` | named tuple 相当のオブジェクト 1 件 | 属性アクセス `raw.bid` |
| `copy_ticks_range` | numpy structured array（行は `numpy.void`） | **キーアクセス `row["bid"]`。`row.bid` は AttributeError** |

ローカル venv に numpy 2.4.6 を一時導入して実測確認済み（確認後アンインストールし、
`pytest -q` が 206 passed / 1 skipped に戻ることも確認した）:

- `row["bid"]` → `158.84` / `row.bid` → `AttributeError: 'numpy.void' object has no attribute 'bid'`
- `str(row["bid"])` は `'158.84'` を返すので、既存の `Decimal(str(x))` 変換がそのまま使える
  （numpy 2.x の `repr` は `np.float64(...)` 形式だが `str` は素の数値表記）
- `int(row["time_msc"])` も期待どおり動く

したがって raw→domain 変換は **経路ごとに 2 つの小さな関数**に分ける
（`_tick_from_info(raw, ...)` = 属性アクセス / `_tick_from_row(row, ...)` = キーアクセス）。
共通化のために片方へ寄せる汎用アクセサは書かない。

**テストの fake もこの差を再現する。** ポーリング用 fake は `SimpleNamespace`
（`test_adapter.py` と同じ）、バックフィル用 fake は **キーアクセスできる dict のリスト**を返す。
両方を `SimpleNamespace` にすると、テストが通るのに Windows 実機のバックフィルだけ
`AttributeError` で落ちる状態を作ってしまう。numpy はローカル依存に無いので dict で代用する
（`row["bid"]` の読み方が同じであれば十分）。

### 既知の限界: ポーリングは取りこぼす（実装前に合意しておく点）

`symbol_info_tick` は「その瞬間の最新ティック」しか返さないため、ポーリング間隔の間に発生した
ティックは構造的に取得できない。USD/JPY は活発な時間帯に 1 秒あたり数ティック出るので、
0.2 秒間隔でもポーリング列だけでは完全な tick 列にならない。

- **この issue のスコープ内での答え**: 完全性は `copy_ticks_range` バックフィルで担保する。
  定常ポーリングは「直近価格を切らさず持つ」ための経路、バックフィルは「研究用の完全な tick 列を作る」
  ための経路と役割を分ける。日次などの定期バックフィル実行は運用手順（タスクスケジューラ）で回す。
- **やらないこと**: run ループの中に自動キャッチアップ・バックフィルを組み込むことはしない。
  間隔・重複範囲・失敗時の扱いを決める必要があり、この issue の範囲を超える。
- 定常経路そのものを増分 `copy_ticks_range` に置き換える案は、実データで取りこぼし率を測ってから
  別 issue で判断する。**本 PR ではこの限界を collector の docstring に明記して残す。**

### 切断時の方針（リトライを実装しない）

`poll_once` は `MT5ConnectionError` をそのまま呼び出し元へ伝播させ、CLI ループは例外で終了する（exit code 1）。

- 理由: 再接続・リトライ間隔・ギャップ検出をここで発明すると、仕様が決まっていない復旧ポリシーを
  コードに固定してしまう。落ちたら Windows のタスクスケジューラ／スーパーバイザが再起動し、
  空いた期間は `--backfill-from/--backfill-to` で埋める、という運用手順で閉じる。
- この方針は README ではなく collector モジュールの docstring に 1 段落で書く。

### MT5 なし環境での import ガード

- MT5 モジュールの読み込みは `adapter.py` の既存ローダを再利用する。現在 private な
  `_load_mt5_module()` を `load_mt5_module()` に改名して公開し、`adapter.py` 内の呼び出し 1 箇所を更新する
  （新規の抽象は作らない。例外型 `MT5NotAvailable` / `MT5ConnectionError` もそのまま再利用する）。
- `collector.py` のモジュールレベル import は **stdlib + `trading.domain` + `trading.backtest.clock` +
  `trading.storage.repository`（protocol のみ）** に限る。
  - `psycopg` は `trading.storage.postgres` 経由で `main()` の中だけで import する。
    `postgres.py` はトップレベルで `psycopg` を import しているため、モジュール先頭で読むと
    psycopg 未導入のローカル環境でユニットテストが collection error になる。
  - MetaTrader5 は `load_mt5_module()` の中で遅延 import される（既存の挙動）。
- テストは fake MT5 module と fake リポジトリを注入して実行するため、Windows 以外でも常時走る。

### CLI

```bash
# 定常ポーリング
python -m trading.data.market.collector --env demo --symbol USDJPY

# 欠損期間のバックフィル（実行後に終了する）
python -m trading.data.market.collector --env demo --symbol USDJPY \
    --backfill-from 2026-08-14T00:00:00Z --backfill-to 2026-08-15T00:00:00Z
```

- `--env`（既定 `demo`）/ `--symbol`（既定は `config.market.primary_instruments[0]`）は
  `preflight.py` の `main()` と同じ流儀に揃える。`load_config` は既定の相対パス `config` を見るので、
  preflight と同じくリポジトリルートからの実行が前提。
- `--interval-seconds` は未指定なら config の `market.tick_poll_interval_seconds` を使う。
- `--backfill-from` / `--backfill-to` は `datetime.fromisoformat` で解釈する
  （Python 3.11+ は `...Z` 表記をそのまま受け付ける。ローカル venv 3.11.0 で確認済み）。
  **naive datetime（tz 指定なし）は境界で reject する。** MT5 は naive datetime をローカル時刻として
  扱うため、素通しすると取得範囲が黙ってずれる。`from >= to` も reject する。
  両方指定されたときだけバックフィルモードで動き、実行後に終了する（ポーリングへは入らない）。
  長い範囲は 1 日単位のウィンドウに分割して `copy_ticks_range` を呼ぶ（メモリ対策。下記参照）。
  終了時に実挿入件数を出力する。
- DSN は `os.environ[config.storage.dsn_env]`（= `TRADING_DB_DSN`）から読み、未設定なら
  `SystemExit` で即座に落とす。空文字フォールバックも既定値も持たない。DSN を引数・ログに出さない。
- `python -m trading.data.market.collector` で起動できるよう、`preflight.py` と同じく
  末尾に `if __name__ == "__main__": main()` を置く。

---

## 変更対象ファイル

### 新規

| パス | 内容 |
| --- | --- |
| `src/trading/data/market/collector.py` | `TickCollector`（`connect` / `poll_once` / `backfill` / `run` / `disconnect`）、経路別の raw→domain 変換 2 関数、`main()` CLI |
| `tests/unit/test_tick_collector.py` | fake MT5 module + fake リポジトリによるユニットテスト |

### 変更

| パス | 内容 |
| --- | --- |
| `src/trading/execution/mt5/adapter.py` | `_load_mt5_module` → `load_mt5_module` に改名（呼び出し 1 箇所も更新）。他の変更なし |
| `src/trading/config.py` | `MarketConfig` に `tick_poll_interval_seconds: float = 0.2` を追加 |
| `config/base.yaml` | `market.tick_poll_interval_seconds: 0.2` を追加（環境 overlay は変更しない） |

### 変更しない（重要）

- `migrations/**` — **#8 では追加も変更もしない**（schema は #7 の専有。0002 は #7、#8 は SQL を足さない）
- `src/trading/storage/repository.py` / `storage/postgres.py` — #7 の成果物をそのまま使う
- `src/trading/domain/market.py` — `Tick` に新規フィールドを足さない
  （`flags` を足すと Strategy が `context.market.ticks()` 経由で MT5 固有ビットマスクを読めてしまう。
  3 列とも不採用で確定したので、そもそも足す動機がない）
- `src/trading/data/market/__init__.py` — `MarketDataService` / `InMemoryMarketData` は無関係
- `tests/support.py` — fake MT5 raw tick は `tests/unit/test_adapter.py` の流儀に倣い
  テストファイル内にローカル定義する（1 ファイルからしか使わないものを共有ファクトリに置かない）。
  `FixedClock` / `T0` は support から import して使う
- `docs/SYSTEM_SPEC.md` — 凍結済み。仕様変更にはあたらないため触れない

### 競合の見込み

- `src/trading/execution/mt5/adapter.py`: issue #6（OMS の pending 一括取り出し）と #7（storage）は
  いずれも adapter を触らない想定。改名は 2 行の変更なので、競合しても解消は容易。
- `src/trading/config.py` / `config/base.yaml`: #6 / #7 の scope 外。

---

## 実装単位（この順に進める）

1. `adapter.py` の `_load_mt5_module` → `load_mt5_module` 改名（`pytest tests/unit/test_adapter.py` で確認）
2. `config.py` + `config/base.yaml` に `tick_poll_interval_seconds` を追加
3. `collector.py`: 経路別 raw→domain 変換 2 関数 + `connect` / `disconnect` + `poll_once` + `backfill`
4. `tests/unit/test_tick_collector.py` を書き、3 の振る舞いを固定する
5. `TickCollector.run`（`poll_once` + `time.sleep` の薄いループ）と `main()` CLI
6. `ruff check .` → `pytest -q`
7. `/code-review-expert` で差分レビュー、P0–P2 を自動修正して 6 を再実行

### `TickCollector` の想定シグネチャ

```python
class TickCollector:
    def __init__(
        self,
        repository: MarketTickRepository,   # #7。import は 1 箇所に閉じる
        *,
        clock: Clock | None = None,      # 既定 SystemClock
        mt5_module: Any | None = None,   # 既定 load_mt5_module()
        source: str = "MT5",
    ) -> None: ...

    def connect(self, symbol: str) -> None: ...           # initialize + symbol_select
    def disconnect(self) -> None: ...                     # shutdown
    def poll_once(self, symbol: str) -> int: ...          # 実挿入件数（重複は 0）
    def backfill(self, symbol: str, start: datetime, end: datetime) -> int: ...
    def run(self, symbol: str, interval_seconds: float) -> None: ...
```

- `ingestion_run` は `__init__` で `uuid4()` を 1 個生成し、その collector インスタンスの全書き込みに付ける
  （1 プロセス = 1 run。行から取得実行を追跡できる）。
- `mt5_module` を注入可能にするのは `MT5ExecutionAdapter` と同じ理由（Windows 以外でロジックを回すため）。
- MT5 定数は `mapper.py` の流儀に倣い collector 側にモジュール定数として複製する
  （`COPY_TICKS_ALL = -1`）。Windows ホストでの初回実行時に値の妥当性を確認する。
- チャンクサイズ（`insert_many` 1 回あたりの件数）とバックフィルのウィンドウ幅（1 日）も
  モジュール定数にする。マジックナンバーを埋め込まない。

---

## #7 依存の吸収点（agent-issue-7 の回答で更新済み）

agent-issue-7 から回答を受領。**ただし #7 も承認ゲート前で未マージのため、下記は「確定した提案」であって
不変ではない。** 実装開始時に `storage/repository.py` の実物と必ず突き合わせる。

| 項目 | #7 の回答 | 備考 |
| --- | --- | --- |
| protocol 名 | **`MarketTickRepository`**（`TickRepository` ではない） | 変わり得ると明言あり。import を 1 箇所に閉じ、名前変更が 1 行差分で済むようにする |
| 書き込み | **確定**: `insert_many(ticks: Sequence[Tick], *, source: str, ingestion_run: UUID) -> int` | **単発 `insert` は作られない。** 1 件でも `insert_many([tick], ...)` |
| batch 単位 | **呼び出し側（= collector）が決める。** リポジトリは渡された Sequence を `executemany` 1 回 + commit 1 回で処理する | 下記「バックフィルの分割方針」を参照 |
| 戻り値 | **確定: `-> int`（実挿入件数）。** #7 が PostgreSQL 14 + psycopg 3.3.4 で実測確認済み（全件重複なら `0` が返る） | 下記の CLI 表示要件が満たせる |
| `source` / `ingestion_run` | リポジトリ引数。**両方 `NOT NULL`** なので毎回渡す必要がある | `ingestion_run` は取り込み実行ごとに UUID 1 個。計画どおり `__init__` で発行 |
| `received_at` | リポジトリが `tick.known_time`（= `received_at or time`）を書く | **collector が `Tick.received_at` を必ず埋める。** 埋め忘れると broker 時刻が受信時刻として記録され、PIT 可視性が壊れる |
| 読み出し | `known_before(symbol, t, since)`（`since` 必須） | collector は読まないので影響なし |
| 列名 | #7 の 0002 で `tick_time` → `event_time` に改名される | 本計画は既に `event_time` 前提 |
| migration 番号 | **0002 は #7 が専有** | #8 は migration を追加しないので影響なし |

**追従を 1 箇所に閉じる**: protocol 名が変わっても collector の raw→domain 変換・ポーリング・
バックフィルのロジックは変わらない。変わるのは「リポジトリをどう呼ぶか」だけなので、
書き込みは `_write(ticks)`（チャンク分割 + `insert_many` 呼び出し）1 メソッドに集約し、
protocol の import もそこに閉じる。

### 戻り値 `-> int` の意味（確定済み）

バックフィルは同じ範囲を再実行しうる（運用手順として「切断期間を埋める」ため）。
`ON CONFLICT DO NOTHING` があるので再実行は冪等だが、戻り値が無いと CLI は
「送った件数」しか表示できず、**既に収集済みの範囲を流し直しても「100 万件書き込み」と出てしまう**。

#7 がこれを受けて `-> int`（実挿入件数）で確定させ、PostgreSQL 14 + psycopg 3.3.4 で実測確認した:
サーバ側の `INSERT ... ON CONFLICT DO NOTHING` は実挿入のみ計上し、psycopg3 の
`executemany` 後の `cursor.rowcount` は**全件重複なら `0`** を返す。

したがって `poll_once` / `backfill` の戻り値も「実際に DB へ入った件数」の意味で統一する
（送信件数ではない）。CLI はバックフィル完了時にこの値を出す。

### バックフィルの分割方針（batch 単位が呼び出し側責務のため）

- `copy_ticks_range` は指定範囲のティックを numpy array として**一括で**返す。USD/JPY の 1 日は
  10 万〜100 万ティック規模になるため、長期間を 1 回で要求するとメモリを圧迫する。
  **CLI は要求された範囲を 1 日単位のウィンドウに分割し、日ごとに `copy_ticks_range` を呼ぶ。**
- リポジトリへの書き込みは 10,000 件ずつのチャンクに切って `insert_many` を呼ぶ
  （`executemany` 1 回あたりのパラメータ数を抑える）。チャンクサイズはモジュール定数にする。
- ポーリング経路は 1 周期 1 件なので分割は不要（`insert_many([tick], ...)`）。

### 確定: `last_price` / `flags` は列ごと入らない。#8 は bid/ask のみ書く

チームリード判断（`tasks/APPROVAL.md`）により、**`market_ticks.last_price` /
`market_ticks.flags` / `market_bars.spread` の 3 列は #7 の 0002 に入らない**ことで確定した
（#8 と #7 の共同推奨を採用）。したがって **#8 はこれらを書かず、collector は bid/ask のみを保存する。**
`flags` のビット定義を collector に持つ必要もない。

決定の根拠（後から経緯を追う人向けに残す）:

1. `last`（最終約定価格）は FX ブローカーの気配では実質常に 0.0 で、USD/JPY の研究価値がない。
2. `flags` を domain `Tick` に足すと、`context.market.ticks()` 経由で **Strategy が MT5 固有の
   ビットマスクを読める**構造になり、`docs/PROJECT_STRUCTURE.md` の
   "Strategy implementation must not know the Broker" に反する。
   なお `tests/unit/test_invariants.py` の禁止文字列スキャンは
   `SCANNED_DIRS = ("strategy", "intelligence")` のみが対象で `domain/market.py` を見ないため、
   **自動テストでは止まらない**（コードで確認済み）。文書化された設計原則違反であって
   不変条件テスト違反ではない。
3. それを避けて storage 層に raw 行専用の書き込み口を足すと、読み手のいない列のために
   抽象が 1 つ増える。AGENTS.md の「テストでの使用が確認できない後方互換用のデッドコードを足さない」
   「将来の仮定要件に備えて設計しない」に反する。
4. 「入れないと 0003 が即必要になる」というコストは `ALTER TABLE ADD COLUMN` 1 本にすぎず、
   実際の writer が現れた時点でその writer と一緒に足すほうが、誰も書かない列を 3 つ抱えて
   「収集済み」に見えるスキーマを作るより安い。

---

## テスト方針

`tests/unit/test_tick_collector.py`（fake MT5 module + fake リポジトリ、実在人物名は使わない）:

1. **raw→domain 変換と received_at の付与**
   fake の `symbol_info_tick` が返す raw tick 1 件に対し、`Tick.time` が `time_msc` 由来の UTC、
   `Tick.received_at` が `FixedClock.now()` と一致することを確認する。
   （`tests/support.py` の `FixedClock` / `T0` をそのまま使う）
2. **ポーリング 1 周期の取得→変換→書き込み**
   `poll_once` 1 回でリポジトリに 1 件届き、戻り値が 1 であること。
3. **同一クオートの連続ポーリングは 1 件しか書かない**
   同じ raw tick を返し続ける fake に対して `poll_once` を 2 回呼び、書き込みが 1 件のままであること。
4. **同一秒内の別クオートは別行として書かれる**
   `time_msc` が 500ms 違い bid が異なる 2 ティックが 2 件とも書かれること。
   秒精度実装への退行をここで捕まえる（このテストは `time` 実装だと落ちる）。
5. **取得失敗（None）は MT5ConnectionError**
   `symbol_info_tick` が `None` を返す fake で `pytest.raises(MT5ConnectionError)`。
   「ティックなし」として無視されないことを固定する。
6. **バックフィルの範囲指定**
   fake の `copy_ticks_range` が呼ばれた引数（symbol / date_from / date_to / flags）を記録し、
   渡した範囲が tz-aware のまま届くこと、返った 2 件が両方書かれ `received_at` が付くこと。
   このときの fake の返り値は **キーアクセスできる dict のリスト**にして、numpy structured array の
   読み方を再現する（属性アクセスの実装が紛れ込んだら落ちる）。
7. **バックフィルの取得失敗（None）は MT5ConnectionError**、空配列は 0 件で正常終了。
8. **接続とシンボル選択**
   `connect(symbol)` が `initialize()` と `symbol_select(symbol, True)` を呼ぶこと、
   それぞれが falsy を返したら `MT5ConnectionError` になることを fake で確認する。
   Market Watch 未選択のまま取得に進む退行をここで捕まえる。
9. **長い範囲は日単位に分割される**
   3 日分の範囲でバックフィルすると `copy_ticks_range` が 3 回呼ばれ、各呼び出しの範囲が
   連続かつ重複しないこと。1 回で全期間を要求する実装への退行を捕まえる。
10. **リポジトリへの `source` / `ingestion_run` の受け渡し**
   fake リポジトリが受け取った `source` と `ingestion_run` を記録し、同一 collector の
   複数回の書き込みで `ingestion_run` が同じ UUID であること（1 プロセス = 1 run）。
   両カラムは `NOT NULL` なので、渡し漏れは本番で `IntegrityError` になる。

`run()` は `poll_once` + `time.sleep` の薄い無限ループなので単体テストは書かない
（振る舞いは 2〜5 の `poll_once` テストで固定される）。

fake リポジトリは `insert_many(ticks, *, source, ingestion_run)` のシグネチャで受け取り、
渡された `Tick` をそのまま貯める。#7 の protocol が変わったらこの fake も合わせて直す。

既存テストの変更は行わない。`tests/unit/test_invariants.py` は緩めない
（collector は `data` 層で、スキャン対象の `strategy` / `intelligence` に該当しない）。

`tests/broker/` への追加はしない（MT5 実機確認は Windows ホストでの手動実行に委ねる）。
`tests/integration/` への追加もしない（DB 書き込みの検証は #7 の担当範囲）。

---

## 制約チェックリスト（実装時に満たすこと）

- [ ] `migrations/` に触れていない
- [ ] collector に生 SQL がない（書き込みは #7 の protocol 経由のみ）
- [ ] DSN は `TRADING_DB_DSN` から読む。credential・口座番号・DSN のハードコードなし。ログにも出さない
- [ ] `datetime.now()` の直呼びなし（`Clock` 注入、本番は `SystemClock`）
- [ ] すべての `Tick` に `received_at` が入っている（#7 のリポジトリは `known_time` を書くので、
      未設定だと broker 時刻が受信時刻として記録され PIT 可視性が壊れる）
- [ ] `insert_many` に `source` / `ingestion_run` を毎回渡している（両方 `NOT NULL`）
- [ ] 価格は `Decimal`。MT5 の float から `Decimal(str(x))` で変換する（`mapper.py` と同じ流儀）
- [ ] `USDJPY` / pip size / 時間足のハードコードなし（symbol は CLI か config から）
- [ ] Strategy / intelligence 層から collector への import を作らない
- [ ] collector が `MT5ExecutionAdapter`（`order_send` を持つ発注面）を保持していない
- [ ] `collector.py` のモジュールレベルで `psycopg` / `MetaTrader5` を import していない
- [ ] `--backfill-from` / `--backfill-to` が naive datetime と `from >= to` を境界で reject する
- [ ] `ruff check .` clean、`pytest -q` が baseline（206 passed / 1 skipped）から増分のみ増える

## コミット・PR

- コミット: `feat: USD/JPY Tick collector（MT5ポーリング + PIT保存）` + 本文に `Fixes #8`（Attribution フッターなし）
- PR 本文の先頭に `Closes #8`、作成直後に `gh pr comment <PR番号> --body "@codex review"`
- `tasks/APPROVAL.md` / `tasks/PARENT-NOTES.md` はコミットに含めない。
  **これらは `.gitignore` に載っていない**（確認済み）ので、`git add -A` / `git add .` は使わず
  対象ファイルを明示的に `git add` する。コミット前に `git status` で混入していないか見る
- lefthook の pre-commit（ruff）/ pre-push（pytest）を通す。`--no-verify` は使わない
