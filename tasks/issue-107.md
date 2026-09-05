# issue #107: 決済専用 signal を導入し、session gate 閉鎖中も exit を通す

worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-107-exit-only-signal`
ブランチ: `feat/issue-107-exit-only-signal`
ADR 番号: **ADR-031**（着手時点の `docs/adr` 最大は ADR-030）

このファイルは実装者（Codex）向けの計画。Codex は会話も GitHub issue も読めないので、
要件・規約・参考実装をすべてここに書く。**計画に無い変更（周辺リファクタ・無関係な整形・
追加の抽象化）はしない。コミットはしない。**

---

## 1. 要件（issue #107 とユーザー判断の全文転記）

### 1.1 issue #107 の内容（要点）

ADR-028 の session profile entry gate は、閉じている間 3 strategy
（`failed_spike_reversal` / `post_event_failed_breakout` / `monetary_policy_convergence`）
の `_evaluate` 自体をスキップする。`PortfolioManager.intents_from_signal` は保有と逆方向の
signal を `CLOSE` + 新規 `OPEN` に分解するため、gate は entry だけでなく**リスク削減側の
`CLOSE` も遮断**している。gate 閉鎖中に反転 setup が成立した場合、既存ポジションは
protective stop が発動するまで残る。現在 gate が閉じるのは `config/base.yaml` の profile では
NY close → Tokyo open の 2〜3 時間だけだが、`london_ny_major`（tokyo DISABLED）のような
profile を live strategy が参照すると露出が大きくなる。

`StrategySignal` は `desired_direction`（LONG/SHORT）しか持たず exit only の signal 形が無い。
ADR-028 は「gate は Strategy 層に閉じる。runner・Portfolio・Risk は session を知らない」
「gate は `_evaluate` の前で効く。`_new_setup` より前に止めることで、gate 閉鎖中に現れた
setup は session が開いた後に signal になれる」を Accepted として決めている。

### 1.2 ユーザー判断（2026-09-06）: 案 A を採る

**決済専用の signal 形を導入し、gate が閉じている間はこれだけを通す。** 方針は
「entry は fail-close（閉鎖時間帯に OPEN / INCREASE しない）、exit は止めない」。
ADR-010（換算レートが古くても reduce/exit は止めない）と同じ考え方に揃える。ADR-028 の
「Strategy 層に閉じる」は維持し（Portfolio・Risk・runner は session を知らないまま）、
「gate 閉鎖中は `_evaluate` を丸ごとスキップする」の部分だけを改訂する。

### 1.3 設計上の要求

1. **新しい signal 形**: `StrategySignal` に決済専用を表せる形を足す。選定基準は
   「`PortfolioManager.intents_from_signal` が `CLOSE` だけを生成し、`OPEN` / `INCREASE` を
   決して生成しない」ことが型で読めること、既存の LONG/SHORT signal の経路を変えないこと、
   `StrategyContext` に執行系を足さないこと（不変条件）。
2. **PortfolioManager**: 決済専用 signal を受けたら、その symbol の既存ポジション（fresh な
   保有状態）に対する `CLOSE` intent だけを出す。ポジションが無ければ intent を出さない。
   既存の「反転 = CLOSE + OPEN」の分解は LONG/SHORT signal に対してのみ。
3. **Strategy 基底と 3 strategy**: gate が閉じている間、`_new_setup`（entry setup の生成）は
   従来どおり止める（memo に残さず、session が開いた後に signal になれる性質を維持）。
   ただし**保有ポジションの決済判定は gate に関係なく走らせる**。反転 setup が gate 閉鎖中に
   成立したら、決済専用 signal を出す（反転後の再 entry は session が開いてから通常の経路で
   成立する）。各 strategy の既存の exit ロジック（どの条件で decisions を出すか）は変えない。
4. **ADR**: `docs/adr/ADR-031-*.md` を追加。Context（#107 の問題と露出範囲）、Decision
   （案 A、entry は fail-close / exit は通す、Strategy 層に閉じる方針は維持）、ADR-028 の
   該当箇所を Amended として相互参照、Consequences（反転後の再 entry は session が開いてから、
   決済専用 signal の dedupe/memo の扱い）。ADR-028 の末尾に「ADR-031 により一部改訂」の
   1 行を足す。
5. **不変条件**: Exit は裸の反対売買にしない（OMS の fresh position select + ticket 参照は
   既存のまま）。Strategy 層から Broker/OMS/DB へ到達しない。Strategy 内で `datetime.now()`
   を呼ばない。LONG/SHORT と BUY/SELL を混同しない。

### 1.4 やらないこと

- Portfolio / Risk / runner に session の知識を足す（案 B）
- gate 閉鎖中の `INCREASE` や、反転の CLOSE に続く即時の `OPEN` を許す
- OMS / Broker / DB 層の変更、マイグレーション（**マイグレーション無し**）
- 3 strategy の entry / exit の判定条件そのものの変更
- `config/*.yaml` の profile 定義の変更
- `tests/unit/test_invariants.py` / `test_state_machine.py` の変更

---

## 2. 調査結果（現状の構造）

- `src/trading/domain/signal.py:17-39` `StrategySignal`（frozen pydantic）。
  `desired_direction: PositionDirection` は必須。
- `src/trading/strategy/base.py`
  - `:132-135` `PortfolioView` Protocol（`position(strategy_id, symbol) -> VirtualPosition | None`）。
    `StrategyContext.portfolio` に載っており、live/backtest/shadow いずれも
    `VirtualPositionLedger` が束縛される（`live/wiring.py:121`, `backtest/engine.py:752`）。
  - `:203-218` `_new_setup(symbol, direction, setup_id) -> bool`。memo は
    `self.__dict__["_signaled_setups"]: dict[(symbol, direction), setup_id]`。
  - `:220-228` `_session_permits_entry(ctx, symbol)`。profile 未参照なら True。
  - `:230-254` `make_signal(...)`。
- 3 strategy は同じ骨格。`on_event` が instruments を回し `_session_permits_entry` で
  `continue`、通れば `_evaluate(symbol, ctx)`。`_evaluate` の中で setup が成立した箇所で
  `if not self._new_setup(...): return None` → stop 距離計算 → `return self.make_signal(...)`。
  - scalp `src/trading/strategy/scalp/failed_spike_reversal.py`: on_event `:65-79`、
    SHORT site `:131-147`（setup_id = `spike_time`）、LONG site `:156-172`（setup_id = `spike_time`）
  - swing `src/trading/strategy/swing/monetary_policy_convergence.py`: on_event `:81-95`、
    SHORT site `:136-155`（setup_id = `bars[highs[-1]].start if highs else bars[-1].start`）、
    LONG site `:160-175`（setup_id = `bars[-1].start`）
  - intraday `src/trading/strategy/intraday/post_event_failed_breakout.py`: on_event `:75-89`、
    SHORT site `:131-143`（setup_id = `entry_bars[-2].start`）、LONG site `:153-165`（同）
  - **3 strategy に「保有ポジションの決済ロジック」は存在しない。** exit は
    (a) protective stop、(b) 反転 signal を `PortfolioManager` が CLOSE + OPEN に分解、の
    2 経路だけ。したがって「gate 閉鎖中の決済判定」＝「保有と逆向きの setup 成立の検出」。
- `src/trading/portfolio/manager.py:53-106` `intents_from_signal`。
  stop 距離 <= 0 → `[]`、換算 → size、size <= 0 → `[]`、その後 ledger の position を見て
  OPEN / INCREASE / CLOSE+OPEN。`_close_intent(:139-156)` は保有方向を direction に持つ
  CLOSE（target_quantity=0、protection=None）。
- 下流で `signal.desired_direction` を読む箇所（**変更しない**）:
  - `live/shadow.py:255-259` と `backtest/engine.py:774-776`: entry_price を
    `ask if LONG else bid` で決める。反転 signal の CLOSE 半分も signal の向きで価格付け
    されている（保有 LONG を閉じる SELL は bid = SHORT 側）。
  - `live/shadow.py:279-290`: `intents_from_signal` の結果を `CLOSE` とそれ以外に分け、
    CLOSE は裁定（Arbitrator）を経ず既存 book で grade する。
  - `backtest/engine.py:812-830`: CLOSE は strategy が持つ ticket 全部を対象に closes、
    ticket が無ければ NOOP。
- `domain/arbitration.py:39-52` `CandidateSignal.from_signal`（entry 候補だけが通る）。
  - `storage/postgres.py:1225-1256` `_insert_signal` は `desired_direction.value` を
    `strategy_signals.desired_direction`（`migrations/0001_initial.sql:122`:
    `TEXT NOT NULL CHECK IN ('LONG','SHORT')`）へ書く。`:1040-1050` `_row_to_signal`。
- 既存テスト:
  - `tests/unit/test_session_entry_gate.py:82-104` `GateProbe` と `ctx_for`
    （`SimpleNamespace(clock, config)`）、`:145-173` 3 strategy の `on_event` を
    `_evaluate` 差し替えで観測する parametrize テスト。
  - `tests/unit/test_strategy_dedupe.py` `_new_setup` の dedupe テスト。
  - `tests/unit/test_portfolio_manager.py:14-66` `make_signal` / `sizing` / `manager_with` / `held`。
  - `tests/support.py`: `T0`, `FixedClock`, `make_event`, `usdjpy_spec`。

### 2.1 Portfolio Arbitrator と永続化経路の追加確認

- `src/trading/live/shadow.py:250-290` は signal を先に
  `PortfolioManager.intents_from_signal` で intent へ変換し、`CLOSE` intent を `exits` へ分離した
  後、OPEN / INCREASE だけを `ArbitrationCandidate` にする。したがって `exit_only` signal は
  他 symbol / strategy の entry とランキングされず、entry 上限・重複判定・netting の対象にも
  ならない。`src/trading/live/shadow.py:296-310` で既存 book に対して直接 grade される。
- backtest は `src/trading/backtest/engine.py:757-787` で signal を PortfolioManager に直接渡し、
  Portfolio Arbitrator を通らない。決済は既存どおり `:822-844` で保有 ticket を参照し、OMS の
  fresh select を経る。
- `StrategySignal.exit_only` 自体は `strategy_signals` に保存しない。signal は PortfolioManager で
  `CLOSE` の `PositionIntent` に変換された後にだけ、`src/trading/storage/postgres.py:1100-1113` で
  signal と intent を同一トランザクションに記録する。`position_intents.action=CLOSE` は
  `:1150-1188` で保存され、`recent(:1258-1300)` は decision trail の表示用で、復元した signal を
  PortfolioManager へ戻す経路はない。
- OMS は `src/trading/oms/service.py:159-200` で CLOSE intent から ticket 参照付き command を作り、
  `src/trading/storage/postgres.py:45-65,129-162` は action / side / ticket を持つ command 自体を
  保存・復元する。claim 後に StrategySignal を再解釈しないため、`exit_only` が再 entry に変わる
  経路はない。

---

## 3. 選定した signal 形と理由

**`StrategySignal` に `exit_only: bool = False` を足す。** 決済専用 signal は
`exit_only=True` で、`desired_direction` には**決済の契機になった反転 setup の向き
（＝保有の逆）**を入れる。

理由:
- `strategy_signals.desired_direction` は `NOT NULL CHECK IN ('LONG','SHORT')`
  （`migrations/0001_initial.sql:122`）。`desired_direction: PositionDirection | None` にすると
  永続化にマイグレーションが要り、「マイグレーション無し」に反する。
- 別クラス `ExitSignal` は runner / arbitration / storage / shadow / backtest / repository
  Protocol / `tests/support.py` の `StrategySignal` 型注釈すべてに波及し、最小差分にならない。
- 反転 setup の向きを `desired_direction` に載せると、shadow / backtest の価格付け
  （`ask if LONG else bid`）が既存の「反転 signal の CLOSE 半分」と同じ側になり、下流を
  一切変えずに正しい side で決済できる。CLOSE intent は shadow で裁定を経ず、backtest で
  ticket 参照の closes になる（いずれも既存経路）。
- `PortfolioManager.intents_from_signal` の先頭で `if signal.exit_only:` として CLOSE だけを
  返す分岐を置くので、「OPEN / INCREASE を決して生成しない」ことがその場で読める。
- 永続化には `exit_only` 列が無い。決済専用であったことは reason_code
  `SESSION_CLOSED_EXIT_ONLY` と、intent が CLOSE のみであることから trail に残る
  （列追加は本 issue の範囲外。ADR の Consequences に書く）。

---

## 4. 実装詳細

### 4.1 `src/trading/domain/signal.py`

`StrategySignal` に追加:

```python
    # True なら「保有を閉じるだけ」の signal。desired_direction には決済の契機になった
    # 反転 setup の向き（保有の逆）が入り、Portfolio はそれを OPEN しない（ADR-031）。
    exit_only: bool = False
```

置き場所は `reason_codes` の前後どちらでもよい。モジュール docstring に
「exit_only は決済専用」の 1 文を足す。

### 4.2 `src/trading/portfolio/manager.py`

`intents_from_signal` の**先頭**（`if signal.stop_distance_pips <= 0:` より前）に:

```python
        if signal.exit_only:
            # 決済専用 signal は size も換算も要らない: 保有があれば CLOSE だけ、
            # 無ければ何もしない。OPEN / INCREASE はここから決して出ない（ADR-031）。
            current = self._ledger.position(signal.strategy_id, signal.symbol)
            if current is None or current.quantity == 0:
                return []
            return [self._close_intent(signal, current.direction)]
```

docstring の末尾段落に「exit_only signal は保有の CLOSE だけを生む」旨を 1〜2 文足す。
`_entry_intent` / `_close_intent` は変更しない。

### 4.3 `src/trading/strategy/base.py`

1. モジュール定数を追加（`LIVE_ELIGIBLE_STATUSES` の近く）:

```python
# gate 閉鎖中に反転 setup が成立したとき、entry の代わりに出す決済専用 signal の印。
SESSION_CLOSED_EXIT_ONLY = "SESSION_CLOSED_EXIT_ONLY"
```

2. `_new_setup` に keyword-only の `exit_only: bool = False` を足し、memo の slot を
   `(symbol, direction, exit_only)` にする（型注釈も
   `dict[tuple[str, PositionDirection, bool], object]`）。既存の呼び出し
   （`backtest/engine.py:117`、`tests/unit/test_strategy_dedupe.py`）は位置引数 3 つのままで
   動く。docstring に「決済専用 signal は entry とは別 slot で dedupe する。gate 閉鎖中に
   決済専用 signal を出した setup は、session が開けば同じ setup_id で entry になれる」を足す。

3. `_session_permits_entry` は**変更しない**。

4. 新規 `_held_position(self, ctx, symbol) -> VirtualPosition | None`:

```python
    def _held_position(self, ctx: StrategyContext, symbol: str) -> VirtualPosition | None:
        position = ctx.portfolio.position(self.strategy_id, symbol)
        if position is None or position.quantity == 0:
            return None
        return position
```

5. 新規 `_session_permits_evaluation(self, ctx, symbol) -> bool`:

```python
    def _session_permits_evaluation(self, ctx: StrategyContext, symbol: str) -> bool:
        """gate が閉じていても、保有がある instrument は反転 setup の検出（決済専用
        signal）のために評価へ進む。保有が無ければ従来どおり評価自体を省く。"""
        return self._session_permits_entry(ctx, symbol) or (
            self._held_position(ctx, symbol) is not None
        )
```

6. 新規 `_setup_signal(...)`（`make_signal` の直前に置く）:

```python
    def _setup_signal(
        self,
        context: StrategyContext,
        *,
        symbol: str,
        direction: PositionDirection,
        setup_id: object,
        conviction: float,
        expected_edge_r: Decimal = Decimal(1),
        stop_distance_pips: Decimal,
        expected_horizon_seconds: int,
        reason_codes: list[str],
    ) -> StrategySignal | None:
        """成立した setup を 1 回だけ signal にする。

        gate が開いていれば entry signal。閉じていれば、保有と逆向きの setup だけを
        決済専用 signal に変え、entry 用の memo には触れない — gate 閉鎖中に現れた
        setup は session が開いた後に entry signal になれる（ADR-028 / ADR-031）。
        同方向の setup は INCREASE になるので閉鎖中は出さない。
        """
        if self._session_permits_entry(context, symbol):
            if not self._new_setup(symbol, direction, setup_id):
                return None
            return self.make_signal(
                context,
                symbol=symbol,
                direction=direction,
                conviction=conviction,
                expected_edge_r=expected_edge_r,
                stop_distance_pips=stop_distance_pips,
                expected_horizon_seconds=expected_horizon_seconds,
                reason_codes=reason_codes,
            )
        held = self._held_position(context, symbol)
        if held is None or held.direction is direction:
            return None
        if not self._new_setup(symbol, direction, setup_id, exit_only=True):
            return None
        return self.make_signal(
            context,
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            expected_edge_r=expected_edge_r,
            stop_distance_pips=stop_distance_pips,
            expected_horizon_seconds=expected_horizon_seconds,
            reason_codes=[*reason_codes, SESSION_CLOSED_EXIT_ONLY],
            exit_only=True,
        )
```

7. `make_signal` に `exit_only: bool = False` の keyword 引数を足し、`StrategySignal(...)` に
   `exit_only=exit_only` を渡す。

`StrategyContext` にはフィールドを足さない。`datetime.now()` は呼ばない
（`context.clock.now()` のみ）。

### 4.4 3 strategy（同じ機械的な置き換え）

対象: `scalp/failed_spike_reversal.py`、`swing/monetary_policy_convergence.py`、
`intraday/post_event_failed_breakout.py`。intraday は現在 profile を参照していないので
挙動は変わらないが、基底の gate 意味論を 3 strategy で揃えるため（そして
`test_session_entry_gate.py` の parametrize を 3 strategy で共通に保つため）同じ置き換えを行う。

1. `on_event`: `if not self._session_permits_entry(context, symbol): continue` を
   `if not self._session_permits_evaluation(context, symbol): continue` に変える。

2. `_evaluate` の各 setup site（scalp 2 箇所、swing 2 箇所、intraday 2 箇所）:
   `if not self._new_setup(symbol, DIR, setup_id): return None` の 2 行を削除し、stop 距離の
   計算はそのまま残し、`return self.make_signal(ctx, symbol=..., direction=DIR, ...)` を
   `return self._setup_signal(ctx, symbol=..., direction=DIR, setup_id=<元の setup_id 式>, ...)`
   に変える。`conviction` / `stop_distance_pips` / `expected_horizon_seconds` / `reason_codes`
   は元の値をそのまま渡す。setup_id の式は §2 に書いたとおり各 site の元の式を使う
   （swing SHORT は `setup_id = ...` のローカル変数が既にあるのでそれを渡す）。
   `_new_setup` が stop 計算の後ろへ移るが、stop 計算は純粋な算術なので順序は問題ない。
   setup 検出の条件式・stop 距離・conviction・reason_codes・spread gate・feature gate は
   一切変えない。

### 4.5 ADR

`docs/adr/ADR-031-exit-only-signal-through-session-gate.md` を新規作成。既存 ADR
（ADR-028 / ADR-010）と同じ体裁（`# ADR-031: ...` / `**Status:** Accepted (2026-09-06)` /
`## Context` / `## Decision` / `## Consequences`）。日本語。内容:

- Context: §1.1 の問題（gate が反転 CLOSE も遮断、protective stop まで残る、
  `london_ny_major` 等で露出が広がる）、ADR-028 が `_evaluate` 丸ごとスキップを決めていたこと。
- Decision:
  - 案 A。「entry は fail-close、exit は止めない」（ADR-010 と同じ非対称）。
  - `StrategySignal.exit_only`。`desired_direction` は反転 setup の向き（保有の逆）で、
    Portfolio は OPEN しない。理由（DB 列の CHECK、下流の価格付け）を短く。
  - Portfolio は `exit_only` を見て保有の CLOSE だけを出す。保有が無ければ何も出さない。
    session は知らない（ADR-028 の「Strategy 層に閉じる」は維持）。
  - Strategy 基底: gate 閉鎖中は保有がある instrument だけ評価へ進む。成立した setup は
    `_setup_signal` で、gate 開なら entry、閉なら保有と逆向きのときだけ決済専用 signal。
    entry 用の `_new_setup` memo は閉鎖中に触らない。同方向 setup（INCREASE）は閉鎖中に出さない。
  - ADR-028 の「gate は `_evaluate` の前で効く」を本 ADR で改訂（Amended）。他の決定
    （policy × status の表、重なる session の扱い、profile の受け渡し）は維持。
- Consequences:
  - gate 閉鎖中に保有を持つ strategy は、反転 setup 成立で決済が通る。反転後の再 entry は
    session が開いてから通常経路（同じ setup_id でも entry memo は未消費なので成立できる）。
  - 決済専用 signal は entry とは別 slot（`(symbol, direction, exit_only=True)`）で dedupe する。
  - `strategy_signals` に exit_only 列は無い。trail では reason_code
    `SESSION_CLOSED_EXIT_ONLY` と CLOSE のみの intent から読む。列追加は別 issue。
  - profile を参照する strategy は、保有中に限り閉鎖時間帯も `_evaluate` を走らせる
    （backtest / shadow のコストは保有中だけ増える）。
  - shadow の仮想 book は fill が届かないため通常空で、shadow で決済専用 signal が出るのは
    保有を記録した場合に限る。

`docs/adr/ADR-028-session-profile-entry-gate.md` の末尾に 1 行:
`ADR-031 により一部改訂（gate 閉鎖中も保有の決済専用 signal は通す）。`

---

## 5. テスト方針（既存を緩めない。`tests/support.py` のファクトリを使う。実在人物名不使用）

### 5.1 `tests/unit/test_portfolio_manager.py`（追加のみ。既存テストは不変）

`make_signal` に `exit_only: bool = False` の引数を足して `StrategySignal(..., exit_only=exit_only)`
を渡す。追加テスト:

- `test_exit_only_signal_closes_the_held_position_without_reopening`:
  `manager_with(held(LONG))` に `make_signal(direction=SHORT, exit_only=True)` →
  `[i.action for i in intents] == [PositionAction.CLOSE]`、`intents[0].direction is LONG`、
  `target_quantity == 0`、`protection is None`。
- `test_exit_only_signal_without_a_position_yields_nothing`:
  `manager_with()` → `[]`。quantity 0 の VirtualPosition を記録した場合も `[]`。
- `test_exit_only_close_survives_conversion_failure`:
  EURUSD 保有 LONG + quote 無し（`test_conversion_failure_keeps_the_reversal_close` と同じ構成）
  + `make_signal(symbol="EURUSD", exit_only=True)` → `[CLOSE]` のみ（ADR-010 と同じ非対称）。

### 5.2 `tests/unit/test_session_entry_gate.py`（ADR-028 のテストを ADR-031 に合わせて改訂）

- モジュール docstring に ADR-031 を追記。
- `ctx_for` に `held: VirtualPosition | None = None` を足し、
  `SimpleNamespace(clock=..., config=..., portfolio=SimpleNamespace(position=lambda sid, sym: held))`
  を返す（`PortfolioView` の形）。`held=None` のときも `portfolio` 属性は必ず持たせる
  （gate 閉鎖時は `_held_position` が `ctx.portfolio.position` を呼ぶ）。`held` は `VirtualPosition(strategy_id=<strategy_id>,
  symbol="USDJPY", direction=..., quantity=Decimal(1000), as_of=<instant>)`。
- 既存 `test_closed_session_stops_before_evaluation` は「保有が無ければ評価しない」に改名
  （例 `test_closed_session_without_a_position_stops_before_evaluation`）し、内容はそのまま。
- 追加（3 strategy parametrize）`test_closed_session_with_a_position_reaches_evaluation`:
  `held=LONG` で `on_event` → `_evaluate` が `("USDJPY", ctx)` で呼ばれる。
- 追加（`GateProbe` を使う基底テスト。`_setup_signal` を直接呼ぶ）:
  - `test_open_session_setup_becomes_an_entry_signal_once`: gate 開（`USDJPY_CORE`, TOKYO_ONLY,
    SHADOW）で `_setup_signal(direction=SHORT, setup_id=X)` → signal（`exit_only is False`、
    `desired_direction is SHORT`）。同じ setup_id で再度 → `None`。
  - `test_closed_session_setup_without_a_position_is_not_remembered`: gate 閉
    （`CLOSED_EVERYWHERE`, TOKYO_ONLY）で flat → `None`。同じ probe インスタンスで gate 開の ctx
    （`USDJPY_CORE`）に同じ setup_id → entry signal が出る。
  - `test_closed_session_reversal_setup_becomes_an_exit_only_signal`: gate 閉 + `held=LONG` +
    `direction=SHORT` → signal で `exit_only is True`、`desired_direction is SHORT`、
    `SESSION_CLOSED_EXIT_ONLY in reason_codes`、元の reason_codes も残る。同じ setup_id で再度
    → `None`（dedupe）。その後 gate 開の ctx に同じ setup_id → entry signal（`exit_only False`）
    が出る（entry memo は未消費）。
  - `test_closed_session_same_direction_setup_is_dropped`: gate 閉 + `held=SHORT` +
    `direction=SHORT` → `None`（INCREASE を出さない）。その後 gate 開で同じ setup_id →
    entry signal。
  - `test_open_session_ignores_the_held_position`: gate 開 + `held=LONG` + `direction=SHORT` →
    通常の entry signal（`exit_only False`）。反転は Portfolio が CLOSE+OPEN に分解する既存経路。
- `_setup_signal` の呼び出しには `conviction=0.5`, `stop_distance_pips=Decimal(10)`,
  `expected_horizon_seconds=60`, `reason_codes=["PROBE"]` を使う。

### 5.3 `tests/unit/test_strategy_dedupe.py`（追加 1 本）

- `test_exit_only_setups_dedupe_in_their_own_slot`:
  `_new_setup("USDJPY", SHORT, X, exit_only=True)` True → 同じで False →
  `_new_setup("USDJPY", SHORT, X)`（entry slot）は True。

### 5.4 `tests/unit/test_shadow_runner.py`（追加 1 本）

- `test_exit_only_signal_bypasses_arbitration_and_persists_as_close_intent`:
  USDJPY の LONG 保有に対する exit-only SHORT signal と、EURUSD の通常 entry signal を同一 cycle
  に出す。USDJPY は CLOSE だけになり arbitration が付かず、EURUSD だけが arbitration を通る。
  decision repository に記録される intent も CLOSE / OPEN の 2 本で、exit-only signal から
  OPEN が生成されないことを固定する。

### 5.5 変更しないテスト

`tests/unit/test_invariants.py`、`tests/unit/test_state_machine.py`、`tests/replay/*`、
`tests/failure/*`。すべて無変更で green であること。

---

## 6. 変更対象ファイル一覧（網羅）

| ファイル | 変更 |
| --- | --- |
| `src/trading/domain/signal.py` | `exit_only: bool = False` を追加、docstring |
| `src/trading/portfolio/manager.py` | `intents_from_signal` 先頭に exit_only 分岐、docstring |
| `src/trading/strategy/base.py` | `SESSION_CLOSED_EXIT_ONLY`、`_new_setup(exit_only=)`、`_held_position`、`_session_permits_evaluation`、`_setup_signal`、`make_signal(exit_only=)` |
| `src/trading/strategy/scalp/failed_spike_reversal.py` | on_event 1 行、setup site 2 箇所 |
| `src/trading/strategy/swing/monetary_policy_convergence.py` | 同上 |
| `src/trading/strategy/intraday/post_event_failed_breakout.py` | 同上 |
| `docs/adr/ADR-031-exit-only-signal-through-session-gate.md` | 新規 |
| `docs/adr/ADR-028-session-profile-entry-gate.md` | 末尾 1 行 |
| `tests/unit/test_portfolio_manager.py` | `make_signal(exit_only=)`、テスト 3 本追加 |
| `tests/unit/test_session_entry_gate.py` | `ctx_for(held=)`、既存 1 本改名、テスト追加 |
| `tests/unit/test_strategy_dedupe.py` | テスト 1 本追加 |
| `tests/unit/test_shadow_runner.py` | exit-only が裁定対象外で CLOSE intent として記録されるテスト 1 本追加 |
| `tasks/issue-107.md` | Portfolio Arbitrator・永続化経路の確認結果とテスト方針を追記 |

触らないもの: `runner.py`、`live/shadow.py`、`backtest/engine.py`、`storage/*`、
`domain/arbitration.py`、`portfolio/arbitrator.py`、`risk/*`、`oms/*`、`migrations/*`、
`config/*.yaml`、`docs/SYSTEM_SPEC.md`。

---

## 7. 完了条件（実行コマンド）

```bash
cd /Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-107-exit-only-signal
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure -q
```

両方が無変更で通ること（broker テストは MT5 なし環境で自動 skip、integration は対象外）。

---

## 8. 規約の転記

- 金額・数量・価格は `Decimal`（indicator 計算のみ float 可）
- frozen モデル + `model_copy`。引数・共有オブジェクトを破壊しない
- 検証はシステム境界だけ。内部関数に防御的分岐・フォールバックを足さない
- WHAT を説明するコメントは書かない。「なぜ」だけ docstring / コメントに。AI レビューの
  引用や「〜のために追加」のような文脈依存コメントを残さない
- 通貨ペア・pip・時間足をハードコードしない（`InstrumentSpec` / config 経由）
- Strategy 層から Broker・OMS・DB へ到達しない。`StrategyContext` に執行系を足さない
- Strategy 内で `datetime.now()` を直接呼ばない（`ctx.clock.now()`）
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない
- テストデータに実在する人物・団体名を使わない
- コード内コメント・docstring は日本語優先（既存の英語 docstring は維持してよい）
- ruff（`pyproject.toml`、line-length 100）に準拠。型注釈を付ける
- **コミットしない**

---

## 9. 計画時の判断（ユーザー確認なしで決めた点）

- **intraday も同じ置き換えを行う**: 現在 profile 未参照で挙動は変わらないが、基底の gate
  意味論を 3 strategy で揃え、profile を将来参照したときに #107 と同じ穴を残さないため。
- **決済専用 signal の dedupe は entry と別 slot**: gate 閉鎖中に決済専用 signal を出した
  setup が、session 開後に同じ setup_id で entry になれる（ADR-028 の「閉鎖中に現れた setup
  は開いた後に signal になれる」を維持）。共有 slot にすると、swing のように setup が
  数日続く strategy で反転後の entry が次の setup まで出なくなる。
- **shadow への影響**: `config/shadow.yaml` で SHADOW なのは `failed_spike_reversal`
  （profile `usdjpy_scalp_research`）と `post_event_failed_breakout`（profile 未参照）。
  shadow の仮想 ledger は fill が届かず通常空なので、決済専用 signal が実際に出るのは
  backtest / live（保有あり）に限る。

未確認事項: なし（計画内の path:line はすべて worktree のコードで確認済み）。

---

## 10. 実装時の追加（セルフレビューでの修正）

- `src/trading/portfolio/virtual_ledger.py`: `position()` が閉鎖中の市場 event ごとに
  strategy から呼ばれるようになったため、append-only の履歴を毎回走査せず
  (strategy_id, symbol) の最新 snapshot を索引で持つ（挙動は同じ。tick 単位 replay の
  閉鎖時間帯で O(履歴長) の走査が積み上がるのを避ける）。`tests/unit/test_virtual_ledger.py`
  に「後から記録した古い as_of の snapshot は現在値を置き換えない」を 1 本追加。
- `tests/unit/test_shadow_runner.py`: 決済専用 signal が shadow で裁定を経ず CLOSE intent
  として trail に残ることを固定するテストを 1 本追加（Codex 実装時）。
