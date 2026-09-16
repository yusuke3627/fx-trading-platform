# issue #77: research の settle 待ちを symbol 単位にスコープする（advisory lock プロトコル）

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-77-symbol-scoped-settle-wait`
- ブランチ: `feat/issue-77-symbol-scoped-settle-wait`（base は `origin/main` = `b278551`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #77 本文（全文転記）

> タイトル: research の settle 待ちを symbol 単位にスコープする（advisory lock プロトコル）
>
> ## 背景
>
> PR #76 のレビューで指摘された内容。research の `stream_between` は開始時にデータ集合をピンするため、`market_ticks` に RowExclusiveLock を持つ書き込みトランザクション（ピン時刻より前に開始）の決着を待つ。未コミット行の symbol・期間は外部から観測できないため、**別通貨ペアや対象期間外の backfill も区別せず待ち、60 秒で明示エラーにする**保守的な実装になっている。
>
> ## 制約の内容
>
> - research 実行の開始と、大規模 backfill（数時間規模）は併走できない（research 側が timeout で明示的に拒否される。静かな汚染はない）
> - 開始後は併走可能（ピン済み集合は id 天井で保護され、削除・更新は指紋照合で検出される）
>
> ## 対応方針（必要になったら）
>
> 書き込み側（collector poll / backfill）が symbol 単位の advisory lock（例: `pg_advisory_xact_lock(hashtext('market_ticks:' || symbol))`）を取り、`stream_between` は対象 symbol のロックだけを待つ。書き込み 2 経路と読み手の 3 者に跨るプロトコル変更のため、併走が実運用で必要になった時点で着手する。

## 着手の理由

USDJPY の replay 実行中に EURUSD の過去データ取り込みを併走させる必要が生じ、issue #77 が着手条件とした「併走が実運用で必要になった時点」に到達した。現行実装は、天井読み以前に開始した EURUSD の書き込みトランザクションが決着しない限り USDJPY replay の開始も待たせ、60 秒で明示エラーにする。

---

## 現状の把握（実コード）

### 書き込み経路

`market_ticks` へ INSERT する SQL 文は本番コードに 1 文しかない。

```bash
rg -n -i '(insert into|copy|merge into|update|delete from|truncate)\s+"?market_ticks' src scripts tests migrations
```

- 本番の INSERT: `src/trading/storage/postgres.py` の `PostgresMarketTickRepository.insert_many`（444 行付近）
- 本番の DELETE / UPDATE: 無し
- `scripts/` と `migrations/` に tick の投入経路は無い（migration はテーブル定義と列・制約・索引の変更のみ）
- テスト: `tests/integration/test_market_repositories.py` の `store()` が `insert_many` を使い、未コミット writer・削除・更新を再現するテストだけが生 SQL を使う

本番の実行経路は 3 つで、いずれも `MarketTickRepository` プロトコル経由。

| 経路 | 呼び出し元 | トランザクション形状 |
| --- | --- | --- |
| collector poll | `data/market/collector.py` の `TickCollector._write`（`poll_once` から） | `insert_many` が自分で commit |
| collector backfill（MT5） | 同 `_write`（`backfill` から。`INSERT_CHUNK_SIZE=10_000` 単位） | チャンクごとに commit |
| Dukascopy 取り込み | `data/market/dukascopy.py` の `import_range`（276 行付近） | 1 時間分ごとに commit。先行する `bounds_between` の SELECT が同一接続でトランザクションを開くため、HTTP 取得中も `xact_start` が古いまま残る。ただしその段階では RowExclusiveLock も advisory lock も持たない |

### 読み手（`stream_between`）の現状

`src/trading/storage/postgres.py:521` 付近。

1. `SELECT max(id) AS ceiling, clock_timestamp() AS pinned_at FROM market_ticks` を実行して commit。天井は全 symbol 共通で、NULL なら空のまま終了
2. `pg_locks` × `pg_stat_activity` を 0.1 秒間隔で見て、`market_ticks` に RowExclusiveLock を持ち `xact_start <= pinned_at` のトランザクションが 0 になるまで待つ。60 秒（`_STREAM_SETTLE_TIMEOUT_SECONDS`）で `RuntimeError`
3. 対象 symbol・期間・`id <= ceiling` の件数と `sum(hashtext(market_ticks::text))` を取る
4. 50,000 行ずつ keyset ページングし、最後にもう一度件数と指紋を取り直して差があれば `RuntimeError`

`stream_between` は generator なので、ここでいう「開始」は generator を作った時点ではなく**最初の `next()` の時点**。末尾の照合は最後まで消費した場合だけ走る。利用者は research CLI と 4 つの study、それに `data/market/bar_service.py:168` の bar backfill。

---

## 設計判断

### 1. ロックを取る書き込み経路

`PostgresMarketTickRepository.insert_many` の 1 か所に置く。本番の書き込み経路 3 つがすべてこのメソッドを通り、INSERT の SQL 文自体がここにしかないため、ここが唯一の絞り込み点になる。

キーは引数ではなく**挿入する行の `tick.symbol`** から導出する（`{tick.symbol for tick in ticks}`）。バッチに複数 symbol が混ざっても構造的に取りこぼさないため。取得は `cursor.executemany(...)` の**直前・同一トランザクション内**で、メソッド末尾の `commit()` がそのまま解放点になる。id は INSERT 時に採番されるので「ロック取得 → id 採番 → commit → ロック解放」の順序になる。これが読み手側の正しさの土台。

**取得順は symbol 名順ではなく実際のロックキー（`hashtext` の値）の昇順にする。** 名前順だと衝突時にデッドロックしうる: `hashtext(A) = hashtext(C) = K1`、`hashtext(B) = K2` で、バッチ `[A,B]` は K1→K2、`[B,C]` は K2→K1 と逆順に取りに行く。キーの値で揃えればこの循環は起きない。実装は「キー一覧を 1 回の SELECT で取ってソート」→「順に `pg_advisory_xact_lock`」の 2 段にする。副作用のある関数呼び出しを SELECT の行評価順に依存させない。

既存の `insert_raw_archive`（postgres.py:1038）が `pg_advisory_xact_lock(hashtext(...), hashtext(...))` → INSERT → commit という同じ形をしているので、書き方はそれに合わせる。

### 2. `stream_between` が待つ対象と timeout

`pg_locks` の観測をやめ、**読み手自身が対象 symbol のキーを取って即座に手放す**。

```python
ceiling_row = ...  # SELECT max(id) AS ceiling FROM market_ticks
self._conn.commit()
if ceiling is None:
    return
