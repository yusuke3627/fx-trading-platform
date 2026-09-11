# issue #148: 時間切れ決済（expected_horizon_seconds）を戦略共通に追加する

- ブランチ: `feat/issue-148-horizon-exit`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-148-horizon-exit`
- 実装担当: Codex CLI（この計画だけを読んで実装する。issue や会話履歴は読みに行かない前提）
- コミット・PR は Claude 側が行う。**Codex はコミットしない**

---

## 1. 要件（issue #148 本文の全文転記）

> タイトル: H4 Model A の前提: 時間切れ決済（expected_horizon_seconds）を戦略共通に追加し、判定規則を事前登録する
>
> ### 背景
>
> H4（失敗したスパイクは数分で平均回帰する、scalp `failed_spike_reversal`）の基礎エッジ（Model A、介入ゲートなし）を測る前に、決済構造を揃える必要がある。
>
> - 現在の戦略の決済は、ブローカー側 SL（スパイク高値 + `stop_buffer_atr` × ATR）か、反対方向のセットアップ信号（`exit_only`）だけ。利確も時間切れ決済も無い
> - `expected_horizon_seconds`（scalp 300 秒、intraday 21,600 秒）は `StrategySignal` に載るだけで、どこにも消費されていない
> - このまま測ると「スパイク反転で入って SL まで持つ」構造の成績になり、H4 の主張（数分の平均回帰）は測れない。H5 でも決済の 236/239 が protection fill だった（`docs/research/2026-09-10-h5-macro-confirmation-ablation.md`）
>
> ### やること
>
> 1. 戦略共通の base に **時間切れ決済** を追加する: 保有の経過時間が `expected_horizon_seconds` 以上になったら、保有と逆向きの `exit_only` signal（reason code `HORIZON_EXPIRED`）を 1 回だけ出す。パラメータ `horizon_exit_enabled`（bool、既定 false）で有効化する
> 2. scalp `failed_spike_reversal` と intraday `post_event_failed_breakout` の `on_event` に配線する（両方とも既定 off。scalp は `strategy_version` を 0.1.0 → 0.2.0）
> 3. テスト（unit + replay）
>
> ### 事前登録（実行前に固定。結果を見てから変えない）
>
> H4 Model A の測定は、上記マージ後に VPS で **2 腕を並列** で流す:
>
> | 腕 | 指定 |
> |---|---|
> | with | `--strategy failed_spike_reversal --param horizon_exit_enabled=true` |
> | without | `--strategy failed_spike_reversal --param horizon_exit_enabled=false` |
>
> 共通: `--env backtest --symbol USDJPY --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42`、scenario normal。YAML 由来 collector を流し切り `fx-macro` を止めてから開始（tick / bar / account / shadow は止めない）。本番の 2 年より先に 2 週間の smoke run で throughput と signal の有無を確認する。
>
> 判定規則:
>
> 1. **H4（主判定）** は with 腕（時間切れ決済あり）の **約定あたり net 損益**（執行コスト込み、carry 未値付け）の CI90（`ablation_compare` の bootstrap）で決める。約定 10 件未満なら「判定不能（標本不足）」
>    - CI90 下限 > 0 → **H4 支持**
>    - CI90 上限 < 0 → **H4 棄却**
>    - それ以外 → **判定不能（差が検出できない）**
> 2. **決済ルールの寄与（副判定）** は `ablation_compare.judge` の既存規則（維持 / 外す / 判定不能）をそのまま当てる
> 3. 併記する: 両腕の件数・hit 率・protection fill の割合・最大ドローダウン。research seed は 1 組（42）なので執行感応度は測らない（H5 と同じ限界）
>
> ### やらないこと
>
> - 利確（take profit）の追加、`ProtectionSpec.take_profit_price` の使用
> - 介入ゲート（Model B / C）の実装
> - `VirtualPosition` / DB スキーマへの `opened_at` 追加（保有開始時刻は strategy 内の memo で持つ）
> - OMS / Risk / Portfolio manager の変更
>
> ### 参考
>
> - `docs/research/2026-08-13-usdjpy.md`（Model A→B→C の ablation 計画）
> - `docs/research/2026-09-10-h5-macro-confirmation-ablation.md`（決済構造の問題と、2 腕並列の教訓）
> - `src/trading/strategy/base.py` `_setup_signal` / `_new_setup`（exit_only signal と dedupe の既存パターン）

「事前登録」節は測定手順であり、この PR の実装対象ではない（PR 本文に転記するのは Claude 側の作業）。

---

## 2. 設計（調査済み。このとおりに実装する）

### 2-1. 追加する機能: 時間切れ決済（戦略共通、既定 off）

置き場所は `src/trading/strategy/base.py` の `Strategy` クラス。helper を 1 つ足す。

```python
def _horizon_exit(
    self, ctx: StrategyContext, symbol: str, *, default_horizon_seconds: int
) -> StrategySignal | None:
```

- 既存の `_held_position`（base.py:242-246）/ `make_signal`（base.py:317-345）を使う。新しい抽象化・新しいクラスは作らない
- **保有開始時刻は strategy 内の専用 memo で持つ**: `self.__dict__.setdefault("_horizon_exits", {})` を symbol キーの dict として使い、値は「観測した保有方向・開始時刻・発火済みフラグ」の 3 つ（`tuple[PositionDirection, datetime, bool]` か小さな `NamedTuple`。どちらでもよいが、新しい公開型は作らない）。`_new_setup` の `_signaled_setups` memo と同じ流儀（base.py:223-231）
  - **`_new_setup(..., exit_only=True)` は使わない。** 既存 slot は `(symbol, direction, exit_only)` ごとに最後の setup_id を 1 つしか持たないため、session 閉鎖中の `_setup_signal`（base.py:303-315）と共有すると互いの記録を上書きし、時間切れが再発火し得る。dedupe は専用 memo の発火済みフラグで行う
  - memo の記録・削除は、有効フラグと session gate に関係なく、`_horizon_exit` が呼ばれるたびに行う（フラグを途中で切り替えても開始時刻が正しく残るように）
  - `_held_position(ctx, symbol)` が None（保有なし、または quantity 0）→ `memo.pop(symbol, None)` で消して None を返す
  - memo に symbol が無い（保有ありに変わった最初の評価）→ `(position.direction, position.as_of, False)` を記録する。`as_of` は fill 時刻で、`VirtualPositionLedger.apply_fill`（`src/trading/portfolio/virtual_ledger.py:49-77`）が `clock.now()` で刻む（OPEN / INCREASE / REDUCE のどれでも更新される）
  - memo があり **方向が同じ** → 開始時刻と発火済みフラグを**保持する**。INCREASE / REDUCE で `position.as_of` が進んでも開始時刻は変わらない
  - memo があり **方向が違う**（flat を観測せずに反転が完了した: 反転の CLOSE と OPEN は次の評価までに両方約定し得る）→ `(position.direction, position.as_of, False)` で取り直す。古い開始時刻を新しい建玉に引き継がない
- 発火条件（すべて満たすとき）:
  1. `params = ctx.config.params_for(symbol)` で `bool(params.param("horizon_exit_enabled", False))` が真（既存の `long_side_enabled` / `macro_confirmation_enabled` と同じ読み方。設定境界の型検証は足さない）
  2. 保有あり、かつ memo の発火済みフラグが False
  3. `horizon_seconds = int(params.param("expected_horizon_seconds", default_horizon_seconds))` として、`ctx.clock.now() - held_since >= timedelta(seconds=horizon_seconds)`
- 発火したら memo の発火済みフラグを True にする（1 建玉 1 回）。CLOSE fill が着くまでの間（latency 中）に再評価されても 2 回目は出ない。建玉が閉じて flat を観測すると memo が消え、次の建玉で取り直すので再び出る
- 判定は市場イベント到着時にだけ行う（タイマーは追加しない）。期限直後にイベントが無ければ次のイベントまで遅れ、約定にはさらに執行 latency が乗る。これは仕様
- 出す signal:
  ```python
  self.make_signal(
      ctx,
      symbol=symbol,
      direction=<保有と逆向き>,   # LONG 保有なら SHORT、SHORT 保有なら LONG
      conviction=1.0,
      stop_distance_pips=Decimal(0),
      expected_horizon_seconds=horizon_seconds,
      reason_codes=[HORIZON_EXPIRED],
      exit_only=True,
  )
  ```
  - `PortfolioManager.intents_from_signal`（`src/trading/portfolio/manager.py:74-81`）は `exit_only` を sizing なしで CLOSE intent にする。`stop_distance_pips <= 0` の判定（manager.py:84）は exit_only 分岐の**後**なので `Decimal(0)` でよい
  - `StrategySignal`（`src/trading/domain/signal.py`）は `conviction` が 0〜1、`stop_distance_pips` に制約なし、`expected_edge_r` は既定 1 のまま
- 定数 `HORIZON_EXPIRED = "HORIZON_EXPIRED"` を `SESSION_CLOSED_EXIT_ONLY`（base.py:54）の直後に置く。用途を 1〜2 行の日本語コメントで添える（既存の `SESSION_CLOSED_EXIT_ONLY` のコメントと同じ調子）
- `datetime.now()` を呼ばない（`ctx.clock.now()` のみ。`tests/unit/test_invariants.py:21` が `strategy/` 配下を文字列スキャンしている）。`StrategyContext`（base.py:143-154）にフィールドを足さない（`test_invariants.py:44-56` がフィールド集合を固定している）
- `timedelta` は base.py:12 で import 済み。`datetime` 型注釈が要る場合だけ同じ行に足す

### 2-2. 配線

**scalp** `src/trading/strategy/scalp/failed_spike_reversal.py`

- `on_event`（65-79 行）の symbol ループを次の形にする:
  ```python
  for symbol in context.config.instruments:
      signal = self._horizon_exit(context, symbol, default_horizon_seconds=300)
      if signal is not None:
          signals.append(signal)
          continue
      if not self._session_permits_evaluation(context, symbol):
          continue
      signal = self._evaluate(symbol, context)
      if signal is not None:
          signals.append(signal)
  ```
  - `_horizon_exit` は **session gate より先に**呼ぶ。gate が閉じている間に保有が消えても（protection fill 等）memo を消せるようにするため。gate 閉鎖中に保有があれば `_session_permits_evaluation` は元々 True なので、時間切れ signal が gate 閉鎖中に出ることは既存の exit_only signal（`_setup_signal`）と同じ扱い
  - 時間切れ signal が返った symbol では `_evaluate`（entry 評価）を**飛ばす**。返らなければ従来どおり `_evaluate` へ進む（発火後の再評価で `_evaluate` を抑止する状態は持たない）
  - 既存の非 `market.*` イベントを除外する分岐は維持する
- `strategy_version` を `"0.1.0"` → `"0.2.0"` にする（クラス属性、37 行付近）。intraday の `strategy_version` に付いているのと同じ調子で、「なぜ版を上げたか」を 1〜2 行コメントで添える（例: 時間切れ決済が入り、記録済み signal と同じ戦略として比較できないため）
- 既定値 300 は `_evaluate` 内の `params.param("expected_horizon_seconds", 300)`（89 行）と同じ値。定数化して共有してもよいが、必須ではない

**intraday** `src/trading/strategy/intraday/post_event_failed_breakout.py`

- `on_event`（78-92 行）を scalp と同じ配線にする。`default_horizon_seconds=21600`（`_evaluate` 内 106 行の既定値と同じ）
- 既定 off なので `strategy_version`（"0.2.0"）は**変えない**

**config** `config/base.yaml`

- `failed_spike_reversal` の `parameters.defaults`（144-152 行）に `horizon_exit_enabled: false` を明示する（`expected_horizon_seconds: 300` の隣）
- `post_event_failed_breakout` の flat な `parameters`（166-173 行）に `horizon_exit_enabled: false` を明示する（`expected_horizon_seconds: 21600` の隣）
- 他の env overlay（`config/shadow.yaml` / `micro_live.yaml` / `production.yaml` / `backtest.yaml` 等）は**触らない**

**research CLI / parameters**: 追加対応不要。`--param horizon_exit_enabled=true` は `parse_param_override`（`src/trading/backtest/research.py:313-328`）が `"true"/"false"` を bool にし、`with_param_overrides`（research.py:331-356）が `defaults` に上書きする。`src/trading/strategy/parameters.py` は変更しない

### 2-3. 参考にすべき既存実装

| 何を | どこ |
|---|---|
| exit_only signal と dedupe の既存パターン | `src/trading/strategy/base.py:268-315`（`_setup_signal`） |
| memo の持ち方 | `src/trading/strategy/base.py:223-231`（`_signaled_setups`） |
| `_held_position` | `src/trading/strategy/base.py:242-246` |
| exit_only → CLOSE intent | `src/trading/portfolio/manager.py:74-81` |
| `VirtualPosition` のフィールド（`as_of` を含む） | `src/trading/domain/position.py:39-59` |
| `params.param(name, default)` | `src/trading/strategy/parameters.py:55-56`（flat dict は `defaults` として扱われる: parameters.py:19-24） |
| 既存 strategy テストの ctx 組み立て | `tests/unit/test_session_entry_gate.py:93-116`（`GateProbe` と `ctx_for`。`SimpleNamespace` で `clock` / `config` / `portfolio` だけ持たせる） |
| テスト用の時刻・clock | `tests/support.py:25-39`（`T0` / `at(**kwargs)` / `FixedClock.advance(**kwargs)`） |
| manager テストの組み立て | `tests/unit/test_portfolio_manager.py:14-62`（`make_signal` / `sizing` / `manager_with` / `held`）と 170-178（exit_only の CLOSE 検証） |
| replay の probe 戦略 | `src/trading/backtest/engine.py:89-135`（`ScriptedStrategy`、strategy_id `vertical_slice_probe`） |
| replay テストのエンジン組み立てと round trip 検証 | `tests/replay/test_vertical_slice.py:49-100`（`build_engine` / `run_slice`）と 230-262（`trades` の検証。`trade.reason == "CLOSE"`） |

### 2-4. 既知の制約（実装しない。PR 本文に書く）

`PortfolioView.position` は最新の `VirtualPosition` だけを返し、建玉の識別子や fill 履歴を持たない（base.py:136-140、position.py:39-59）。次は仕様上の限界として受け入れ、helper で解決しようとしない。

- 保有開始時刻は「最初に保有を観測したときの `as_of`」。OPEN 後の最初の評価より前に INCREASE が完了した場合や、既存保有を抱えたまま strategy を再生成した場合は、その時点の `as_of` が開始時刻になる（近似）
- flat を観測せずに同方向の CLOSE → OPEN が完了した場合（発火後の CLOSE latency 中に出た同方向の setup signal がエンジンで deferred され CLOSE 直後に OPEN されるケース）は、同方向 INCREASE と区別できず、新しい建玉が前の開始時刻と発火済みフラグを引き継ぐ。latency 窓（normal で 150ms）内に新しい setup が要るので稀。検出も抑止もしない
- 1 回だけ出す signal と約定成功は別。Risk 拒否・執行拒否・部分約定でも再送しない。発火後は通常評価に戻るので、反転 setup と protection は従来どおり効く

---

## 3. テスト方針

pytest。共有ファクトリは `tests/support.py`。テストデータに実在の人物・団体名を使わない（架空値のみ）。

### 3-1. `tests/unit/test_horizon_exit.py`（新規）

`tests/unit/test_session_entry_gate.py:93-116` の `GateProbe` / `ctx_for` に倣い、`Strategy` を継承した probe と、`SimpleNamespace(clock=FixedClock(...), config=StrategyConfig(...), portfolio=SimpleNamespace(position=lambda _s, _y: held))` の ctx で `_horizon_exit` を直接呼ぶ。`StrategyConfig(strategy_id=..., instruments=["USDJPY"], parameters={"horizon_exit_enabled": True, "expected_horizon_seconds": 300})` のように flat dict で渡せる。保有は `VirtualPosition(strategy_id=<probe の id>, symbol="USDJPY", direction=..., quantity=Decimal(1000), as_of=at(...))`。

必要なケース:

1. **無効（既定）**: `horizon_exit_enabled` を渡さず、保有開始から horizon を超えていても None
2. **有効・未到達 / 到達**: 保有開始 `as_of=T0`、`FixedClock(at(seconds=299))` で None、`FixedClock(at(seconds=300))` で signal。signal は `exit_only=True`、`desired_direction` が保有と逆（LONG 保有 → SHORT。SHORT 保有 → LONG も 1 ケース）、`reason_codes == [HORIZON_EXPIRED]`、`stop_distance_pips == 0`、`generated_at == ctx.clock.now()`
3. **dedupe**: 同じ建玉で clock を進めて 2 回目を呼ぶと None。その後 `held=None` で 1 回呼んで（memo が消える）、`as_of` の違う新しい建玉を渡すと再び signal
4. **INCREASE で開始時刻が動かない**: 最初に `as_of=T0` の保有で評価（未到達）、次に `as_of=at(seconds=200)` に進めた同方向の保有を渡しても、`at(seconds=300)` で発火する（`at(seconds=500)` を待たない）
5. **flat を観測しない反転**: `as_of=T0` の LONG を観測した後、flat を挟まず `as_of=at(seconds=250)` の SHORT を渡すと、`at(seconds=300)` では None、`at(seconds=550)` で LONG 向きの exit_only signal
6. **既存 dedupe との独立**: 発火の前後に `_new_setup(symbol, direction, <別の setup_id>, exit_only=True)` を呼んでも、時間切れは再発火しない。逆に `_horizon_exit` が `_signaled_setups` に何も書かないことを確認する
7. **manager 連携**: `_horizon_exit` が返した signal を `tests/unit/test_portfolio_manager.py` の `manager_with(held(...))` に通し、`intents_from_signal(signal, sizing())` が `[CLOSE]` 1 件、`direction` が保有方向、`target_quantity == 0` になる。probe の `strategy_id` と `held` の `strategy_id` を揃えること（manager は `signal.strategy_id` で ledger を引く）

### 3-2. scalp / intraday の `on_event` 配線

`FailedSpikeReversalStrategy()` / `PostEventFailedBreakoutStrategy()` を実体化し、`await strategy.on_event(make_event(...), ctx)` を呼ぶ（`make_event` は `tests/support.py:468-479`。`event_type` の既定は `market.tick`。既存の非同期テストは `pytest.mark.asyncio` を付けている: `rg -n 'mark.asyncio' tests/unit | head` で書き方を確認）。`_evaluate` を差し替えるので、ctx は `test_session_entry_gate.py:101-116` の `ctx_for` と同じ `SimpleNamespace(clock, config, portfolio)` で足りる（`market` / `indicators` / `features` は要らない。`_session_permits_evaluation` は profile 未設定なら常に True）。`StrategyConfig` の `strategy_id` と `held` の `strategy_id` はそれぞれの戦略の `strategy_id`（`failed_spike_reversal` / `post_event_failed_breakout`）に揃える。intraday の既存 ctx 組み立ては同ファイル 391 行の `intraday_ctx` も参考になる。置き場所は `tests/unit/test_horizon_exit.py` でよい。

両戦略で各 1 本以上:

- horizon 到達時に `on_event` が exit_only signal 1 件を返し、その symbol の `_evaluate` を呼ばない（`monkeypatch.setattr(strategy, "_evaluate", ...)` で呼ばれたら `pytest.fail` する関数にする）
- 同じ保有で次のイベントを与えると signal は返らず、`_evaluate` は従来どおり呼ばれる（呼ばれたことを記録する差し替え関数で確認）
- session 閉鎖中（`ctx_for` の `profile=` と `status=StrategyStatus.MICRO_LIVE`、`OFF_SESSION` の時刻）に flat を観測すると memo が消える（その後 gate 内で新しい建玉を渡したとき、古い開始時刻で即発火しない）。scalp か intraday のどちらか 1 本でよい

### 3-3. replay（`tests/replay/test_vertical_slice.py` に追加）

- `ScriptedStrategy`（`src/trading/backtest/engine.py:89-135`）を継承した probe をテスト内に定義し、`on_event` の先頭で `self._horizon_exit(context, symbol, default_horizon_seconds=60)` を呼び、signal があればそれだけを返す。無ければ親の scripted 動作（`super().on_event`）。`ScriptedStrategy._seen` は親呼出し時だけ進むので、複数回の発火を前提にした plan を組まない
- `build_engine` / `run_slice` は `strategy_factory` と `strategy_config` を固定しているので、テスト内で `BacktestEngine(...)` を直接組む（`build_engine` の中身をコピーして `strategy_factory=lambda: HorizonProbe(...)`、`strategy_config=StrategyConfig(strategy_id=ScriptedStrategy.strategy_id, enabled=True, instruments=["USDJPY"], parameters={"horizon_exit_enabled": True, "expected_horizon_seconds": 60})` にする）。既存の `build_engine` / `run_slice` の signature は変えない
- **正常系（1 本）**: plan は `{300: PositionDirection.LONG}` だけ（反転 signal を入れない）、`stop_distance_pips=Decimal(200)`（protection が先に当たらないよう広く）。tick は `synthetic_ticks(... count=2000, seed=7)` で 1 秒間隔（`src/trading/backtest/data.py:18-28`）。検証: `result.trades` が 1 件で `reason == "CLOSE"`、`direction == "LONG"`、`exit_at - entry_at >= timedelta(seconds=60)`。fills に `("OPEN", "BUY", "LONG")` と `("CLOSE", "SELL", "LONG")` があり、`("OPEN", "SELL", "SHORT")` が**無い**（exit_only は反転を開かない）。`metrics["open_positions_at_end"] == "0"`
- **既定 off の回帰（1 本）**: 同じ probe を `horizon_exit_enabled` 無しの config で流し、既存の `run_slice(STRESS_SCENARIOS["normal"])`（plan 既定 `{300: LONG, 1200: SHORT}`）と `fills` / `trades` / `metrics` が一致することを確認する（helper を呼ぶだけでは挙動が変わらない）
- 既存の replay テストの期待値は変更しない。`ScriptedStrategy` 本体（engine.py）も変更しない

### 3-4. 触らないテスト

- `tests/unit/test_invariants.py` は触らない。落ちたら設計を見直す（テストを緩めない）
- 既存テストの期待値を変えない。`strategy_version` を文字列で固定している既存テストがあれば `rg -n '0\.1\.0' tests` で探して報告する（あっても計画外なので勝手に直さず、完了報告に書く）

---

## 4. 変更対象ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/trading/strategy/base.py` | `HORIZON_EXPIRED` 定数、`Strategy._horizon_exit` helper |
| `src/trading/strategy/scalp/failed_spike_reversal.py` | `on_event` 配線、`strategy_version` 0.2.0 |
| `src/trading/strategy/intraday/post_event_failed_breakout.py` | `on_event` 配線 |
| `config/base.yaml` | 両戦略に `horizon_exit_enabled: false` |
| `tests/unit/test_horizon_exit.py` | 新規 |
| `tests/replay/test_vertical_slice.py` | replay テスト 2 本追加 |

