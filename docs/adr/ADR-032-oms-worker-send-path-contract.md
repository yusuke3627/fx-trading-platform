# ADR-032: OMS worker の送信経路契約

**Status:** Accepted (2026-09-07)

## Context

ADR-030 は DB claim 後の command を in-memory queue で並べ、送信直前に expiry、
claim lease、broker position、risk を再確認する方針を定めた。一方、複数 worker が動く
場合の claim 世代、process 間の rate limit、netting delta の分類、再 claim 後の signal
失効時刻については契約が不足していた。

そのまま worker を配線すると、回収後に別 worker が取得した claim を古い worker が
上書きする、process ごとの rate limiter が broker 上限を超える、netting の縮小を新規
entry として待たせる、再起動後に失効済み signal を送る、という経路が残る。

## Decision

### 1. D1（#117）: claim を保持する state 保存は claim 世代も比較する

`PostgresCommandRepository.save_state` の引数は増やさない。書き込む command の
`claimed_by` が非 NULL なら、state に加えて `claimed_by` と `claim_expires_at` を
`IS NOT DISTINCT FROM` で比較する。比較値には SET と同じプレースホルダを使う。

recovery sweep の `CLAIMED -> READY` のように claim を解放する command は claim 列が
NULL になるため、従来どおり state だけを比較する。SQL は保持用と解放用の 2 本を定数で
持ち、Python の真偽値で選択する。

### 2. D2（#116）: 単一 dispatcher を session advisory lock で保証する

`ExecutionQueue` は `DispatchLock` を必須で受け取り、`dispatch()` の先頭で保持を確認する。
未保持なら queue を変更せず `DispatcherNotHeldError` を送出する。process ごとに独立した
rate limiter が同時に送信する構成は許可しない。

PostgreSQL 実装は 2 引数版 `pg_try_advisory_lock(classid, objid)` を使う。`classid` は
subsystem ごとに割り当てる 4 byte の ASCII tag を big-endian の整数にし、4 byte 未満は
右側を NUL で埋める。`objid` は subsystem 内で 1 から順に割り当て、用途とともにコードへ
記録する。OMS は `classid = 0x4F4D5300`（`OMS\0`）、dispatcher は `objid = 1` とする。
research など別 subsystem は別 tag を使うため、将来の advisory lock と衝突しない。

`PostgresDispatchLock` は構築時に渡された 1 接続だけを保持し、pool から接続を取り直さない。
lock 取得と `held()` はその同じ接続に束縛し、queue はその lock object を通して dispatch の
可否を判定する。`held()` は取得済みフラグと `conn.closed` だけを見て、DB へ問い合わせない。

### 3. D3（#115）: netting command は broker exposure の変化で分類する

`command_for_netting` は current net と zero-cross clamp 後の resulting net を比較する。
結果がゼロなら `CLOSE`、絶対値が減るなら `REDUCE` とし、direction は解消される current
position の符号から決める。それ以外は intent の action と direction を保つ。order の side
は delta の符号から決め、`execution_side(direction, action)` と一致させる。

### 4. D4（#114）: signal 失効時刻は command と DB が保持する

`ExecutionCommand.expires_at` を `execution_commands.expires_at` に保存し、claim 時に同じ
command へ復元する。`QueuedCommand` に期限を複製せず、queue は
`command.expires_at` を送信前に確認する。失効時刻は command 作成後に変わらないため、
`save_state` の更新対象には含めない。

## Consequences

- 古い worker の保存は、DB の state が同じ `CLAIMED` でも owner または lease expiry が
  変わっていれば `StaleCommandStateError` になる。claim を解放する sweep は state-only
  CAS のまま動く。
- session advisory lock は transaction の commit / rollback では解放されず、dispatcher が
  使う接続の session 終了まで保持される。正常な接続 close と process 終了による backend
  終了では PostgreSQL が lock を解放し、次の process が取得できる。異常切断時は server が
  切断を認識するまで解放が遅れる場合があり、その間は新 dispatcher が送信せず待つ。
- netting の縮小と全決済は close / reduce の優先度と rate limit 区分に入り、新規 entry の
  1 秒窓を消費しない。idempotency key は元の intent action を使うため変わらない。
- migration 0009 適用後は process 再起動や claim 回収を挟んでも signal expiry が失われず、
  失効済み command は broker へ送られない。
