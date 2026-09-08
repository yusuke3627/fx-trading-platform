# issue #36: realized_pnl_day を balance 差分ではなく deal から出す

## 要件（issue #36 の全文要点）

`account_snapshots.realized_pnl_day` は現在、JST 日の最初のスナップショットからの
**balance の移動**として記録している。

**何が問題か**: balance を動かすのは取引だけではない。入金・出金・クレジット付与も
同じ列を動かすので、資金移動があった日の `realized_pnl_day` は取引の結果ではなくなる。
10 万円を出金した日は `-100000` が記録され、取引で失ったように読める。
`build_snapshot` は `account_info()` しか見ないため、balance の変化が約定によるものか
資金移動によるものかを区別する手段を持たない。

**現状の影響範囲**: Risk の判定には影響しない。`src/trading/risk/limits.py` の 3 つの
損失指標（JST 日次 `daily_loss_pct` / ローリング 24h `rolling_24h_loss_pct` /
高値からのドローダウン `hwm_drawdown_pct`）はすべて equity から計算されており、この列を
読まない。影響を受けるのは、この列を人が読むときと、将来これを日次レポートの入力に
したときである。

**対応方針（issue 本文）**: 正確な日次実現損益は deal から出すのが筋で、
`profit + commission + swap` を当日の売買 deal について合計する。
資金移動の deal（`DEAL_TYPE_BALANCE` 等）は型で除外できる。

## 実装方針

### 1. 純関数として集計ロジックを切り出す（新規ファイル）

`src/trading/data/account/realized_pnl.py` を新規作成し、次の純関数を置く。

```python
def realized_pnl_between(
    raw_deals: Iterable[Any], *, start: datetime, end: datetime
) -> Decimal
```

- `raw_deals` は MT5 の `history_deals_get()` が返す生オブジェクト列
  （属性アクセス: `type` / `time` / `profit` / `commission` / `swap`）
- `start` / `end` は **broker ラベル軸**の datetime（下記 3 を参照）。両端含む
  `start <= t <= end` で絞る
- 売買 deal だけを対象にする。判定は既存の
  `trading.execution.mt5.mapper.is_trade_deal(raw)` を再利用する
  （`DEAL_TYPE_BUY=0` / `DEAL_TYPE_SELL=1` のみ真。`DEAL_TYPE_BALANCE` 等の
  資金移動・クレジット・手数料 deal はここで落ちる）。定数を再定義しない
- 各 deal の時刻は既存の
  `trading.execution.mt5.mapper.broker_time_from_epoch(raw.time)` で datetime に変換する
- 合計は `profit + commission + swap`。**すべて `Decimal(str(value))` で受ける**
  （MT5 は float を返す。金額に float を使わない）。フィールドが欠けている前提の
  防御的な `getattr` フォールバックは書かない — MT5 の deal は常にこの 3 つを持つ
- 空列なら `Decimal(0)` を返す
- 同じモジュールに `RES_S_OK = 1`（MT5 の公開定数）を置く。collector の 3-1 が使う

MT5 に依存する呼び出しは Windows でしか実行できないため、集計はこの純関数に閉じ込め、
`tests/unit` で検証できるようにするのが本 issue の眼目。

### 2. `build_snapshot` から日次実現損益の計算を外す

`src/trading/data/account/collector.py`:

- `build_snapshot` の引数 `day_baseline: AccountSnapshot | None` を
  `realized_pnl_day: Decimal` に置き換える。`realized_pnl_day=balance - day_open_balance`
  の行と、その上の「balance の移動」を説明するコメントを削除し、渡された値をそのまま入れる
- `day_baseline` は `realized_pnl_day` の算出にしか使われていないので、引数ごと消える
- `previous`（high water mark の持ち越し）はそのまま残す

### 3. collector が broker ラベル軸へ変換して deal を引く

`AccountSnapshotCollector`:

- `__init__` にキーワード専用の必須引数 `server_ahead_of_ny_hours: float` を足す
  （既定値を持たせない。config と食い違う既定が黙って使われるのを避ける）。
  `self._server_ahead_of_ny = timedelta(hours=server_ahead_of_ny_hours)` として保持する
