# issue #7 実装計画 — PIT市場データschema（market_ticks/market_bars）とBarデータ接続

対象 issue: https://github.com/yusuke3627/fx-trading-platform/issues/7
ブランチ: `feat/issue-7-pit-market-data-schema`
ベースライン: `origin/main` = 3b8fc1c、`pytest -q` は 206 passed / 1 skipped（測定済み）

---

## 1. 調査で分かった前提（計画に効くもの）

- **`market_ticks` / `market_bars` は 0001_initial.sql に既に存在する。** issue は「新規作成」の
  つもりで書かれているが、実際には 0002 は `CREATE TABLE` ではなく **`ALTER TABLE`** になる。
  - 既存 `market_ticks`: `id BIGINT GENERATED ALWAYS AS IDENTITY PK / symbol / bid / ask /
    tick_time / received_at` ＋ `idx_market_ticks_symbol_time (symbol, tick_time)`
  - 既存 `market_bars`: `id / symbol / timeframe / start_time / open / high / low / close /
    tick_volume` ＋ `UNIQUE (symbol, timeframe, start_time)`
- **この2テーブルを参照するコード・テスト・ドキュメントは 0001 以外に一切ない**
  （`rg "market_ticks|market_bars"` のヒットは 0001 のみ）。したがってカラム名変更は
  コード側の追随作業を伴わず、実質ゼロリスク。
- CI の `migration-check` は `migrations/*.sql` を空の PostgreSQL 17 へ順に流すだけ。
  pytest は別 job で走り DB を持たないため、**`tests/integration` は CI でも実行されない**。
- **ローカルに PostgreSQL 14 が起動している**（`psql` / `createdb` 利用可）。計画段階で
  scratch DB へ 0001 → 5章 Step 1 の 0002 を実際に流して**適用可能なことを確認済み**:
  - 0001 が付ける UNIQUE 制約の自動生成名は `market_bars_symbol_timeframe_start_time_key`
    （実測。`RENAME CONSTRAINT` の対象名はこの推測ではなく実測値）
  - `RENAME COLUMN` に既存 index `idx_market_ticks_symbol_time` が追随し
    `(symbol, event_time)` になることを実測
  - 空テーブルへの `ADD COLUMN ... NOT NULL`（DEFAULT なし）が通ることを実測
- `tests/unit/test_invariants.py` の禁止文字列スキャン対象は `src/trading/{strategy,intelligence}`
  のみ。本計画が触る `strategy/base.py` へ `trading.domain.market` の import を足しても
  禁止語（`trading.storage` / `psycopg` / `datetime.now(` 等）に当たらず、
  `StrategyContext` のフィールドも変えないため不変条件テストへの影響はない。
- lefthook の `no-migration-rewrite` は「origin/main に存在する migration の変更・削除」だけを
  止める。新規 `0002_*.sql` の追加は通る。
- `Bar.close_time` = `start + TIMEFRAME_SECONDS[timeframe]`（`domain/market.py`）。
  `InMemoryMarketData.bars()` は `close_time <= clock.now()` で可視性を判定し、
  `ReplayEngine.replay_time()` も Bar を `close_time` で配信する。
  → schema の `end_at` / `known_at` は**この導出値をそのまま書くだけ**にし、SQL 側に
  timeframe→秒 の対応表を二重に持たない。
- Timeframe の選択は設定が持つ（`PROJECT_STRUCTURE.md`「Strategy configuration owns
  timeframe selection」／`StrategyConfig.timeframes: TimeframeMap`）。エンジンが構築する
  Bar の時間足も設定から導出し、コードに `"1m"` 等を埋め込まない。

---

## 2. 設計判断（レビューで潰すべき点はここ）

### 2-1. 0002 は ALTER で既存テーブルを PIT 形状へ寄せる
0001 は適用済みのため書き換え禁止。0002 で `RENAME COLUMN` ＋ `ADD COLUMN` ＋ 制約追加を行う。
両テーブルとも書き込みコードが存在せず実運用データが無いので、`ADD COLUMN ... NOT NULL`
（DEFAULT なし）を使ってよい。万一行があれば migration が明示的に失敗する＝望ましい失敗の仕方。

