# ADR-041: market_ticks の書き込みと settle 待ちを symbol 単位にする

**Status:** Accepted (2026-09-17)

## Context

`stream_between` は開始時に `market_ticks` 全体の最大 id を天井として読み、
その範囲のデータを短いトランザクションでページングする。従来は、天井読みより前に開始し、
同テーブルに `RowExclusiveLock` を持つトランザクションが決着するまで待っていた。
未コミット行の symbol や期間は外部から判別できないため、別 symbol の書き込みも待ち、
60 秒で `RuntimeError` になっていた。

USDJPY の replay と EURUSD の過去データ取り込みを併走させる必要が生じたため、
Issue #77 の方針に沿って待機対象を symbol ごとに分ける。
本番の tick 書き込みは collector poll、MT5 backfill、Dukascopy 取り込みのいずれも
`PostgresMarketTickRepository.insert_many` を通る。

## Decision

1. `insert_many` は挿入行の `tick.symbol` からキーを求め、INSERT による id 採番より先に
   `pg_advisory_xact_lock(classid, objid)` を取得する。`classid` は `0x5449434B`（ASCII `TICK`）、
   `objid` は `hashtext(symbol)` の符号付き 32bit 値とし、commit まで保持する。
   複数 symbol のバッチでは、1 回の SELECT で実キーの一覧を取得し、重複を除いて昇順に並べ、
   その順に個別にロックを取得する。symbol 名順や SELECT 内の関数評価順には依存しない。
2. `stream_between` は天井を読んで commit した後、別の短いトランザクションで
   対象 symbol の同じキーを取得する。`set_config('lock_timeout', ..., true)` で待機上限を
   60 秒に設定し、トランザクションを抜けて、行を返す前にロックを解放する。
   `LockNotAvailable`（SQLSTATE 55P03）だけを symbol と待機上限を含む `RuntimeError` に変換する。
   `pinned_at`、トランザクション開始時刻の比較、`pg_locks` のポーリングは使わない。
3. [SYSTEM_SPEC §8.5](../SYSTEM_SPEC.md#s8-5) の 2 引数版と subsystem tag の形式を使う。
   symbol ごとのキーが必要なので、同節の「objid は subsystem 内で 1 から採番」には従わず、
   `hashtext` を使う。`insert_raw_archive` にも同じ方式の前例がある。
   1 引数版とは別のキー空間であり、OMS の dispatcher lock とも subsystem tag が異なる。

### 保証の前提と取得順序

本番の writer は全員このプロトコルに参加する。id の採番は
`migrations/0001_initial.sql` の identity 定義に従い、増分 1、CACHE 1、NO CYCLE とする。
これは既存の id 天井方式が元から依存している前提であり、migration や実行時の設定検査は追加しない。

天井を C とすると、読み手がロックを取得した時点で、それ以前の同一キーの writer は
commit または rollback を終えている。それ以後にキーを取得する writer は id もそれ以後に
採番するため C を超える。したがって、対象 symbol かつ `id <= C` の集合が確定する。
別 symbol の未コミット行は読み手の `WHERE symbol = ...` に一致しない。

順序は「天井読み、ロック取得・解放」とする。先にロックを解放すると、その後に対象 writer が
X を採番して未コミットのまま、別 symbol の writer が Y > X を commit できる。
その状態で天井を読むと、未確定の X が天井内に入ってしまう。

reader と writer は同じチェックアウトから動く運用のため、`git pull` 後は collector を再起動し、
全 writer に新しい実装が反映されてから research を実行する。

## Consequences

- 別 symbol の取り込みと replay 開始を併走できる。同一 symbol の writer は、
  対象期間外への書き込みや天井読み後に開始したものも、キーを保持していれば待つ。
  60 秒の上限はこのロック待ちに適用され、ストリーム全体の処理時間には適用されない。
- `hashtext` の衝突は別 symbol の待機を増やし、最悪の場合は timeout になる。
  待つべき writer を見逃すことはないため、データ集合の保証は弱まらない。
  ただし複数キーの取得順を実キーの昇順に揃えることが条件であり、symbol 名順では
  衝突によって取得順が逆転し、デッドロックを招く可能性がある。
- 手作業の SQL、将来の別スクリプト、旧実装など、プロトコルに参加しない writer は待たれない。
  末尾の件数・指紋照合は、開始時の集計から終了時の集計までに可視化された変化しか検出しない。
  非参加 writer が終了時の集計後に commit すれば、取りこぼした run が成功したまま、
  同じ天井で後から集合が増えることがある。列挙を途中で止めた場合は末尾の照合自体が走らない。
  **取りこぼしが必ず `RuntimeError` になる保証はなく、全 writer の参加が必要である。**
  これは research の集合の再現性に関する制約であり、PIT の
  `known_at <= replay_clock.now()` という時刻条件の変更ではない。
- 書き込み側の回帰テストで、ロック待機中は identity sequence が進まないことと、
  混在バッチの全 symbol が対象になることを検証する。ソース走査テストで
  `INSERT INTO market_ticks` が `storage/postgres.py` の 1 か所にあることを維持する。
  スキーマ、ページング、指紋照合、他テーブルのロック方式は変更しない。
