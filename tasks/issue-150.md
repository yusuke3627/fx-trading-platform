# issue #150: research の tail/head guard が DB セッションの timezone に依存して月曜終端を誤拒否する

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-150-research-guard-timezone`
- ブランチ: `fix/issue-150-research-guard-timezone`（base は `origin/main` = `d70fd67`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #150 本文（全文転記）

> ### 現象
>
> VPS（DB セッションの timezone が Asia/Tokyo）で、research の期間終端を月曜 00:00（broker ラベル）にすると、データが揃っているのに tail guard が拒否する:
>
> ```
> python -m trading.backtest.research --env backtest --symbol USDJPY --strategy failed_spike_reversal --from 2026-08-10T00:00:00+00:00 --to 2026-08-24T00:00:00+00:00 ...
> stored history ends at 2026-08-22 08:59:58.108000+09:00, market-open time before the requested period end 2026-08-24 00:00:00+00:00; the report would claim the full period — backfill the tail or move --to earlier
> ```
>
> `2026-08-22 08:59:58+09:00` は broker ラベル金曜 23:59:58（週の終値）で、tick 自体は欠けていない。
>
> ### 原因
>
> `src/trading/backtest/research.py` の `open_market_seconds` が `cursor.weekday()` と `replace(hour=0, ...)` を、渡された datetime の tzinfo のまま計算している。psycopg が返す `Tick.time` は接続の TimeZone（VPS では JST）で描画された aware datetime なので、ラベル軸（UTC 刻印）では金曜 23:59:58 → 月曜 00:00 の 2 秒が、JST 軸では土曜 08:59:58 → 月曜 09:00 と読まれ、月曜 0〜9 時の 9 時間が「市場が開いていた欠損」と数えられる。
>
> `--to` が土曜 00:00 のとき（H5 の 2 年 run など）は JST 軸でも土曜のままなので発火しない。`indicators/session.py` など他の壁時計計算は `astimezone` で明示的に正規化しており影響しない。
>
> ### 対応
>
> `open_market_seconds` の冒頭で `start` / `end` を `astimezone(UTC)` に正規化してからラベル軸の weekday / midnight を計算する。テストは JST tzinfo を付けた datetime で「金曜 23:59:58 → 月曜 00:00 が 2 秒」になることを固定する。

---

## ユーザー判断で確定していること（変更しない）

- 修正は `open_market_seconds` の入口での UTC 正規化のみ。guard の閾値（`EDGE_GAP_TOLERANCE_SECONDS`）・guard の構造・CLI 引数は変えない。
- `indicators/session.py` など他の壁時計計算には触れない。
- 周辺のリファクタ・無関係な整形をしない。

---

## 変更範囲

### 1. `src/trading/backtest/research.py`（`open_market_seconds`、126 行〜）

現状:

```python
def open_market_seconds(start: datetime, end: datetime) -> float:
    total = 0.0
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = min(end, next_midnight)
        if cursor.weekday() < 5:
            total += (day_end - cursor).total_seconds()
        cursor = day_end
    return total
```

- 冒頭で `start` / `end` を `astimezone(UTC)` に正規化し、以降の `weekday()` / `replace(hour=0, ...)` はその値で計算する（`UTC` は `datetime` モジュールから import。同ファイルの既存 import を確認して合わせる）。
- 戻り値・引数・閾値は変えない。
- docstring に、ラベル軸が UTC 刻印（ADR-005）であること、psycopg が返す `Tick.time` はセッションの TimeZone で描画された aware datetime なので UTC へ戻してからラベル軸の曜日・日付境界を取る、という理由を 1〜2 文で足す。WHAT の説明や「issue #150 のため」のような文脈依存コメントは書かない。
- `_ensure_head_covered` / `_ensure_tail_covered`（148〜164 行）はどちらも `open_market_seconds` を呼ぶだけなので、この修正で head / tail 両方が直る。別経路の weekday / midnight 計算は無い（`rg -n "weekday\(\)|replace\(hour=0" src/trading/backtest/research.py` で確認済み。この 1 か所だけ）。

### 2. テスト: `tests/unit/test_research_runner.py`

`open_market_seconds` の直接テストは現状無い（guard は `test_period_coverage_rejects_the_shapes_that_would_report_plausibly`（220 行〜）で `ensure_period_covered` 経由で検証されている）。同じファイルに次を追加する。既存の `tick()` ヘルパー（36 行〜）と `START` / `END` 定数を使う。

(a) `open_market_seconds` を JST tzinfo（`zoneinfo.ZoneInfo("Asia/Tokyo")`）付きの datetime で呼び、ラベル軸の金曜 23:59:58 → 月曜 00:00 が 2 秒になる。

- ラベル軸の金曜 2026-08-21 23:59:58+00:00 は JST 表記では `datetime(2026, 8, 22, 8, 59, 58, tzinfo=ZoneInfo("Asia/Tokyo"))`（土曜朝）。
- ラベル軸の月曜 2026-08-24 00:00+00:00 は JST 表記では `datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))`。
- 期待値は `2.0`。修正前は月曜 0〜9 時の 9 時間（32400 秒）が加算されるので、このテストは修正前に落ちる。

(b) 同じ 2 つの瞬間を UTC 表記（`tzinfo=UTC`）で渡しても `2.0`。JST 表記の結果と等しいことも assert する。

(c) issue の実経路の再現: `ensure_period_covered` に、`last` tick の `time` を JST 表記（金曜 23:59:58 ラベル = `2026-08-22 08:59:58+09:00`）、`end` を UTC 表記の月曜 `2026-08-24 00:00+00:00` で渡しても SystemExit にならない（psycopg の `Tick.time` は JST、CLI の `--to` は `broker_label` で +00:00、という混在が実際の入力）。

(d) 既存のテスト（UTC 入力）は無変更で通る。

テストデータに実在の人物・団体名を使わない（既存のヘルパーは架空値のみ）。

### 触らないもの

- `EDGE_GAP_TOLERANCE_SECONDS` の値、`_ensure_head_covered` / `_ensure_tail_covered` / `ensure_period_covered` の構造とメッセージ
- CLI 引数（`broker_label` の +00:00 限定を含む）
- `src/trading/indicators/session.py` ほか他モジュールの壁時計計算
- `tests/unit/test_invariants.py`
- `config/`、`migrations/`、`docs/`

---

## 実装上の規約

- 検証はシステム境界のみ。`open_market_seconds` の内部に naive datetime 対策などの防御的分岐を足さない（`Tick.time` と `broker_label` の戻りはどちらも aware）。
- WHAT を説明するコメント、コミット文脈に依存するコメント（「issue #150 のために追加」等）を書かない。
- ruff の line-length は 100。

## 完了条件

worktree 内で次がすべて通ること。

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` 無指摘
- 上記 3 ディレクトリの pytest が green（`tests/broker` は MT5 なしで自動 skip、`tests/integration` は PostgreSQL 必須のためここでは対象外）
- 追加したテスト (a) が、修正を戻すと落ちることを一度確認する（`git stash` は使わない。`git diff` で修正箇所を確認し、手で戻して pytest → 再適用でよい）

## 影響範囲の確認

```
rg -n "open_market_seconds" src tests
rg -n "weekday\(\)|replace\(hour=0" src/trading/backtest
```

## コミットしないもの

- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md` / `tmp/`（`tmp/` は gitignore されていないので `git add` の対象に含めない）

このファイル（`tasks/issue-150.md`）はコミットに含める。
