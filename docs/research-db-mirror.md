# VPS の研究データを Mac に複製する

`python -m trading.storage.research_mirror` は、研究専用 PostgreSQL に
`market_ticks`、`macro_observations`、`events`、`swap_snapshots` を写す。
VPS の live 収集設定は変更しない。Mac の既存 `trading` は収集 DB なので使わない。

## 研究 DB の準備

repo のルートで、`.[dev,db]` を導入した Python を使う。Mac の DB は PostgreSQL 14 以上、
初期化を行うロールは新設 DB の所有者とする。

```bash
export RESEARCH_DB_DSN='postgresql://localhost:5432/trading_research'
(
  set -e
  createdb trading_research
  for migration in migrations/*.sql; do
    psql "$RESEARCH_DB_DSN" -v ON_ERROR_STOP=1 -f "$migration"
  done
  python -m trading.storage.research_mirror init --target-dsn-env RESEARCH_DB_DSN
)
```

上の DSN はパスワードなしのローカル接続用。パスワードが必要なら、シェル履歴に値を入力せず、
秘密管理ツール等からプロセスの環境変数へ渡す。`set -x` は使用しない。
既存の `TRADING_DB_DSN` は変更しない。

`init` は対象4表が存在することを確認し、4表をロックして空であることを確認してから、
DB コメントに `fx-trading-platform:research-mirror:v1` を付ける。
研究に関係しない表・索引や migration の適用履歴は検査しない。
必ず新設 DB に全 SQL を順番に適用する。上の手順は DB 作成や migration が失敗すると、
初期化に進まず終了する。同期時には4表すべての列名・順序・型を複製元と照合し、
不一致なら書き込み前に止まる。
同期済み DB に `init` をやり直す必要はない。

印はこのツールだけが使う運用メタデータなので、専用表や live 用 migration は追加していない。
同期は印がなければ書き込まずに止まる。同一 DB の判定にはサーバー側の advisory lock を使い、
DSN の別名、別ポート、SSH 転送による表記の違いにも対応する。同じ複製先への同期の同時実行も拒否する。

## 初回同期と差分同期

Mac の Python に `psycopg` が入り、`ssh fxvps` の公開鍵接続が設定済みであることが前提。
`RESEARCH_PYTHON` で Python のパス、`RESEARCH_SSH_HOST` で SSH のホスト別名を変更できる。
SSH は `BatchMode=yes` で実行するため、対話的な認証は行わない。

```bash
RESEARCH_PYTHON="$PWD/.venv/bin/python" \
  bash scripts/sync_research_db.sh \
  --target-dsn-env RESEARCH_DB_DSN \
  --symbols USDJPY --chunk-size 100000 --sleep-seconds 0.5 --max-rows 2000000
```

`--chunk-size` は **id 範囲の幅**。対象通貨の密度や欠番によって、1チャンクの行数はこれより少なくなる。
`--max-rows` は今回コピーする **tick の行数上限（全対象通貨の合計）** で、小さい3表は毎回全置換する。
上限に達したら同じコマンドを繰り返す。通貨を複数指定した場合は指定順にコピーする。
`--sleep-seconds` は tick のチャンク間の待ち時間で、この間に DB トランザクションは保持しない。
省略時は USDJPY、id 幅100,000、待ち0.2秒、行数上限なし。

ラッパーは空きローカルポートを選び、`ssh -N -L` のループバック転送を作る。
PowerShell の `$env:TRADING_DB_DSN` を別 SSH 子プロセスからメモリ内で取得し、
接続先だけをトンネルに差し替え、同期子プロセスの `RESEARCH_SOURCE_DSN` に渡す。
DSN はファイル・ログ・コマンドライン引数へ保存しない。
通常終了、失敗、Ctrl-C、TERM では同期子とトンネルを終了する。

DSN を別の安全な手段で環境変数に設定済みなら、直接実行できる。

```bash
python -m trading.storage.research_mirror sync \
  --source-dsn-env RESEARCH_SOURCE_DSN --target-dsn-env RESEARCH_DB_DSN \
  --symbols USDJPY --chunk-size 100000 --sleep-seconds 0.5 --max-rows 2000000
```

各実行は、表ごとの確定行数 `rows`、tick の id 天井 `ceiling`、所要秒数
`elapsed_seconds`、全表合計の行/秒 `rows_per_second`、`status` を JSON で出す。
通常の成功は終了コード0、失敗は1、Ctrl-C は130。