### 2-2. issue 本文からの意図的な逸脱（3点）
| issue の記述 | 本計画 | 理由 |
| --- | --- | --- |
| `tick_id BIGSERIAL PK` | `id`（既存）を維持 | 0001 の全テーブルが `id` 命名で、既存カラムは `GENERATED ALWAYS AS IDENTITY`（BIGSERIAL の現代版）。改名は churn のみで利得なし |
| `bid/ask NUMERIC(12,5)` | 桁指定なし `NUMERIC` を維持 | プラットフォームは通貨ペア非依存。5桁固定は 6桁以上で見積もる建値を**黙って丸める**。0001 も全価格が桁指定なし `NUMERIC` |
| `spread` / `last_price` / `flags` | **3列とも追加しない**（最終承認で確定） | 4章 ASK-1 のとおり writer が居ない。実 writer が現れた時点で `ALTER TABLE ADD COLUMN` 1本と一緒に足す |
| `(symbol, event_time)` index を維持 | 既存 `idx_market_ticks_symbol_time` を**削除**し、UNIQUE 制約の index に兼務させる | 下記 2-3 |

`event_time` / `start_at` / `end_at` / `known_at` への改名は issue どおり実施する
（参照ゼロで無コスト、かつ 0001 の `known_at` / `observed_at` / `effective_at` の `_at` 規約に揃う）。

### 2-3. tick の重複 index を落とす
issue が要求する 2 つ — index `(symbol, event_time)` と UNIQUE `(symbol, event_time, bid, ask)` —
は、後者の btree が前者を**先頭2列として完全に包含する**（実測で確認）。両方を残すと、
本システムで最も行数が増えるテーブルに対し、同じ先頭列の index を 2 本維持することになり、
insert ごとの書き込み増幅とディスクを二重に払う。後者だけで
`WHERE symbol = ? AND event_time BETWEEN ? AND ?` の range scan は同じ効率で処理できる。
→ 0002 で `DROP INDEX idx_market_ticks_symbol_time` する（→ 4章 ASK-3）。

### 2-4. Bar の `end_at` / `known_at` はリポジトリが `Bar.close_time` から導出する
`end_at = known_at = bar.close_time`。ドメイン規約が唯一の正本になり、SQL と Python で
timeframe 定義が二重化しない。schema 側は矛盾検出のための CHECK だけ置く
（`end_at > start_at`、`known_at >= end_at`）。`known_at >= end_at` にするのは、将来
遅延受信した Bar を保存するとき「終値確定より前に知っていた」データだけを弾くため。

### 2-5. BarBuilder は bid ベース
MT5 の FX チャート（`copy_rates`）は bid 系列。mid を使うと (a) live の MT5 由来 Bar と
replay の合成 Bar が系統的にズレる（indicators の「Backtest・Live 間の計算乖離を防ぐ」原則に反する）、
(b) `(bid+ask)/2` が InstrumentSpec の `digits` より 1 桁多い値を生む場合がある。
→ **OHLC は `tick.bid` から構築**し、その理由を docstring に 1 行残す。

### 2-6. BarBuilder のバケツ規則
- バケツ境界は **broker 時刻（`tick.time`）** を epoch 秒で `TIMEFRAME_SECONDS[timeframe]`
  に floor して決める（UTC 基準。JST 変換は Risk Day・表示のみという SPEC に従う）。
- **完成した Bar だけを返す。** 開いているバケツより新しい broker 時刻の tick が来た瞬間に
  直前のバケツを Bar として emit する。未完成バケツを吐き出す `flush()` は**作らない**
  （作れば「完成 Bar のみ公開」を破る口になる）。
- ticks が無いバケツの Bar は生成しない（fill-forward しない）。
- 既に閉じたバケツに属する遅延 tick は**捨てる**（確定した Bar を書き換えない）。
- `tick_volume` はバケツ内の tick 本数。
- API は `on_tick(tick) -> Bar | None`。開いているバケツは常に 1 個なので 1 tick から
  完成 Bar が 2 本以上出ることはない。