- DB マイグレーション: **なし**
- 共有リソース（migrations / storage / config overlay / `parameters.py`）: 触らない

---

## 5. 完了条件（実行可能なコマンド）

worktree のルートで:

```bash
.venv/bin/ruff check .                                   # 無指摘
.venv/bin/pytest tests/unit tests/replay tests/failure   # すべて green
```

`tests/integration` は PostgreSQL が要るので対象外。`tests/broker` は上のコマンドでは選択されない。

---

## 6. やらないこと

- take profit の追加、`ProtectionSpec.take_profit_price` の使用
- 介入ゲート（Model B / C）
- `VirtualPosition` / DB スキーマへの `opened_at` 追加（`src/trading/domain/` は変更しない）
- OMS / Risk / Portfolio manager / storage / migrations の変更
- `src/trading/strategy/parameters.py` への型検証の追加（既存の bool パラメータと同じ `bool(params.param(...))` で読む）
- 発火後に `_evaluate` を抑止し続ける状態機械（発火したイベントでだけ飛ばす）
- `config/shadow.yaml` / `micro_live.yaml` / `production.yaml` / `backtest.yaml` の変更
- 研究ノート（`docs/research/`）の作成・更新（測定後に別途書く）
- `src/trading/backtest/engine.py` の変更（`ScriptedStrategy` はテスト側で継承する）
- 周辺リファクタ・無関係な整形・追加の抽象化・既存コメントの書き換え
- `git commit` / `git push`（Claude 側が行う）

---

## 7. プロジェクト規約の転記（Codex 向け）

- すべてのやり取り・コメント・commit 文言は日本語（既存の英語 docstring は維持してよい）
- worktree 内でのみ編集する。メインリポジトリ `/Users/yusuke/Products/fx-trading-platform` を直接編集しない
- 構造把握は Serena、全文検索は `rg`（`grep` ではなく）
- 金額・数量・価格に float を使わない（Decimal）。Indicator 計算のみ float 可
- frozen モデル + `model_copy` のパターンを維持し、引数や共有オブジェクトを破壊しない
- 検証はシステム境界（設定・外部 API・Broker 応答）だけ。内部関数間に防御的分岐・フォールバックを足さない
- WHAT を説明するコメント、「○○のために追加」のようなコミット文脈依存のコメント、AI レビューの引用を残さない
- `tmp/` 配下・`tasks/PARENT-NOTES.md`・`tasks/APPROVAL.md` はコミット対象外（Codex はそもそもコミットしない）
- `--no-verify` は使わない（コミットしないので無関係）
- 完了報告には「変更したファイル一覧」と「実行したテストとその結果」を必ず含める。UI は無い
