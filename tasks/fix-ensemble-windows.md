# 執行アンサンブルを Windows の実行ホストで安全に使えるようにする

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットと PR は別担当**なので行わない。

作業前に `AGENTS.md` と `.claude/rules/workflow.md` を読むこと。

## 背景

`src/trading/backtest/execution_ensemble.py`（#220 / #226）は Linux の CI でしか試されておらず、
研究リプレイの実行ホストである Windows VPS では 2026-09-23 に初めて実行された。
VPS（Windows Server 2025、Python 3.11.9、日本語ロケール、Defender のリアルタイム保護が有効）での
実機検証で、次の事実を確認した。

| # | 事実 | 影響 |
|---|---|---|
| 1 | Ctrl-C（1 回・0.3 秒間隔の 2 回）は正常。0.8 秒で終了、孤児 0、状態も正しく保存 | 問題なし |
| 2 | **Stop-ScheduledTask で止めると、実行中の研究リプレイの子が孤児として走り続ける**。results.json は running のまま | 長時間ジョブはタスクスケジューラで流す運用なので、実際に踏む停止経路 |
| 3 | `_write_json` の `temporary.replace(path)` は、差し替え先を他プロセスが開いていると `PermissionError [WinError 5]` で失敗する（テストで再現）。Defender は新しく書いたファイルを走査で開く | `checkpoint()` の多くは try の外なので、起きるとアンサンブル全体が落ち、並列時は finally で実行中の子も止まる |
| 4 | 最後の `print(json.dumps(..., ensure_ascii=False))` は、stdout をファイルへ逃がすと cp932 で書かれる（実機で確認） | cp932 外の文字（絵文字など）を `--purpose` に書くと終了時に `UnicodeEncodeError` |
| 5 | テストが Windows で走らない（下記） | 本番 OS で自動検証する手段がない |
| 6 | 並列実行中、live のティック収集の取り込み遅延が悪化した（下記） | live 側のデータ品質に影響 |
| 7 | #226 で help と `docs/research/execution-ensemble.md` に書いた「4 並列で約 2 倍」は誤り | 実測と矛盾する記述が残っている |

### 5 の内訳（Windows で `tests/replay/test_execution_ensemble.py` と `tests/unit/test_execution_ensemble.py`）

- 通常の Windows 環境：54 failed / 17 passed / 6 skipped
- うち 46 件は `read_text()` / `write_text()` のエンコーディング未指定（既定の cp932 で UTF-8 の `plan.json` 等を読む）。
  テスト本体と `tests/fixtures/execution_ensemble/trial.py` の両方にある
- `PYTHONUTF8=1` でも残る 8 件：
  - 事実 3 の `PermissionError`（フィクスチャの子が `results.json` を読んでいる最中に親が replace）
  - 別プロセス間で `time.monotonic_ns()` を比べて重なりを数えるテスト。Windows の `monotonic` は
    `GetTickCount64()` で**分解能 15.625ms**（実機で `time.get_clock_info` を確認）
  - `test_research_cli_repeats_real_pipeline_without_database_or_broker` の `subprocess.run(text=True)` が、
    子の cp932 出力を読めず `stdout` が `None` になる（事実 4 と同根）
- 中断テストは `skipif(sys.platform == "win32")`

### 6 の内訳（2026 年 7 月・`range_edge_reversal`・1 本 416 万 tick、main `5405716`）

| | 1 本あたり | 全体 | 単独比の実効向上 |
|---|---:|---:|---:|
| 単独 | 554 秒 | 559 秒 | 1.0 倍 |
| 2 並列 | 639 秒 | 647 秒 | 1.71 倍 |
| 4 並列 | 641 秒 | 646 秒 | 3.43 倍 |

live 収集の取り込み遅延（`received_at − event_time`、broker は UTC+3）:

| | p95 | p99 | 最大 |
|---|---:|---:|---:|
| 実験前 | 0.22 秒 | 0.73 秒 | 2.1 秒 |
| 4 並列中 | 2.4 秒 | 11.5 秒 | 19.2 秒 |
| 実験後 | 0.21 秒 | 0.23 秒 | 2.2 秒 |

研究が読まない GBPJPY を含む 4 通貨すべてで悪化したので、原因は DB ではなく CPU の取り合い。
live の収集タスクも研究リプレイも、タスクスケジューラ既定の **BelowNormal** という同じ優先度クラスで動いていた。

### Windows のプロセス構成（実機で確認）

venv の `.venv\Scripts\python.exe` は**中継役**で、本体の `Python311\python.exe` を子として起動する。
研究リプレイ 1 本は 2 プロセスに見える。中継役を `TerminateProcess` すると本体も一緒に終了する
（中継役が自分のジョブで本体を束ねている）。ただし中継役のジョブは、本体がさらに起こした子
（＝アンサンブルから見た研究リプレイ）までは巻き込まない。事実 2 の孤児はこれが原因。

## 実装すること

### 1. 研究リプレイの子をジョブオブジェクトに入れる（Windows のみ）

- アンサンブル 1 回につきジョブを 1 つ作り、`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` を設定し、
  **ハンドルをプロセスの終了まで保持**する。アンサンブルがどんな形で終わっても（強制終了を含む）
  OS がハンドルを閉じ、ジョブ内の子がすべて終了する