### 2-7. エンジンへの接続は StrategyConfig.timeframes 経由
`TimeframeMap` に `all() -> tuple[str, ...]`（重複排除＋`TIMEFRAME_SECONDS` 昇順で決定的に整列）
を追加し、`BacktestEngine._wire()` が `strategy_config.timeframes.all()` の各時間足へ
BarBuilder を 1 個ずつ作る。`handle()` では `market.add_tick()` の直後・strategy 評価の前に
完成 Bar を `market.add_bar()` する。

- BarBuilder は `_Wiring` に持たせる＝run ごとに新規生成 → 決定性を維持。
- `run.py` の `ScriptedStrategy` は `timeframes` 未設定なので `all()` は空 → Bar は生成されず、
  既存の vertical slice の出力（dataset hash・metrics）は不変。既存テストが変わらないことで確認する。
- 可視性は既存経路で守られる。根拠は次の2点で、`Tick` は `received_at >= time` を保証しない
  （broker 時刻が受信時刻より先に進んでいる skew があり得る）ため「emit 時点で clock は必ず
  `bucket_end` 以降」とは**言えない**ことに注意する:
  1. 完成 Bar の中身は**既に配信済みの tick だけ**から作られる（未配信 = clock 未到達の
     データは builder に入らない）。したがって未来の値が Bar に混入する経路が無い。
  2. `InMemoryMarketData.bars()` の `close_time <= now` フィルタが最終防壁。skew のある
     tick で emit された Bar は clock が `close_time` に達するまで単に**見えないだけ**で、
     早く見えることはない。

### 2-8. storage は protocol ＋ postgres 実装のみ（テストなし）
`repository.py` の既存 protocol（`EventRepository.known_before` 等）の流儀に合わせる。
psycopg がローカル未導入・CI の pytest job にも DB が無いため、
`tests/integration` へのテスト追加は**行わない**（実行されないテストを置かない）。
SQL の妥当性は CI の migration-check が担保する。

---

## 3. 変更対象ファイル一覧

新規:
- `migrations/0002_market_data.sql`
- `src/trading/data/market/bars.py`（BarBuilder）
- `tests/unit/test_bar_builder.py`
- `tests/replay/test_bar_feed.py`（エンジン受入テスト）

変更:
- `src/trading/storage/repository.py`（protocol 2 個追加）
- `src/trading/storage/postgres.py`（実装 2 クラス＋行マッパ 2 個追加）
- `src/trading/backtest/engine.py`（`_Wiring` に builders、`_wire()` と `handle()` の配線）
- `src/trading/strategy/base.py`（`TimeframeMap.all()` 追加）
- `tasks/issue-7.md`（本ファイル）

変更しない（明示）:
- `migrations/0001_initial.sql`（適用済み）
- `docs/SYSTEM_SPEC.md`（v1.3 凍結。カラム定義は元々書かれておらず、
  「可視性 = `known_at <= replay_clock.now()`」の記述と本変更は整合するので ADR も不要）
- `src/trading/domain/market.py`（`Bar` / `Tick` にフィールドを足さない）
- `config/*.yaml`（新規キーを増やさない）

---

## 4. ASK（`tasks/APPROVAL.md` で確定済み）

**確定結果**: ASK-1 = 3列とも入れない / ASK-2 = 承認（既存 `id` 維持・桁指定なし `NUMERIC`）/
ASK-3 = `DROP INDEX` 承認 / `insert_many -> int` 承認。以下は判断に至った経緯の記録。

