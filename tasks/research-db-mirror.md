# 研究用 DB を VPS から Mac へ複製する同期ツール

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットと PR は別担当**なので行わない。

作業前に `AGENTS.md`、`.claude/rules/workflow.md`、`tests/integration/README.md` を読むこと。

## 目的

研究リプレイを live の VPS から外し、Mac で回せるようにする。そのために、VPS の PostgreSQL から
研究に必要な表を Mac の研究専用 DB へ写す同期ツールを作る。

### なぜ移すか（2026-09-23 の VPS 実測）

- VPS で研究リプレイを並列実行すると、live のティック収集の取り込み遅延（`received_at − event_time`）が
  悪化した。4 並列で p99 0.73 秒 → 11.5〜15.8 秒、最大 19〜22 秒。実験の前後は元の水準
- 研究の子を Idle 優先度にしても改善しなかった。研究の読み出しを処理する PostgreSQL 側の競合が疑われる（未特定）
- Mac（M3）は 1 コアあたり VPS の約 2.8 倍速い

## 複製元と複製先

| | 複製元（VPS） | 複製先（Mac） |
|---|---|---|
| PostgreSQL | 17.11（Windows） | 14.18（Homebrew） |
| DB | `trading`（live の収集と共用） | **研究専用の DB を新設**（例: `trading_research`、全 migration を適用） |
| 接続 | `ssh fxvps`（Tailscale）の `ssh -L` で VPS の `localhost:5432` へ | ローカル |
| DSN | VPS の machine 環境変数 `TRADING_DB_DSN`（`postgresql://trading:<pw>@localhost:5432/trading`） | 利用者が指定 |

- VPS の SSH の既定シェルは **PowerShell**。`ssh fxvps '$env:TRADING_DB_DSN'` で DSN が取れる（`%VAR%` は展開されない）
- VPS の Postgres は `listen_addresses='*'` だが、Windows ファイアウォールでは開けていない。**開けない**こと。SSH のポート転送だけを使う
- Mac の既存 `trading` DB は **Mac の収集用**で、シェル環境の `TRADING_DB_DSN` はそこを指す。同期先をここに向けると、
  macro_observations / events を VPS の内容で全置換して Mac の収集データを壊す。これを構造的に防ぐこと（下記「安全装置」）

### 対象の表（列構成は VPS と最新 migration で完全一致を確認済み、4 表・52 列）

| 表 | VPS の規模 | 写し方 |
|---|---|---|
| `market_ticks` | 1 億 6148 万行・34GB（本体 16GB、主キー索引 3.5GB、`(symbol, event_time, bid, ask)` の一意索引 14GB）。通貨比率 USDJPY 69% / EURUSD 29% / GBPJPY 1% / GBPUSD 0.4% | 対象通貨を指定して id による差分同期 |
| `macro_observations` | 5.3 万行 | 毎回全置換 |
| `events` | 889 行 | 毎回全置換 |
| `swap_snapshots` | 32 行 | 毎回全置換 |

研究リプレイ（`src/trading/backtest/research.py`）と各イベントスタディが読むのはこの 4 表だけ（`rg "Postgres[A-Za-z]*Repository\(" src/trading/backtest` で確認済み）。
`market_bars` は研究リプレイが tick から組み直すので要らない。

制約の注意:
- `market_ticks.id` は `GENERATED ALWAYS AS IDENTITY`。PostgreSQL の `COPY FROM` は識別列にも入力値をそのまま書くので、元の id を保てる
- `macro_observations.revision_of` は自表への外部キー。全置換の順序に注意する
- `fundamental_events.event_id` が `events` を参照するが、研究 DB には `fundamental_events` を入れない

## 作るもの

### 1. 同期ツール（Python、repo 内）

置き場所・CLI 名は実装者の判断（例: `python -m trading.storage.research_mirror`）。`src/trading/storage/postgres.py` の
定数と手順を再利用するので、その近くが自然。

**接続**：複製元・複製先の DSN を**環境変数名**で受け取る（値をコマンドライン引数に取らない。プロセス一覧に出るため）。

**market_ticks（差分同期）**
- 対象通貨をリストで指定する（既定は USDJPY だけ）
- 複製元の読み出しは `stream_between` と同じ固定手順を踏む：`max(id)` を天井として読み、対象通貨ごとに
  `pg_advisory_xact_lock(_TICK_ADVISORY_LOCK_CLASS_ID, hashtext(symbol))` を取得して即解放し、書き込み途中の行を確定させる。
  そのうえで「対象通貨かつ id ≤ 天井」の確定済み集合だけを写す。`stream_between` のコメントにある理由（id の割り当てと
  コミット順が一致しない）をそのまま引き継ぐ
- 再開：複製先の対象通貨の `max(id)` より大きい id から写す。中断しても続きから再開できる
- id の範囲で小分けにし（チャンクの大きさは指定可）、**各チャンクを複製先で 1 トランザクション**で写す。
  チャンクごとに、複製元で読んだ行数と複製先に入った行数を突き合わせ、食い違えば例外にする
- **複製元は live の収集と同じ DB なので、負荷を抑える手段を持たせる**：チャンク間の待ち時間、1 回の実行で写す行数の上限
  （初回の全量を何回かに分けて流せるように）