- `collect_once()`:
  - `today = self._repository.known_before(...)` の呼び出しを削除する
    （`day_baseline` が不要になったため。`known_before` 自体は
    `src/trading/live/shadow.py` が使っているので protocol からは消さない）
  - 新しい private メソッド `_realized_pnl_day(day_start, now)` を呼び、その戻り値を
    `build_snapshot(..., realized_pnl_day=...)` に渡す
- `_realized_pnl_day(self, day_start: datetime, now: datetime) -> Decimal`:
  ```python
  start = known_to_broker_label(day_start, self._server_ahead_of_ny)
  end = known_to_broker_label(now, self._server_ahead_of_ny)
  raw = self._mt5.history_deals_get(
      start - BROKER_TIME_MARGIN, end + BROKER_TIME_MARGIN
  )
  if raw is None:
      code, description = self._mt5.last_error()
      if code != RES_S_OK:
          raise MT5ConnectionError(
              f"history_deals_get failed: ({code}, {description})"
          )
      raw = ()
  return realized_pnl_between(raw, start=start, end=end)
  ```
  - `day_start` は既存の `trading.risk.limits.jst_day_start(now)`
  - `known_to_broker_label` は `trading.data.market.dukascopy` から import する。
    vendor 名のモジュールだが、`backtest/shock_trigger_study.py:50` と
    `backtest/intervention_event_study.py:50` が既に同じ形で共有ユーティリティとして
    import している既存パターンに合わせる（関数の移動はしない）
  - `BROKER_TIME_MARGIN` は `trading.execution.mt5.adapter` の既存定数（1 日）を import する。
    `history_deals_get` の日付境界はブローカーの時計で解釈されるので、
    問い合わせ窓は広げ、正確な絞り込みは純関数側の `start`/`end` 比較で行う
    （`MT5ExecutionAdapter.history_deals` と同じ考え方）

### 3-1. `None` 応答を「取引の無い日」と「取得失敗」に分ける（重要）

`MT5ExecutionAdapter.history_deals` は `None` を無条件に失敗として投げているが、
**このメソッドでは同じ扱いにしない。**

MT5 公式サンプルは `history_deals_get` の `None` に対して "No deals with group=..." と
表示しており、`None` は「該当 deal 無し」でも返りうる。約定がまだ発生していない日は
毎分これに当たるので、無条件に投げると collector が毎分落ち、
`account_snapshots` の系列に穴が空く。モジュール docstring が
「a gap in the series does not disable a limit, it moves the baseline and reports a
different loss than the real one. The series has to be kept」と書いているとおり、
この穴は損失指標のベースラインを動かすので、放置できない。

区別できるのは `last_error()` だけなので、`None` のときだけそれを読む。
公開定数 `RES_S_OK = 1`（成功）を、`execution/mt5/mapper.py` が MT5 定数を
複製しているのと同じ理由（Windows 以外でもテストできるように）で
`realized_pnl.py` に置き、collector から import する。

- `last_error()` が成功を返した = 窓に deal が無い → 空列として集計し `Decimal(0)`
- それ以外 → `MT5ConnectionError`。既存の `account_info` と同じくリトライしない

`Decimal(0)` を返すのは「取引が無かった」ときだけで、取得失敗を 0 として
永続化することはない。

### 4. broker 時刻オフセットの取り方（issue の提案からの逸脱・要記載）

issue 本文は「オフセットの取得（`symbol_info_tick`）が要る」としているが、**採らない**。

- `symbol_info_tick` が返すのは最新 tick で、週末・休場中は数時間〜数日前のものになる。
  `broker_now - now` でオフセットを出すと、休場中はそのぶんまるごとずれる
- 本システムには既に「server の壁時計は NY より
  `market.broker_server_ahead_of_ny_hours` 時間先行」という NY クローズ規約があり
  （ADR-014 / ADR-016 / ADR-024、`config/base.yaml` の既定 7.0）、
  swap rollover 境界と replay 時刻復元が同じ規約で動いている。同じ規約を使う