- **ASK-1 → 確定: 3列とも入れない。** 経緯: 暫定回答「入れる」の根拠が #8 側の回答で崩れ、再判断となった。
  - 暫定回答（APPROVAL.md）の根拠: 「#8 の collector が MT5 raw の flags / last を書くため、
    今入れないと 0003 が即必要になる」
  - **#8 担当の回答: #8 では flags / last を書かない。** 根拠3点 —
    (a) `last` は FX では常に 0.0（OANDA の FX 気配に最終約定価格は載らない）、
    (b) `flags` を domain `Tick` に足すと `Tick` は Strategy が `context.market.ticks()` で
    読むオブジェクトなので "Strategy implementation must not know the Broker" に反する、
    (c) storage 層に raw 書き込み口を作る案は「今は誰も読まないデータのための抽象」になる
  - **#7 の見解: #8 の (b)(c) に同意。**ただし (b) は自動テストの違反ではない
    （`test_invariants.py` の禁止文字列スキャンは `strategy/` `intelligence/` のみが対象で、
    `domain/market.py` へのフィールド追加は検出しない）。`PROJECT_STRUCTURE.md` の
    文書化された原則に反する、というのが正確な位置づけ。
  - **#7 の推奨: 3列とも入れない。** 「入れないと 0003 が即必要」のコストは
    `ALTER TABLE ... ADD COLUMN` 数行の migration 1本で、実書き手が現れた時点で
    その writer と一緒に足すほうが、誰も書かない列を3つ抱えるより安い。
    なお当初 ASK-1 が3列を一括で扱ったのは framing の誤りで、本来 `market_bars.spread`
    （MT5 `copy_rates` が返す実データで FX でも意味がある）と `market_ticks.last_price`
    （FX では常に 0.0 で本質的に無意味）は別評価すべきだった。ただし **#7 も #8 も
    spread を書かない**（#8 は tick collector、#7 の BarBuilder は Bar に spread を持たない）
    ため、「今回は3列とも入れない」で結論は揃う。
- **ASK-2 → 確定: 承認**（既存 `id` 維持 / 桁指定なし `NUMERIC`）。`spread` NULL 許容の論点は ASK-1 の確定により消滅。
- **ASK-3 → 確定: DROP する。** migration 内に理由をコメントで残すこと（対応済み）。

---

## 5. 実装ステップ

### Step 1: `migrations/0002_market_data.sql`

```sql
-- 0002_market_data.sql
-- Point-in-time market data: tick provenance and bar visibility.
-- 0001 created market_ticks / market_bars in a minimal shape; this migration
-- brings them to the PIT contract, where a bar is known at its own close.
-- All timestamps are timestamptz (UTC); JST is a display and risk-day concern.

BEGIN;

ALTER TABLE market_ticks RENAME COLUMN tick_time TO event_time;

-- Provenance of the ingesting batch: a run that recorded bad data has to be
-- identifiable and removable after the fact.
ALTER TABLE market_ticks
    ADD COLUMN source        TEXT NOT NULL,
    ADD COLUMN ingestion_run UUID NOT NULL;

-- Re-ingesting an archive must not duplicate quotes. A repeated
-- (symbol, event_time) carrying a DIFFERENT bid/ask is a genuine second
-- quote within the same second and is kept.
ALTER TABLE market_ticks
    ADD CONSTRAINT market_ticks_quote_key UNIQUE (symbol, event_time, bid, ask);

-- The constraint's index leads with (symbol, event_time) and serves every
-- lookup the dropped one did. Ticks are the highest-volume inserts in the
-- system, so the table must not carry two indexes on the same leading columns.
DROP INDEX idx_market_ticks_symbol_time;

ALTER TABLE market_bars RENAME COLUMN start_time TO start_at;
ALTER TABLE market_bars
    RENAME CONSTRAINT market_bars_symbol_timeframe_start_time_key
    TO market_bars_symbol_timeframe_start_at_key;

-- end_at and known_at are written from the domain's Bar.close_time
-- (start + TIMEFRAME_SECONDS[timeframe]), so the timeframe table is not
-- duplicated here. The checks only reject rows that contradict it.
ALTER TABLE market_bars
    ADD COLUMN end_at   TIMESTAMPTZ NOT NULL,
    ADD COLUMN known_at TIMESTAMPTZ NOT NULL,
    ADD CONSTRAINT market_bars_span_check CHECK (end_at > start_at),
    -- High, low and close exist only once the bar has closed: a bar cannot
    -- have been known before its own end.
    ADD CONSTRAINT market_bars_known_at_check CHECK (known_at >= end_at);

CREATE INDEX idx_market_bars_visibility ON market_bars (symbol, timeframe, known_at);

COMMIT;
```

補足:
- 上記 SQL は 0001 適用済みの scratch DB（ローカル PostgreSQL 14）へ**実際に流して適用成功を確認済み**。
  制約名・index 追随・NOT NULL 追加はいずれも実測。ASK-3 が却下なら `DROP INDEX` 行だけ落とす。
