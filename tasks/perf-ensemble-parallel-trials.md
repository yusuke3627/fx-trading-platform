# 執行アンサンブルの試行を並列実行できるようにする

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットと PR は別担当**なので行わない。

作業前に `AGENTS.md` と `.claude/rules/workflow.md` を読むこと。

## 目的

`src/trading/backtest/execution_ensemble.py` の `run_ensemble` が、seed × cost scenario の
全試行を**完全に逐次**で回している（:224 付近の `for trial in trials:` の中で
`subprocess.run` をブロッキング実行）。

研究リプレイ 1 本は VPS 実測で約 11 時間（戦略 `failed_spike_reversal`、tick 1 億 36 万本）。
3 seed × 2 scenario = 6 試行なら **6 × 11 時間 ≒ 2.8 日**かかり、その間 6 コアのうち
1 コアしか使っていない。

各試行は `python -m trading.backtest.research` の独立したサブプロセスで、出力先も
試行ごとに別ディレクトリ（`{連番}-{scenario}-seed-{seed}`）。共有するのは読み取り専用の
tick 履歴だけなので、同時実行できる。

## 実装

### CLI

`--max-parallel N` を足す。**既定は 1 で、現在の逐次実行と完全に同じ挙動にする。**
明示指定したときだけ並列になる。N は正の整数のみ受け付ける。

### 並列実行

`subprocess.run` は子プロセスの待機中に GIL を離すので、`concurrent.futures.ThreadPoolExecutor`
で十分（`ProcessPoolExecutor` は不要）。

維持すべき性質:

1. **`checkpoint()` の直列化。** `results.json` / `summary.json` を書くのは親プロセスだけにし、
   複数スレッドから同時に呼ばれないようにする（ロック、またはメインスレッドでのみ呼ぶ）。
   `_write_json` は一時ファイル経由の atomic replace なので、直列化されていれば壊れない
2. **`baseline` の決定性。** 現在は「最初に完了した試行」の `common` が baseline になるが、
   逐次実行なので実質「`trials` の先頭」である。並列にすると完了順が変わるので、
   **`trials` の順序で最初に成功した試行**を baseline にすること。そうしないと、
   どの試行が「不一致」として報告されるかが実行ごとに変わる
3. **失敗した試行の扱い。** 1 本が失敗しても残りは走り切り、`status` が試行ごとに記録され、
   最終 `summary["status"]` が `complete` にならないこと（現在の挙動）
4. **`KeyboardInterrupt`。** 未開始の試行をキャンセルし、走っている子プロセスを終了させ、
   `checkpoint()` してから再送出する。現在は中断された試行に `status="failed"` /
   `error="interrupted"` を入れている
5. `git_state() != plan["git_state"]` の検査（:242）は親で行う。現在と同じタイミングで効くこと
6. 進捗の stderr 出力（`print(f"{trial['scenario']} seed={trial['seed']}", file=sys.stderr)`）は
   並列でも行が混ざらない形にする

### データセットの同一性について（調査済み・追加実装は不要）

`src/trading/storage/postgres.py` の `stream_between` は、ストリーム開始時点の `max(id)` を
天井としてデータセットを pin する。開始時刻がずれた試行どうしが違う集合を読む可能性がある、
というのが並列化の理論上の懸念だった。

**調査の結果、既存コードがすでに loud に落ちる**ことを確認した。`_validate_trial`（:120）は
manifest から `run_id` / `created_at` / `seed` / `scenario` を除いた全項目を `common` として取り出し、
:150 で `if baseline is not None and common != baseline: raise` している。`common` には
`dataset_hash` / `feature_dataset_hash` / `swap_dataset_hash` / `tick_count` が含まれる。
つまり試行間で読んだデータが食い違えば、静かに通ることはなく必ず失敗する。

したがって**この件で新しい検査を足す必要はない**。ただし上記 2（baseline の決定性）は、
この検査の報告内容を安定させるために必要。

実運用の注意として、研究期間の終端が過去であれば live 収集の新規行は `event_time` の範囲外に
落ちるので問題は起きない。危ないのは Dukascopy の**過去区間へのバックフィルを同時に走らせた
場合**だけ。これは `--max-parallel` の help か docstring に一行書いておくこと。

### 並列度の目安（docstring / help に書く）

各試行は単一スレッドの CPU 処理。実行ホストが live のティック収集や MT5 を同居させている場合、
それらを飢えさせないよう `--max-parallel` はコア数より小さくする。

## やってはいけないこと

- 1 本のリプレイを時間で分割して並列化する（建玉・未約定注文・carry の引き継ぎが切れる）
- 試行間で DB 接続・キャッシュ・乱数状態を共有する
- 既定の挙動を変える（`--max-parallel` 未指定なら現在と同じ逐次）
- `plan` / `results.json` / `summary.json` のスキーマを壊す変更（`schema_version` がある）

## 完了条件と検証

1. `--max-parallel` 未指定時に、現在と同じ結果・同じ出力ファイルになることをテストで示す
2. 並列指定時に、全試行が走り切り、`results.json` / `summary.json` が壊れないことをテストで示す。
   既存の `tests/replay/test_execution_ensemble.py` と `tests/fixtures/execution_ensemble/` を
   参照して同じ作法で書く（実際の研究リプレイは重いので、fixture の軽量な代替コマンドを使う）
3. 試行が 1 本失敗したときの `status` と最終 `summary["status"]` が現在と同じであることを示す
4. baseline が `trials` の順序で決まることをテストで固定する
5. `ruff check .` と `pytest` を通す（venv は `.[dev,db]` で作成済み）
6. **通すために既存テストを緩めない**

## 未確定事項

- `KeyboardInterrupt` 時に走っている子プロセスをどう終わらせるか（`Popen.terminate()` へ
  切り替えるか、`subprocess.run` のままスレッド終了を待つか）は実装者の判断。
  ただし「中断後に孤児プロセスが残らない」ことは満たすこと