- `known_to_broker_label` は実 UTC → broker ラベルの向きなので DST の fold / gap で
  未定義にならない全域関数（逆写像の `broker_label_to_known` は例外を投げうる）。
  deal 1 件ごとに逆変換するのではなく、窓の両端 2 回だけ順変換する

この判断は PR 本文にも書く。

### 5. `main()` の配線

- `AccountSnapshotCollector(...)` の生成時に
  `server_ahead_of_ny_hours=config.market.broker_server_ahead_of_ny_hours` を渡す
  （`config` は既に `load_config(args.env)` で読んでいる）
- `--once` の print 文はそのまま

### 6. モジュール docstring の更新

`collector.py` の冒頭 docstring に、日次実現損益が deal 由来になったこと
（balance 差分ではないので入出金が混ざらない）を 1 段落で足す。

## 変更対象ファイル一覧

| ファイル | 変更 |
| --- | --- |
| `src/trading/data/account/realized_pnl.py` | 新規。純関数 `realized_pnl_between` |
| `src/trading/data/account/collector.py` | `build_snapshot` 引数変更 / `_realized_pnl_day` 追加 / `__init__` に `server_ahead_of_ny_hours` / `main()` 配線 / docstring |
| `tests/unit/test_account_realized_pnl.py` | 新規。純関数のテスト |
| `tests/unit/test_account_collector.py` | 既存テストの更新（下記） |

**DB マイグレーション: なし。** 口座スナップショットのスキーマは変更しない
（列の意味が変わるだけで型も名前も同じ）。マイグレーションが必要と判明したら
実装を止めて報告すること。

## テスト方針

pytest。`tests/support.py` の既存ファクトリ（`T0` / `at` / `make_snapshot` /
`FixedClock` / `FakeAccountSnapshotRepository`）を使う。実在の人物・団体名は使わない。

### 新規 `tests/unit/test_account_realized_pnl.py`

生の deal を模す `SimpleNamespace` のヘルパを 1 つ置き（`type` / `time` / `profit` /
`commission` / `swap` を持ち、MT5 に合わせて float を返す）、次を検証する:

1. **入出金 deal が実現損益に混ざらない（本 issue の核）** — 売買 deal（type 0/1）と
   `DEAL_TYPE_BALANCE`（type 2、大きな入金額を `profit` に持つ）を混ぜた系列を渡し、
   戻り値が売買 deal の合計だけになること。balance 差分なら入金額が乗ってしまう額を
   期待値にしない
2. **`profit + commission + swap` を合計する** — 3 つとも非ゼロの deal 1 件で、
   3 値の和が返ること（`Decimal` であることも確認する）
3. **窓の外の deal を除く** — `start` より前 / `end` より後の deal を混ぜ、
   窓内の分だけが合計されること。両端ちょうどの deal は含まれること
4. **deal が無い日は 0** — 空列で `Decimal(0)`

### 既存 `tests/unit/test_account_collector.py` の更新

- `snapshot_of()` ヘルパの `day_baseline=` を `realized_pnl_day=`（既定 `Decimal(0)`）に置き換える
- `test_the_day_result_is_the_balance_move_since_the_days_first_snapshot` と
  `test_the_day_result_starts_from_zero_when_the_jst_day_has_no_snapshot_yet` は
  balance 差分の振る舞いを固定しているテストなので、**deal 由来の振る舞いに書き換える**
  （テストを緩めるのではなく、対象の振る舞いが変わったための書き換え）。
  代わりに `AccountSnapshotCollector` 経由で「入金 deal があった日でも
  `realized_pnl_day` が売買分だけになる」ことを検証するテストを 1 本置く