USDJPY 全量の保存量は、2026-09-23 の計画時点の見積もりで約23GB（本体11GB＋索引）。
DB の索引・WAL・一時領域にも余裕を持たせる。実測では同期中に live の取り込み遅延が増えたため、
**初回の全量同期は休場中（土曜早朝〜月曜早朝 JST）を推奨する。**
開始前に実際の収集停止状況を確認する。
平日に実行する場合は少量・待ち時間ありで始め、live の取り込み遅延を確認して調整する。

### 転送速度と live への影響（2026-09-23 実測）

以下は Claude が実行した検証結果。ラッパー経由で VPS の PG17 から Mac の使い捨て研究 DB
（PG14）へ、USDJPY の先頭2,000,000行と小さい3表を同期した。

| 項目 | 結果 |
|---|---|
| 指定 | `--chunk-size 100000 --sleep-seconds 0.2 --max-rows 2000000` |
| 所要時間 | 93.8秒 |
| 転送速度 | 全4表合計で21,899行/秒 |
| データ照合 | 4表とも行数と全列チェックサムが一致 |
| SSH トンネル | 終了後に残存なし |
| 初期化 | 新設 DB で成功。Mac の収集 DB `trading`（migration 0009 未適用）では拒否 |

この初期化結果は簡素化前の実装での記録。現在の `init` は migration 0009 の有無では判定せず、
対象4表の存在と空であることを検査する。データのある収集 DB には印を付けない。

VPS の tick 全体は約1億6148万行、最大 id は169,735,067で、id の欠番は少ない。
この実データでは id 幅によるチャンク分割で支障はなかった。
USDJPY 全体の約1億1200万行は、今回の速度を単純に当てはめると **約1.4時間**。
これは全量同期の実測ではなく見積もりであり、索引の増大、ディスク性能、回線や同時処理で変わる。

live 収集の取り込み遅延（`received_at - event_time`）は次のとおり。

| 指標 | 同期前 | 同期中（96秒間） |
|---|---:|---:|
| p95 | 0.21秒 | 0.22秒 |
| p99 | 0.22秒 | 1.46秒 |
| 最大 | 0.26秒 | 2.38秒 |

同期中に2秒を超えた取り込みが4件あり、同期後は元の水準に戻った。
p95 がほぼ変わらなくても一部の取り込みは遅れたため、平日の差分同期でも p99・最大値を確認する。
全量同期は影響が長時間続く可能性があるため、上記の休場中に行う。

### 再開とデータの前提

tick は通貨ごとの複製先 `max(id)` より後だけを、各チャンク1トランザクションで追加する。
中断したチャンクは取り消し、確定済みチャンクの後から再開する。件数不一致も取り消して失敗する。
テキスト COPY のため PG17 → PG14 でバイナリ表現に依存せず、id と全列を保つ。
識別列のシーケンスは採番に使わないので更新しない。この DB で収集タスクを動かさない。

複製元では、全表の `max(id)` を読んだ後、対象通貨ごとの書き込みロックを取得して即解放する。
id 採番順とコミット順が違っても、天井以下の未確定行を待ってからコピーを始める。
この保証は、すべての tick writer が `PostgresMarketTickRepository.insert_many` のロック手順を守り、
既存 tick を更新・削除せず、シーケンスを巻き戻さないことが前提。
別の複製元に切り替える場合や過去 tick が修正された場合は、新しい研究 DB を作り直す。
研究 DB への独自書き込みや手動削除はしない。

小さい3表は同一の複製元スナップショットを読み、複製先でも3表まとめて置換する。
`macro_observations.revision_of` の自己参照は単一 COPY 文の終了時に検査する。
外部キー・trigger は無効にしない。`fundamental_events` は複製せず、研究 DB に入れない。
tick 全量と小さい3表が同一時刻のスナップショットになるわけではないため、
研究の比較は同じ期間・設定を指定して manifest で確認する。同期とリプレイは同時に実行しない。

## Mac で研究を実行する

```bash
TRADING_DB_DSN="$RESEARCH_DB_DSN" caffeinate -i python -m trading.backtest.research \
  --strategy range_edge_reversal --symbol USDJPY \
  --from 2026-09-01T00:00:00 --to 2026-09-02T00:00:00 --out reports/research-mac

caffeinate -i python -m trading.backtest.execution_ensemble \
  --dsn-env RESEARCH_DB_DSN --strategy range_edge_reversal --symbol USDJPY \
  --from 2026-09-01T00:00:00 --to 2026-09-02T00:00:00 \
  --seeds 1 2 --scenarios normal --max-parallel 2 \
  --purpose '同一データで執行条件のばらつきを確認する' \
  --risk-basis 'backtest 設定を固定して比較する' --out reports/ensemble-mac
```