- `source` / `ingestion_run` を NOT NULL にするのは、取り込みバッチ単位で
  「どの実行が入れた行か」を後から削除・再取り込みできるようにするため。
- ローカル検証コマンド（実装時にも同じ手順で確認する）:
  ```bash
  createdb fx_migration_check
  psql -q -d fx_migration_check -v ON_ERROR_STOP=1 -f migrations/0001_initial.sql
  psql -q -d fx_migration_check -v ON_ERROR_STOP=1 -f migrations/0002_market_data.sql
  psql -d fx_migration_check -c '\d market_ticks' -c '\d market_bars'
  dropdb fx_migration_check
  ```

### Step 2: `src/trading/data/market/bars.py`

```python
class BarBuilder:
    """Tick -> Bar aggregation for one (symbol, timeframe)."""
    def __init__(self, symbol: str, timeframe: str) -> None: ...
    def on_tick(self, tick: Tick) -> Bar | None: ...
```
- バケツ境界: `epoch - epoch % TIMEFRAME_SECONDS[timeframe]` を UTC の datetime へ戻す
- OHLC は `tick.bid`、`tick_volume` は本数
- 新バケツ到来時のみ完成 Bar を返す。閉じたバケツ宛の遅延 tick は None を返して捨てる

### Step 3: `TimeframeMap.all()` を `strategy/base.py` に追加

```python
def all(self) -> tuple[str, ...]:
    """Distinct configured timeframes, shortest first (deterministic order)."""
```
`TIMEFRAME_SECONDS` を並べ替えキーにするため、表に無い時間足（YAML の打ち間違い等）では
KeyError になる。これは `Bar.close_time` の既存挙動と同じで、run 途中ではなく配線時に
落ちる分むしろ望ましい。追加の検証層は設けない。

### Step 4: `BacktestEngine` の配線
- `_Wiring` に `bar_builders: list[BarBuilder]`
- `_wire()`: `[BarBuilder(self._spec.symbol, tf) for tf in self._strategy_config.timeframes.all()]`
- `handle()`: `w.market.add_tick(item)` の直後に各 builder へ tick を流し、返った Bar を
  `w.market.add_bar(bar)`（strategy 評価より前）

### Step 5: storage
`repository.py`:
```python
class MarketTickRepository(Protocol):
    # 戻り値は「実際に挿入された件数」。issue #8 の collector が、バックフィル再実行時に
    # CLI へ「送った件数」ではなく「実際に埋まった件数」を出すために必要（先方から要求あり）。
    def insert_many(self, ticks: Sequence[Tick], *, source: str, ingestion_run: UUID) -> int: ...
    # `since` は必須。tick テーブルは無限に伸びるので、下限の無い可視性クエリを
    # 作らない（MarketDataService.ticks(symbol, window_seconds) と同じ窓の意味論）。
    def known_before(self, symbol: str, t: datetime, since: datetime) -> Sequence[Tick]: ...

class MarketBarRepository(Protocol):
    def insert_many(self, bars: Sequence[Bar]) -> None: ...
    def known_before(self, symbol: str, timeframe: str, t: datetime, count: int) -> Sequence[Bar]: ...
```
`postgres.py`:
- `PostgresMarketTickRepository.insert_many`: `executemany` ＋ プレースホルダ。
  `received_at` は `tick.known_time`（`received_at or time`）を書く＝可視性の定義と一致。
  `ON CONFLICT (symbol, event_time, bid, ask) DO NOTHING` で再取り込みを冪等に。
  戻り値は `cursor.rowcount`。**実測で確認済み**（ローカル PostgreSQL 14 + psycopg 3.3.4、
  一時 venv で検証後に破棄。worktree の `.venv` は CI と同一のまま psycopg 未導入）:
  - サーバ側: 4件中3件が重複の INSERT は `INSERT 0 1` を返す（実挿入のみ計上）
  - psycopg3: `executemany` 後の `rowcount` は 4件中2件新規で `2`、全件重複で `0`
  → 「送った件数」ではなく「実際に埋まった件数」が返るので、#8 の CLI 表示要件を満たす。