- `FakeMt5` に `history_deals_get(date_from, date_to)` を足す。保持している生 deal のうち
  引数の窓に入るものを返す。`None` を返すモードも作れるようにし、次の 2 本を足す:
  - `last_error()` が失敗コードを返す状態で `None` → `MT5ConnectionError` が上がる
  - `last_error()` が `(RES_S_OK, "Success")` を返す状態で `None` →
    例外にならず `realized_pnl_day == Decimal(0)` のスナップショットが記録される
    （約定がまだ無い日に系列へ穴を空けないこと。3-1 参照）
- `AccountSnapshotCollector(...)` を生成している全テストに
  `server_ahead_of_ny_hours=7.0` を渡す
- `test_successive_collections_carry_the_mark_and_the_day_forward` は
  high water mark / drawdown の検証部分を残し、`realized_pnl_day` の
  期待値は deal 由来に合わせて直す

## 完了条件（実行可能なコマンド）

worktree の venv（`.venv/bin/`）で、次がすべて通ること。

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` が無変更・無指摘で終わること
- `tests/unit/test_account_realized_pnl.py` と `tests/unit/test_account_collector.py` が green
- `tests/unit/test_invariants.py` を含む既存テストを緩めないこと
- `tests/broker` は MT5 が無い環境で自動 skip される（正常）。
  `tests/integration` は PostgreSQL が要るのでローカルでは実行対象外
- lefthook の pre-push は `.venv/bin/pytest -q`（`testpaths = ["tests"]` なので全体）を
  流す。本変更はマイグレーションを足さないので、既存の `TRADING_DB_DSN` のままで通る想定

## 未確認事項

- `history_deals_get` が「該当 deal 無し」で空タプルを返すのか `None` を返すのかは
  MT5 の版に依存する。3-1 の分岐は**どちらでも正しく動く**ように書くこと
  （空タプルならそのまま集計して 0、`None` なら `last_error()` で判定）。
  実機での確認は Windows ホストでの micro-live 開始時に行う
- broker の壁時計オフセットが `broker_server_ahead_of_ny_hours: 7.0` の想定と実際に
  一致しているかは未測定（`config/config.py` の docstring も「winter rows ができたら
  received_at で検証」と書いている）。ずれると JST 日境界付近の deal が前日/翌日へ
  寄る。ずれの影響は日境界の数分に限られ、`symbol_info_tick` 方式（休場中は
  数時間〜数日ずれる）より小さいという判断（4 節）

## やらないこと

- **口座スナップショットの DB スキーマ変更**（`migrations/` に何も足さない）
- **リスク層のポリシー変更** — `src/trading/risk/limits.py` と `src/trading/risk/engine.py`
  は触らない。3 つの損失指標は equity ベースのままで正しく、この列を読んでいない
- `MT5ExecutionAdapter` / `execution/mt5/mapper.py` の変更
  （`is_trade_deal` と `broker_time_from_epoch` は読むだけ。`BrokerDeal` に
  profit/commission/swap を足さない — 執行系の照合用モデルであって集計用ではない）
- `known_to_broker_label` を別モジュールへ移す refactor
- `docs/SYSTEM_SPEC.md` の改訂（v1.3 で凍結。この変更はドメイン不変条件に触れないので
  ADR も追加しない）
- fills テーブルへの約定永続化（micro-live の別作業）
- 依頼に無い周辺リファクタ・無関係な整形・追加の抽象化

## プロジェクト規約の転記（AGENTS.md 以外に書かれているもの）

- 金額・数量・価格に float を使わない（`Decimal`）。Indicator 計算のみ float 可
- 検証はシステム境界（設定・外部 API・Broker 応答）だけで行い、内部関数間に
  防御的分岐を足さない
- frozen な pydantic モデルを破壊しない（`model_copy` パターンを維持）
- Strategy 内で `datetime.now()` を直接呼ばない（Clock 注入）。本変更は collector なので
  既存の `self._clock.now()` を使う
- WHAT を説明するコメントを書かない。コミット文脈に依存するコメント
  （「issue #36 のために追加」等）や AI レビューの引用をコードに残さない
- 構造把握は `rg`（`grep` ではなく）
- **コミットはしない。** コミットと PR は呼び出し側が行う