- COPY は**テキスト形式**（PG17 → PG14 のため）。psycopg の copy API で複製元の `COPY ... TO STDOUT` を
  複製先の `COPY ... FROM STDIN` へ流す。中間ファイルを作らない

**小さい 3 表（全置換）**
- 複製先で 1 トランザクションの中で削除して写し直す。自己参照の外部キーに注意する

**安全装置（必須）**
- 複製先が研究専用 DB であることを、**初期化コマンドで付ける明示的な印**（DB コメント、専用の目印表など。方式は実装者の判断）で確認し、
  印がなければ何も書かずに止まる
- 初期化コマンドは、全 migration 適用済みで対象 4 表が空の DB にだけ印を付ける。既にデータのある DB（Mac の収集用 `trading` など）には付けない
- 複製元と複製先が同じ DB を指していたら止まる

**出力**：実行ごとに、写した行数（表ごと）、天井、所要時間、行/秒を出す。DSN やパスワードは出さない。

### 2. SSH トンネルと DSN 取得のラッパー（`scripts/` 配下のシェル）

- `ssh -N -L <ローカルの空きポート>:localhost:5432 fxvps` でトンネルを張る（ホスト名は引数か環境変数で変えられるように）
- `ssh fxvps '$env:TRADING_DB_DSN'` で DSN を取り、host:port をトンネル側へ書き換え、**同期ツールの子プロセスの環境変数にだけ**渡す
- **パスワードを Mac のディスク・ログ・コマンドライン引数・シェル履歴に残さない**
- 終了時（異常終了・Ctrl-C を含む）にトンネルを必ず閉じる
- 同期ツールへの引数（対象通貨、チャンク、待ち時間、上限など）はそのまま渡す

### 3. 手順書（`docs/` 配下）

- 研究 DB の作成（createdb、全 migration の適用、初期化コマンドで印を付ける）
- 初回同期：USDJPY だけで約 23GB（本体 11GB＋索引）。所要時間と live への影響の目安（Claude の実測値を後で追記するので、節だけ用意してよい）。
  推奨タイミング（相場の休場中＝土曜早朝〜月曜早朝 JST は収集が止まっていて live への影響がない）
- 差分同期のやり方
- Mac で研究リプレイを回す方法：`TRADING_DB_DSN` を研究 DB に向けて `python -m trading.backtest.research ...`、
  執行アンサンブルは `--dsn-env` で研究 DB の DSN を持つ環境変数名を渡す、長時間は `caffeinate -i`
- 研究 DB と VPS の結果の一致確認のしかた（manifest の `dataset_hash` / `feature_dataset_hash` / `swap_dataset_hash` / `tick_count` を比べる）

### 4. テスト

- **integration**：`TRADING_DB_DSN` の Postgres サーバー上に、使い捨ての複製元・複製先 DB をテスト内で作って全 migration を当て、
  終わったら消す（CI は postgres:17 に superuser で接続しているので `CREATE DATABASE` できる。`.github/workflows/ci.yml` を参照）。
  `TRADING_DB_DSN` が無ければ既存の integration と同じく skip する
  - 差分同期（天井までの確定済み集合だけ写る、対象外の通貨は写らない、元の id が保たれる）
  - 再開（途中で止めて再実行すると続きから写り、重複しない）
  - 固定手順（天井を読んだ後に始まった書き込みは今回の対象に入らない、など `stream_between` のテストに倣う）
  - 全置換（3 表の内容が複製元と一致、自己参照の外部キーがあっても通る）
  - 安全装置（印のない DB には何も書かない、データのある DB には印を付けない、同一 DB は止まる）
  - チャンクと上限・待ち時間の指定が効くこと
- 単体で済むものは unit
- ラッパーのシェルは、少なくとも `bash -n` と、トンネルの後始末（trap）が効くことを確認する

## やってはいけないこと

- VPS の live 収集タスク・Postgres の設定・Windows ファイアウォールを変える
- 同期先を Mac の収集用 DB（`trading`）に向ける、そこへ書き込むテストを書く
- 研究リプレイ本体（`research.py` / engine）の挙動を変える
- 既存の migration を書き換える（研究 DB 用の表が要るなら新しい連番で足すか、ツールの初期化で作る。どちらが適切か判断し理由を返す）
- パスワードや DSN をログ・出力・ファイルに残す

## 完了条件と検証

1. `ruff check .` と `pytest` が通る。integration は全 migration 適用済みの使い捨て DB を `TRADING_DB_DSN` に指定して回す。
   **環境の `TRADING_DB_DSN` は Mac の収集 DB なので、そのまま integration を回さないこと**
2. 追加したテストが、実装を壊したときに落ちること（固定手順を外す、安全装置を外す、件数照合を外す、など）を確認する
3. 実データでの確認は Claude が行う（Codex からは VPS に届かない）：使い捨ての DB へ VPS の先頭数百万行を写し、
   複製元と複製先で行数とチェックサムが一致すること、live 収集の取り込み遅延への影響、転送速度
4. 変更ファイル、実行した検証と結果、未確認項目を返す

## 未確定事項

- ツールの置き場所と CLI 名
- 安全装置の印の方式
- 研究 DB 用の補助的な表が要る場合、migration に足すかツールの初期化で作るか