- `PostgresMarketTickRepository.known_before`: `WHERE symbol = %s AND event_time >= %s
  AND received_at <= %s ORDER BY event_time`。可視性の判定は **`received_at`**
  （tick の可視時刻は受信時刻 — SYSTEM_SPEC の規約）、範囲の絞り込みは `event_time`。
  後者が `market_ticks_quote_key` の先頭2列に一致するので range scan で済む。
  下限なしにすると銘柄の全履歴を毎回走査することになるため `since` を必須にしている。
- `PostgresMarketBarRepository.insert_many`: `end_at` / `known_at` に `bar.close_time` を書く。
  `ON CONFLICT (symbol, timeframe, start_at) DO NOTHING`（PIT ストアなので確定 Bar を上書きしない）
- `PostgresMarketBarRepository.known_before`: `WHERE symbol = %s AND timeframe = %s AND
  known_at <= %s ORDER BY start_at DESC LIMIT %s` を取得して反転（`MarketDataService.bars()`
  と同じ「直近 count 本を昇順で返す」意味論に合わせる）
- 行→ドメイン変換では `end_at` / `known_at` / `spread` / `last_price` / `flags` を読まない
  （`Bar` / `Tick` に対応フィールドが無く、`close_time` で再導出できるため）

---

## 6. テスト方針

**`tests/unit/test_bar_builder.py`（新規・必須）**
1. バケツが閉じるまで `on_tick` は None を返す（未完成 Bar を公開しない）
2. 完成 Bar の OHLC / `tick_volume` が投入 tick と一致し、`start` が時間足境界に揃う
3. 完成 Bar の `close_time` == バケツ終端（= schema の `end_at` / `known_at` 規約との整合）
4. tick が無いバケツをまたいだ場合、空 Bar を捏造しない
5. 既に閉じたバケツ宛の遅延 tick を捨てる（確定 Bar を書き換えない）
6. 最後の未完成バケツは最後まで emit されない

**`tests/replay/test_bar_feed.py`（新規・必須）**
1. 合成 tick でエンジンを回し、戦略が `context.market.bars(symbol, tf, n)` で Bar を読める
   （プローブ戦略が観測値を記録するだけで、シグナルは出さない）。
   **観測 Bar が空でないこと・本数が tick 数と時間足から期待される値と一致することを必ず表明する。**
   これが無いと 2・3 は「Bar が常に空」でも通ってしまう（vacuous test）。
2. 戦略が観測したどの Bar も `close_time <= clock.now()` を満たす（look-ahead 禁止の受入）
3. 同一 seed の 2 回の run で観測 Bar 列が完全一致（決定性）

**既存テストで守るもの**
- `tests/unit/test_backtest_run.py` / `tests/replay/test_vertical_slice.py` が無改変で通ること
  = `timeframes` 未設定の既存 run に副作用が無いことの証拠。
- `tests/unit/test_invariants.py` は緩めない（`StrategyContext` を変更しないので影響しない見込み）。

**やらないこと**
- `tests/integration` への postgres テスト追加（CI の pytest job に DB が無く、
  ローカルにも psycopg が無いため実行されない）

---

## 7. リスク

| リスク | 対応 |
| --- | --- |
| 0002 の `ADD COLUMN NOT NULL` が既存行のある DB で失敗する | 両テーブルとも書き込みコードが存在せず空。失敗した場合は行の存在＝想定外なので、握りつぶさず調査する |
| BarBuilder が strategy 評価より後に Bar を足すと、tick と Bar が 1 本ズレる | `handle()` 内の順序（add_tick → bars → strategy）をテスト 1・2 で固定 |
| 既存 vertical slice の出力が変わる | `timeframes` 空で builders ゼロ。既存テスト無改変通過で確認 |

## 8. 完了条件

- `ruff check .` が clean
- `pytest -q` が 206 passed から新規テスト分だけ増えて全 green（既存テストは無改変）
- `migrations/0002_market_data.sql` が 0001 の直後に空 DB へ適用できることを
  **ローカル psql（5章 Step 1 の検証コマンド）で確認**し、CI の migration-check でも green
- 新規テストが「Bar が空でも通る」形になっていない（6章の非空表明）