try:
    with self._conn.transaction():
        self._conn.execute(
            "SELECT set_config('lock_timeout', %s, true)",
            (f"{int(_STREAM_SETTLE_TIMEOUT_SECONDS * 1000)}ms",),
        )
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (_TICK_ADVISORY_LOCK_CLASS_ID, symbol),
        )
except psycopg.errors.LockNotAvailable as exc:
    raise RuntimeError(...) from exc  # symbol と待機上限を含む文面
```

- `_STREAM_SETTLE_TIMEOUT_SECONDS = 60.0` の名前・値・「timeout は `RuntimeError` で落とす」契約を維持し、実現手段だけ「0.1 秒ポーリング + `time.monotonic()` 期限」から「`lock_timeout` + ブロッキング取得」に置き換える。`SET LOCAL` は bind パラメータを取れないため `set_config(..., true)`（トランザクションローカル）を使い、SQL に値を文字列結合しない
- `with self._conn.transaction():` が commit とエラー時の rollback を持つ（`insert_raw_archive` と同じ書き方）。直前に commit 済みなので savepoint ではなく本物のトランザクションになる
- `lock_timeout` が縛るのは**この 1 回のロック待ち**であって、メソッド全体の所要時間ではない。取得できた時点で commit して手放すので、`yield` より先にロックは残らない
- エラー文面に symbol を含める。翻訳するのは 55P03（`LockNotAvailable`）だけで、他の例外はそのまま伝える

**正しさ**（前提: 本番の writer が全員このプロトコルに参加し、id は `GENERATED ALWAYS AS IDENTITY` の既定＝増分 1・CACHE 1・NO CYCLE で採番される。`migrations/0001_initial.sql:90`。適用済み DB でも `seqcache=1, seqincrement=1, seqcycle=false` を確認済み）:

1. 天井読みの時点を T0、そこで見えた最大 id を C とする。C は「T0 までに採番された id の最大値」以下
2. 読み手がキーを取得できた時点を T（> T0）とする。T より前に同じキーを保持していた writer は、その時点で全員 commit / rollback 済み。PostgreSQL の commit は `RecordTransactionCommit → ProcArrayEndTransaction → ロック解放` の順なので、ロックが空いていることは「その行はもう見える／もう無い」を含意する
3. T 以降に初めてキーを取る writer は id の採番も T 以降になる。CACHE 1・増分 1 なので新しい id は C より大きい
4. よって T の時点で「対象 symbol かつ `id <= C`」の集合は確定する。他 symbol の未コミット行が残っていても、ストリームの `WHERE symbol = %s` に掛からないので無関係

**順序は逆にできない。** 先にキーを取って手放してから天井を読むと、その間に対象 writer が X を採番して未コミットのまま、別 symbol の writer が Y > X を commit して C >= X になる並びが作れる。天井読みが先、ロック取得が後。

**過剰に待つ側の変化**: 時刻条件を外したので、T0 より後にキーを取った同一 symbol の writer や、対象期間外だけを書く writer も、保持中なら待つ。id が C より大きいと分かっている相手を待つぶんだけ現行より保守的になるが、writer のトランザクションは INSERT 1 回分なので実害は小さい。

**削除する仕掛け**: `clock_timestamp()` の `pinned_at` と `xact_start <= pinned_at` の絞り込みは `pg_locks` 観測の過剰待ちを抑えるためだけに存在する。ロックの取得順序で直接議論できるようになるため、両方とも消す。`time` の import もこのポーリング以外で使っていなければ消す。

### 3. 書き込み経路の取りこぼしをどう担保するか

1. **構造**: `INSERT INTO market_ticks` は `insert_many` の 1 文だけで、キーは挿入行の symbol から導出する。この文を通る限り取りこぼしは起きない
2. **書き込み側の回帰テスト**: 外部セッションが対象 symbol のキーを保持している間、`insert_many` がブロックし、かつ **identity sequence の `last_value` が進まない**ことを実 DB で確認する。「ブロックする」だけだと INSERT の後にロックを取る誤実装も通ってしまうため、採番より前に取っていることまで見る
3. **ソース走査テスト**: `tests/unit/test_invariants.py` の既存方式（ソースを読んで禁止パターンを検出する）に倣い、`src/trading` 配下で `INSERT INTO market_ticks` が `storage/postgres.py` の 1 か所にしか現れないことを検査する。新しい書き込み経路を足した時点で落ちる
4. **残余リスク（正確に書く）**: リポジトリを経由しない書き込み（psql の手作業、将来のスクリプト、旧バイナリ）は待たれない。末尾の件数・指紋照合は**開始時の集計から終了時の集計までに可視化された変化しか検出しない**ので、非参加 writer が終了時の集計より後に commit すれば、その run は成功したまま「同じ天井で後から集合が増える」状態になりうる。列挙を途中で止めた場合は末尾の照合自体が走らない。つまり「取りこぼしても必ず `RuntimeError`」とは言えない。保証は**全 writer がプロトコルに参加していること**に依存する。これを ADR に明記する（`known_at <= replay_clock.now()` という PIT の時刻条件の違反ではなく、research 集合の再現性の問題である点も併記する）

### 4. `hashtext` 衝突時の挙動

同じ symbol を同じ式でキー化する限り、衝突は**待つ相手を増やす方向にしか働かない**。

- 衝突した別 symbol の writer を待つ ＝ 過剰待ち。最悪ケースは 60 秒後の明示エラーで、現行の「全 writer を待つ」実装と同じ安全側の失敗
- 逆（待つべき writer を見逃す）は起きない。衝突は 2 つの symbol を同じキーに**まとめる**方向にしか働かず、キーを分ける方向には働かないため
- 読み手と書き手は同一 DB 上の同じ式でキーを作るので、両者のキーが食い違うことはない
- ただし「衝突は過剰待ちだけ」と言えるのは、取得順を実キー順に統一した後（§1）。名前順のままだと衝突がデッドロックの原因になりうる

ADR に「衝突の帰結は liveness の劣化だけで safety は劣化しない。ただし取得順を実キー順に揃えることが条件」と記録する。

### 5. キー形式

`docs/SYSTEM_SPEC.md` §8.5 に合わせて 2 引数版を使う。

- `classid = 0x5449434B`（ASCII `TICK`、10 進 1414087499）
- `objid = hashtext(symbol)`（符号付き 32bit のまま使う）

§8.5 の「objid は subsystem 内で 1 から採番」は固定用途のロック向けの規約で、本 subsystem は symbol ごとにキーが要る。同じ形（objid に `hashtext` を使う）は既存の `insert_raw_archive` にも前例がある。この点を ADR に書く。1 引数版と 2 引数版は `pg_locks` 上で `objsubid` の違う別空間なので、issue 本文の例（1 引数版）から形式を変えても OMS の dispatcher lock とは衝突しない。

### 6. 実 PostgreSQL での事前確認（PG 14.18、この worktree で実測済み）

別 symbol のキー保持中でも取得は 0.001 秒、同一 symbol 保持中は `lock_timeout=250ms` で 0.402 秒後に `psycopg.errors.LockNotAvailable`（SQLSTATE 55P03）、保持側の rollback 後は 0.001 秒で取得。`pg_locks` 上は `classid=1414087499, objsubid=2` で見える。

---

## 変更する対象

### `src/trading/storage/postgres.py`

- `_TICK_ADVISORY_LOCK_CLASS_ID = 0x5449434B` を `_OMS_ADVISORY_LOCK_CLASS_ID` の隣に、同じ書式のコメントで追加
- `insert_many`: `executemany` の直前に、挿入行の symbol から求めたキーを昇順で `pg_advisory_xact_lock(classid, key)`
- `stream_between`: `clock_timestamp()` と `pg_locks` ポーリングを廃止し、`set_config('lock_timeout', ...)` + `pg_advisory_xact_lock` の取得＆即解放に置き換える。`LockNotAvailable` を `RuntimeError` に翻訳する
- `_STREAM_SETTLE_TIMEOUT_SECONDS` のコメントと `stream_between` 冒頭のコメントを、新しい正しさの議論（天井読みが先・ロック順序・他 symbol は無関係・保証は全 writer の参加に依存する）に書き直す
- 不要になった import を整理する（`ruff` で確認）

### `tests/integration/test_market_repositories.py`

追加・更新（既存の pin / 削除 / 更新検出テストは変更しない）:

1. `test_stream_does_not_wait_for_another_symbols_writer`（**必須**）: 別 symbol の参加 writer が未コミットのまま（キー保持 + INSERT）でも、対象 symbol の stream が待たずに最後まで正しい集合を返す
2. `test_stream_refuses_to_start_over_an_unsettled_write`（既存を書き換え）（**必須**）: 同一 symbol の参加 writer が未コミットなら待ち、`_STREAM_SETTLE_TIMEOUT_SECONDS` を 0.5 秒に monkeypatch した状態で `RuntimeError`。文面に symbol が入ること、原因が 55P03 であること、その後 writer を解放すれば同じ接続で再実行できることを確認する
3. `test_stream_waits_for_a_writer_that_began_after_the_readers_transaction`: 既存の `test_pin_instant_is_the_ceiling_read_not_the_transaction_start` を置き換える。読み手側に先行 SELECT のトランザクションを開いてから writer が参加・未コミットになる並びで、writer を見逃さず待つ（＝時刻条件を外しても要件が残ることの回帰）
4. `test_stream_returns_a_row_committed_below_the_ceiling`: A が対象 symbol のキーを取って X を INSERT（未コミット）→ B が別 symbol に Y > X を INSERT・commit（天井が Y になる）→ 別スレッドが 0.5 秒後に A を commit → stream が待ってから X を含む正しい集合を返す。採番順と commit 順の逆転、天井の時点、待機後の可視性をまとめて検証する
5. `test_insert_many_waits_for_the_symbol_write_lock`: 外部セッションが `pg_advisory_lock`（session 版）で対象 symbol のキーを保持している間、別スレッドの `insert_many` が完了せず、**identity sequence の `last_value` も進まない**こと。解放後に正しい件数で完了すること。別 symbol のキー保持中はブロックしないこと
6. `test_insert_many_takes_every_symbols_lock_in_the_batch`: 2 symbol 混在バッチで、どちらの symbol のキーを保持していても `insert_many` がブロックする

並行テストの共通条件: writer・reader・観測用で接続を分ける（1 接続を複数スレッドで共有しない）。同期は `threading.Event` と join の timeout で行い、待ち時間の sleep を成否の根拠にしない。`repos` fixture は 1 symbol しか後片付けしないので、追加で使った symbol は各テストで消す。

### `tests/unit/test_invariants.py`

`src/trading` 配下で `INSERT INTO market_ticks` が `storage/postgres.py` の 1 か所にしか現れないことの検査を追加する。既存テストは緩めない。

### `docs/adr/ADR-041-market-ticks-symbol-scoped-write-lock.md`（新規）

`docs/SYSTEM_SPEC.md` は v2.0 で凍結済みなので本文は変更せず ADR を追加する。番号は追加直前に `ls docs/adr` で最大値を再確認する（並行作業の worktree が 2 本あるため）。書く内容:

- Context: 現行の保守的な settle 待ちと、USDJPY replay × EURUSD 取り込みの併走要求
- Decision: `insert_many` が挿入行の symbol のキー（`classid="TICK"`, `objid=hashtext(symbol)`）を id 採番より先に取り、commit まで保持する。`stream_between` は天井を読んだ後で同じキーを `lock_timeout` 付きで取得し、即解放する
- 前提: 本番の writer が全員参加すること、id が増分 1・CACHE 1 で採番されること（既存の天井方式が元から依存している前提）。reader と writer は同じチェックアウトから動くので、`git pull` 後は collector を再起動してから research を回す
- Consequences: 別 symbol の併走が可能 / 同一 symbol と対象期間外の同一 symbol 書き込みは保持中なら待ち、60 秒で明示エラー / `hashtext` 衝突は過剰待ちのみで safety は劣化しない（取得順を実キー順に揃えることが条件）/ **非参加 writer は待たれず、末尾の指紋照合も「終了時の集計までに可視化された変化」しか検出しないため、取りこぼしが必ず loud に落ちるとは言えない** / §8.5 の objid 採番からの逸脱と `insert_raw_archive` の前例

---

## やらないこと

- PIT の可視性ルール（`known_at <= replay_clock.now()`）の変更
- 指紋照合・id 天井・ページング方式の作り替え
- `migrations/` への追加（advisory lock はスキーマを必要としない）。sequence 設定の実行時アサートも足さない（`0001_initial.sql` が正本で、既存の天井方式が元から依存している前提を ADR に書くにとどめる）
- `market_bars` など他テーブルへの横展開、`insert_raw_archive` の既存ロック方式の変更
- 接続の `autocommit` モードの検証分岐（`connect()` が唯一の生成点で、内部の不変条件に防御的分岐を足さない）
- 衝突によるデッドロックの再現テスト（実 DB で安定して再現できずテストが flaky になる。取得順を実キー順にすることで構造的に防ぐ）
- 周辺リファクタ、無関係な整形
- VPS 上での実行・確認（長時間 replay が 4 本走っているため触らない）

---

## 検証（すべて Mac ローカル・この worktree 内）

```bash
W=/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-77-symbol-scoped-settle-wait
$W/.venv/bin/ruff check .
$W/.venv/bin/pytest tests/unit tests/replay tests/failure
TRADING_DB_DSN=postgresql:///fx_itest_77 $W/.venv/bin/pytest tests/integration
```

- 使い捨て DB `fx_itest_77` は作成済み（`migrations/*.sql` 適用済み、PostgreSQL 14.18、`market_ticks` の sequence は `seqcache=1, seqincrement=1, seqcycle=false`）。Mac の収集用 DB（環境変数の `TRADING_DB_DSN`）には触れない
- integration が skip されていないこと（`-q` の結果に skip が出ていないこと）を確認する
- 追加した integration テストの所要時間を測って PR 本文に書く
- 完了条件: ruff 無指摘、`tests/unit tests/replay tests/failure` green、上記 DSN で `tests/integration` green

## コミット・PR

- PR 作成前にリポジトリ同梱の `code-review-expert` と `AGENTS.md`「AIレビュー指示」の観点で差分をレビューする
- コミットメッセージに `Fixes #77`、PR 本文の先頭に `Closes #77`
- PR 作成直後に `gh pr comment <PR番号> --body "@codex review"`