期間は例。複製済み範囲とウォームアップ期間を含む範囲を選ぶ。
期間指定は既存 CLI と同じ broker-clock のラベルであり、JST ではない。
最初のコマンドの `TRADING_DB_DSN` 上書きはその子プロセスだけに効く。

## VPS の結果と照合する

同じコード、設定、通貨、期間、ウォームアップ、seed、scenario で実行する。
両者の `manifest.json` にある次の値を比較する。

| キー | 比較対象 |
|---|---|
| `dataset_hash` | tick データ |
| `feature_dataset_hash` | macro / events の特徴量入力 |
| `swap_dataset_hash` | swap 入力 |
| `tick_count` | リプレイが読んだ tick 数 |

```bash
python - VPS_MANIFEST.json MAC_MANIFEST.json <<'PY'
import json
import sys
from pathlib import Path

keys = ('dataset_hash', 'feature_dataset_hash', 'swap_dataset_hash', 'tick_count')
left, right = [json.loads(Path(path).read_text()) for path in sys.argv[1:]]
for key in keys:
    print(key, '一致' if left[key] == right[key] else '不一致')
assert all(left[key] == right[key] for key in keys)
PY
```

不一致なら、まず同期上限で未転送の tick がないか、同じ期間・ウォームアップか、
小さい3表が更新されていないかを確認する。入力ハッシュの一致と売買結果の一致は分けて確認する。

### 行数とチェックサムの SQL

2026-09-23 の検証では、両側でタイムゾーンと日付表示を揃え、行ごとの MD5 の先頭60ビットを
数値に変換して合算した。行数とこの合計を4表ごとに比較した。
これは複製内容の照合用であり、研究リプレイの manifest による比較とは別に行う。
大きい複製元への照会は負荷になるため、初回の確認は先頭の数百万行に限定する。

まず、同期を終えた研究 DB で実際にコピーできた USDJPY の最大 id を取得する。

```sql
SELECT count(*) AS tick_count, max(id) AS copied_tick_max_id
FROM public.market_ticks
WHERE symbol = 'USDJPY';
```

新設 DB への `--max-rows 2000000` の同期なら、ここで2,000,000行を確認する。
続く SQL を複製元と研究 DB の両方の `psql` で実行し、同じ `copied_tick_max_id` を入力する。
JSON 出力の `ceiling` は複製元全体の天井なので、行数上限を指定した回のコピー済み範囲には使わない。

```sql
\prompt '研究 DB の copied_tick_max_id: ' tick_max_id
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TimeZone = 'UTC';
SET LOCAL DateStyle = 'ISO';

SELECT 'market_ticks' AS table_name, count(*) AS row_count,
       coalesce(sum(('x' || substr(md5(concat_ws('|',
           id, symbol, bid::text, ask::text,
           extract(epoch FROM event_time)::text,
           extract(epoch FROM received_at)::text,
           source, ingestion_run::text
       )), 1, 15))::bit(60)::bigint), 0) AS checksum
FROM public.market_ticks
WHERE symbol = 'USDJPY' AND id <= :'tick_max_id'::bigint;

SELECT 'macro_observations' AS table_name, count(*) AS row_count,
       coalesce(sum(('x' || substr(md5(t::text), 1, 15))::bit(60)::bigint), 0) AS checksum
FROM public.macro_observations AS t
UNION ALL
SELECT 'events', count(*),
       coalesce(sum(('x' || substr(md5(t::text), 1, 15))::bit(60)::bigint), 0)
FROM public.events AS t
UNION ALL
SELECT 'swap_snapshots', count(*),
       coalesce(sum(('x' || substr(md5(t::text), 1, 15))::bit(60)::bigint), 0)
FROM public.swap_snapshots AS t;

COMMIT;
```

小さい3表は同期後に複製元が更新されると不一致になる。同じ内容が保たれている間に比較し、
差があれば更新の有無を調べてから再同期・再照合する。
ティックの時刻は PG の版や表示形式による文字列表現の差を避けるため epoch に揃えている。

## 開発時の検証

通常のテストは収集 DB を継承しない。

```bash
ruff check .
env -u TRADING_DB_DSN pytest -q
bash -n scripts/sync_research_db.sh
```

integration は全 migration 適用済みの使い捨て DB を明示する。
この同期用テストは同じサーバー上にさらに2つの使い捨て DB を作るため、実行ロールに
`CREATE DATABASE` 権限が必要。テスト終了時にその2つだけを削除する。
既存 integration には接続先の表を削除するものもあるので、収集 DB を指定しない。

```bash
TRADING_DB_DSN=postgresql://localhost:5432/fx_win_push_143919 pytest tests/integration -q
```

PG17 → PG14 の実転送と VPS の負荷確認は、VPS に接続できる環境から Claude が実行する。