- 子を起こしたら直後に `AssignProcessToJobObject` で入れる。pywin32 は無いので `ctypes` で実装する。
  OS API の戻り値は検査し、失敗したら例外にする（黙って孤児を生む状態で進めない）
- 中継役が本体 python を起こすまでの間に割り当てが間に合わない競合はあり得るが、
  中継役が終われば本体も終わることを実機で確認済みなので、**中継役を確実にジョブへ入れれば足りる**
- **逐次・並列の両経路**に適用する。逐次経路は現在 `subprocess.run` なので、起動直後に割り当てるには
  `Popen` と `wait` に分ける必要がある。そのとき `subprocess.run` が例外時（`KeyboardInterrupt` を含む）に
  子を kill して待つ挙動を落とさないこと
- 子の起動処理は両経路で共通の小さな関数にまとめてよい
- POSIX では何もしない（現在の挙動のまま）

### 2. 研究リプレイの子を Idle 優先度で起動する（Windows のみ）

- `subprocess.Popen(..., creationflags=subprocess.IDLE_PRIORITY_CLASS)`。live 収集（BelowNormal）より
  低くして CPU を譲らせる。live 側の設定には触れない
- 効果（研究の所要時間と live 収集の遅延）は Claude が VPS で 4 並列を再実行して確かめる

### 3. `_write_json` の差し替えを `PermissionError` のときだけ再試行する

- 短い間隔で数回（合計で数秒以内）。再試行しても失敗したら元の例外を伝える。握りつぶさない
- `PermissionError` 以外の `OSError` は再試行しない

### 4. 最後の stdout 出力を UTF-8 に固定する

- `main()` の最後の `print(json.dumps(..., ensure_ascii=False, ...))`。ファイルやパイプへ逃がしても UTF-8 で
  書かれること、コンソールへの表示が壊れないこと
- 研究リプレイ本体（`research.py`）の出力には触れない

### 5. テストを Windows でも通るようにする

- `tests/replay/test_execution_ensemble.py` と `tests/fixtures/execution_ensemble/trial.py` の
  テキスト I/O に `encoding="utf-8"`、`subprocess.run(text=True)` にも `encoding="utf-8"`
- 別プロセス間の時刻比較は、Windows でも分解能のある時計（`time.perf_counter_ns()` など）へ
- フィクスチャの子が `results.json` を読むことで事実 3 が起きるが、これは実装の再試行（3）で吸収される
  べきもの。テストを事実 3 から逃がす方向で直さないこと
- **ジョブオブジェクトの挙動を検証するテストを足す**：アンサンブル（またはそれ相当の親）を強制終了したら
  子も終わること。Windows のみで走らせ、Linux の CI では skip でよい
- 既存の POSIX 用中断テストは緩めない

### 6. docs と help を実測値に直す

- `--max-parallel` の help と `docs/research/execution-ensemble.md` から「H6 の記録と単独換算から約 2 倍」を削除する
- 代わりに、上の 6 の実測（4 並列で 3.43 倍、1 本あたり +16%、2026 年 7 月・`range_edge_reversal`・1 本 416 万 tick）と、
  並列実行中に live 収集の取り込み遅延が悪化した事実、その対策として子を Idle 優先度で起動していることを書く
- 運用上の注意：`Stop-ScheduledTask` などの強制終了では後始末が走らないので `results.json` は running のまま残るが、
  子はジョブオブジェクトで終了する。止めるなら Ctrl-C が望ましい
- 波数の算術（`ceil(試行数 / N)`）の記述は正しいので残す

## やってはいけないこと

- live の収集タスクや VPS の設定を変える
- 既存テストを緩める（特に POSIX の中断テスト、`tests/unit/test_invariants.py`）
- 研究リプレイ本体（`research.py` / engine）の挙動を変える
- `--max-parallel` 未指定時（逐次）の成果物（`results.json` / `summary.json` / 各試行のファイル）を変える。
  stdout の文字コード、子の優先度、ジョブ所属は変わってよい

## 完了条件と検証

1. Linux（この Mac）で `ruff check .` と `pytest` が通る。integration を含めるなら、全 migration を適用した
   使い捨て DB を `TRADING_DB_DSN` に指定する。環境の `TRADING_DB_DSN` は Mac の収集 DB なので向けない
   （unit / replay だけなら `TRADING_DB_DSN` を外して実行してよい）
2. Windows での検証は **Claude が VPS の研究用 worktree で行う**（Codex からは VPS に届かない）。
   Codex は Windows 専用の分岐とテストが、Linux では正しく skip されること・import で落ちないことまで確認する
3. 追加・変更したテストが、実装を壊したときに落ちること（ジョブ割り当てを外す、再試行を外す、など）を確認する
4. 変更ファイル、実行した検証と結果、未確認項目を返す

## 未確定事項

- ジョブオブジェクトの ctypes 実装の置き場所（`execution_ensemble.py` 内か、小さな別モジュールか）は実装者の判断。
  他で使う予定はないので、分けるなら理由があるときだけ
- Windows 専用テストの形（実プロセスを起こして親を強制終了する、など）は実装者の判断
