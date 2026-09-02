# issue #64（後半）実装計画: OMS priority queue / rate limiter / send 直前 revalidation

- issue: https://github.com/yusuke3627/fx-trading-platform/issues/64（本計画は issue の**後半 = OMS 部分だけ**。前半の Portfolio Arbitrator は別 worktree で並行実装中）
- branch: `feat/issue-64-oms-priority-queue`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-64-oms-priority-queue`
- ベースライン: `origin/main` (d4366c9) で `.venv/bin/ruff check .` = All checks passed、`.venv/bin/pytest tests/unit -q` = 802 passed
- PR は issue #64 の一部なのでコミットに `Fixes #64` は書かない（本文に `Part of #64`）。**コミットは Claude 側が行う。Codex はコミットしない。**

---

## 0. 要件（issue #64 本文の該当部分と設計書の転記。Codex は issue を読まない前提）

issue #64「M5: Portfolio Arbitrator（同時 signal 裁定）と OMS rate-limit priority queue」のうち、本計画が担うのは次の 1 項目:

> OMS: priority queue（emergency > close/reduce > protection repair > new entry > telemetry）、per-symbol 5 req/s・market new entry 1/s の rate limiter、queue 待機中 candidate の send 直前 full revalidation

テスト要件（issue「テスト」節 = 設計書 §34.7）:

> 34.7 Unit — OMS / Rate Limit: per-symbol 5 req/sec / entry 1/sec / exits prioritized / queued entry expires / queued entry revalidated
> 34.8 既存 `test_invariants.py` を全件維持

設計書 `docs/research/2026-08-25-fx-multicurrency-system-design-v2.1.md` の該当節（原文）:

- §32.2 Rate Limits: 「実測制約として設計に織り込む: 同一 symbol: 最大 5 requests/sec、market new entry: 1/sec。Rate limiter は OMS / adapter 側。」
- §32.3 Priority Queue: 優先順位 1. Emergency / forced risk reduction、2. Position close / reduce、3. Protective order repair、4. Accepted new entries、5. Non-critical amendments / telemetry。「4 ペア同時 new entry は Portfolio Arbitrator の順位を維持しつつ 1 秒間隔で queue する。」
- §32.4 Queue 中の Revalidation: 「queue 待機中に signal expiry / price move / spread expansion / event mode change / conversion staleness / portfolio exposure change が起きる可能性がある。したがって send 直前に pre-trade risk を再実行する。」
- §33.12 Rate-Limit Stale Entry: 「4 signal を queue し、最後の order が古い price で送られる。対策: send 直前 full revalidation」

OMS の正本 `docs/SYSTEM_SPEC.md` L45-55「OMS State Machine」:

```
CREATED → RISK_APPROVED → READY → CLAIMED → SUBMITTING → ACKNOWLEDGED → PARTIAL_FILL → FILLED
異常: REJECTED / CANCELLED / EXPIRED / UNKNOWN
- CLAIMED + lease 失効 + broker request 未開始 → READY へ回収可
- SUBMITTING で死んだら UNKNOWN。再送禁止、Reconciliation でのみ解決
- Claim は PostgreSQL FOR UPDATE SKIP LOCKED
```

---

## 1. 設計方針（確定事項）

### 1.1 queue の置き場所: DB claim の後、SUBMITTING の直前（in-memory）

```
READY ──claim_next (DB, FOR UPDATE SKIP LOCKED, created_at 順)──> CLAIMED
   CLAIMED ──enqueue──> ExecutionQueue（in-memory、priority 順）
      dispatch(): expiry → lease → rate limit → fresh select（ticket 付き exit）→ revalidation
         → SEND: mark_submitting → SUBMITTING（呼び出し元が persist して broker へ送る）
         → EXPIRED / CANCELLED: 送らず、遷移済みのコピーを返す（呼び出し元が persist）
```

- DB の claim（`src/trading/storage/postgres.py:208` `claim_next`、`src/trading/oms/claim.py:14` `CLAIM_SQL`）は「READY 行を 1 行ずつ `created_at` 順に取る」だけで、複数行を見比べて優先順位を決められない。優先順位付けは、worker が claim した CLAIMED コマンドを **in-memory の queue に載せてから**行う。
- したがって **migration は追加しない**。`execution_commands` テーブルにも `src/trading/storage/` にも触らない。priority は command の `action`（+ 呼び出し元が指定する EMERGENCY 等）から導出する。
- queue は状態遷移の合法性を保つため **CLAIMED のコマンドだけを受け付ける**（`enqueue` で `ValueError`）。CLAIMED からは `SUBMITTING` / `EXPIRED` / `CANCELLED` への遷移が既に許可されている（`src/trading/oms/state_machine.py:16` `_ALLOWED`、CLAIMED 行は L27-30）ので、**state machine は変更しない**。
- revalidation で reject されたコマンドは **`CANCELLED`** にする（`REJECTED` は CLAIMED から到達不可。`REJECTED` は「CREATED 時点の risk 拒否」と「broker 拒否」に予約し、queue が取り下げた注文は `CANCELLED` と区別する）。RiskDecision（reject codes 付き）は dispatch 結果に添えて返し、呼び出し元が decision trail に記録できるようにする。
- worker ループ（claim → enqueue → dispatch → persist → `order_send`）と live 配線は **本 PR のスコープ外**（M6 execute path）。本 PR は queue / limiter / revalidation の部品と unit テストまで。

### 1.2 priority と決定論的順序

```python
class QueuePriority(IntEnum):        # 小さいほど先に送る
    EMERGENCY = 0          # forced risk reduction（kill switch 起因の強制縮小）
    CLOSE_REDUCE = 1       # REDUCE / CLOSE
    PROTECTION_REPAIR = 2  # SL/TP の修復（本 PR に producer は無いが順位として定義する）
    NEW_ENTRY = 3          # OPEN / INCREASE
    TELEMETRY = 4          # 非クリティカルな amendment / telemetry（同上）
```

並び順キー（`QueuedCommand.sort_key()`）:

```
(priority, 0 if arbitration_rank is not None else 1,
 arbitration_rank if arbitration_rank is not None else sequence,
 sequence)
```

- `sequence` は queue が enqueue 時に採番する連番（`itertools.count()`）。
- `arbitration_rank` は Portfolio Arbitrator（別 worktree で実装中）が付ける順位。呼び出し元が `enqueue` の引数で渡す `int | None`。**本 PR は portfolio パッケージを import しない。**
- 同 priority では rank 付きが rank 順で先、rank 無しは enqueue 順。rank が同値なら `sequence` で決まる。→ 同じ集合を別の順で enqueue しても dispatch 順は同じ（rank 付き）／enqueue 順（rank 無し）で、入力順序に依存する非決定性は無い。

`priority_for(command, *, forced_risk_reduction=False)`: `forced_risk_reduction=True` なら `EMERGENCY`、`action in (REDUCE, CLOSE)` なら `CLOSE_REDUCE`、それ以外（OPEN / INCREASE）は `NEW_ENTRY`。`PROTECTION_REPAIR` / `TELEMETRY` は `enqueue(priority=...)` で明示指定する。

### 1.3 rate limiter: sliding window 2 段、`now` は引数で受け取る

```python
class RateLimitConfig(BaseModel):                     # frozen
    per_symbol_requests_per_second: int = Field(default=5, ge=1)
    market_entries_per_second: int = Field(default=1, ge=1)

class RateLimiter:
    WINDOW = timedelta(seconds=1)
    def __init__(self, config: RateLimitConfig) -> None
    def allows(self, symbol: str, *, market_entry: bool, now: datetime) -> bool
    def record(self, symbol: str, *, market_entry: bool, now: datetime) -> None
```

- 段 1: symbol ごとに「直近 1 秒（`now - WINDOW < t <= now`）の record 数 < `per_symbol_requests_per_second`」。entry / exit を問わず全 request を数える。
- 段 2: `market_entry=True`（`action in (OPEN, INCREASE)`）のときだけ、全 symbol 共通で「直近 1 秒の entry 数 < `market_entries_per_second`」も要求する。
- 実装は `dict[str, deque[datetime]]`（symbol 別）+ `deque[datetime]`（entry 全体）。`allows` / `record` の先頭で `t <= now - WINDOW` を左から捨てる。
- token bucket ではなく sliding window を選ぶ理由: 「任意の 1 秒窓で N 件以下」を厳密に満たし、`FixedClock.advance` で境界（0.999 秒は拒否、1.000 秒で許可）をそのままテストできる。
- limiter は **`datetime.now()` を呼ばない**。時刻は queue が Clock から 1 回読んで `now` として渡す（expiry 判定・lease 判定・revalidation と同一時刻で評価するため）。
- 値は `config/base.yaml` の `broker.rate_limit` から読む（1.5 節）。設定境界で `ge=1` を検証し、limiter 内部に防御的分岐は置かない。

### 1.4 queue と dispatch

```python
class Revalidator(Protocol):
    def revalidate(self, entry: QueuedCommand, now: datetime) -> RiskDecision: ...

@dataclass(frozen=True)
class QueuedCommand:
    command: ExecutionCommand          # state == CLAIMED
    intent: PositionIntent             # revalidation で RiskEngine.evaluate(intent, ctx) に渡すため保持
    priority: QueuePriority
    sequence: int
    arbitration_rank: int | None = None
    expires_at: datetime | None = None # signal の有効期限（entry）。exit は None（期限なし）
    def sort_key(self) -> tuple[int, int, int, int]

class DispatchOutcome(StrEnum):
    SEND = "SEND"                                    # 送ってよい。command は SUBMITTING のコピー
    EXPIRED = "EXPIRED"                              # expires_at 超過。command は EXPIRED のコピー
    LEASE_EXPIRED = "LEASE_EXPIRED"                  # claim lease 失効。command は無変更（回収は recovery sweep の責務）
    ALREADY_CLOSED = "ALREADY_CLOSED"                # ticket 付き exit の fresh select で position 無し。command は CANCELLED のコピー
    REVALIDATION_REJECTED = "REVALIDATION_REJECTED"  # pre-trade risk 再評価で不承認。command は CANCELLED のコピー、decision 付き

@dataclass(frozen=True)
class Dispatch:
    outcome: DispatchOutcome
    entry: QueuedCommand
    command: ExecutionCommand
    decision: RiskDecision | None = None

class ExecutionQueue:
    def __init__(self, *, clock: Clock, rate_limiter: RateLimiter,
                 revalidator: Revalidator, broker: BrokerPositionReader) -> None
    def enqueue(self, command: ExecutionCommand, intent: PositionIntent, *,
                priority: QueuePriority, arbitration_rank: int | None = None,
                expires_at: datetime | None = None) -> QueuedCommand
    def pending(self) -> tuple[QueuedCommand, ...]   # sort_key 順
    def __len__(self) -> int
    def dispatch(self) -> Dispatch | None            # 1 回の呼び出しで最大 1 件
```

`enqueue` の前提条件（違反は `ValueError`）:

- `command.state is CommandState.CLAIMED` でない
- 同じ `command_id` が既に queue にある（二重送信の芽を API 境界で断つ）

`dispatch()` のアルゴリズム（1 回で最大 1 件を処理する。`now = self._clock.now()` を先頭で 1 回だけ読む）:

```
for entry in sorted(self._entries, key=QueuedCommand.sort_key):
    cmd = entry.command
    if entry.expires_at is not None and now >= entry.expires_at:
        remove(entry); return Dispatch(EXPIRED, entry, transition(cmd, CommandState.EXPIRED, now=now))
    if cmd.claim_expires_at is not None and now >= cmd.claim_expires_at:
        remove(entry); return Dispatch(LEASE_EXPIRED, entry, cmd)
    market_entry = cmd.action in (PositionAction.OPEN, PositionAction.INCREASE)
    if not self._limiter.allows(cmd.symbol, market_entry=market_entry, now=now):
        continue                      # rate limit 中の entry は飛ばし、次の候補を見る（1.4.1）
    remove(entry)
    if cmd.broker_position_ticket is not None and self._broker.position(cmd.broker_position_ticket) is None:
        return Dispatch(ALREADY_CLOSED, entry, transition(cmd, CommandState.CANCELLED, now=now))
    decision = self._revalidator.revalidate(entry, now)
    reduced = decision.approved_quantity is not None and decision.approved_quantity < cmd.quantity
    if not decision.approved or reduced:
        return Dispatch(REVALIDATION_REJECTED, entry,
                        transition(cmd, CommandState.CANCELLED, now=now), decision)
    self._limiter.record(cmd.symbol, market_entry=market_entry, now=now)
    return Dispatch(SEND, entry, mark_submitting(cmd, now))
return None                           # 空、または全件 rate limit 待ち
```

#### 1.4.1 rate limit 中の entry を飛ばしてよい理由

- 同一 symbol の entry は同じ per-symbol 窓に掛かるので、飛ばしても同 symbol 内の相対順序は変わらない。
- market entry（OPEN / INCREASE）は全 symbol 共通の 1/sec 窓に掛かるので、先頭の entry が待ちなら後続の entry も全て待ち。Arbitrator 順位は崩れない。
- 飛ばした先で送れるのは、別 symbol の exit など「別の窓」に属するものだけ。exit を entry の rate limit で待たせない、という §32.3 の趣旨に合う。

#### 1.4.2 各ステップの根拠

- **expiry を最初に見る**: 期限切れは rate limit の有無に関わらず送らない（§33.12）。expiry は「queue 待機」中の signal 失効を表す `expires_at`（呼び出し元が Arbitrator の candidate から渡す）。exit は `None` で期限無し。
- **lease**: CLAIMED の lease（`claim_expires_at`）が queue 内で切れた行は、recovery sweep（`src/trading/oms/claim.py:65` `recovery_state`）が READY に戻して再 claim され得る。同じ行を 2 経路から送らないため、queue 側は送らず捨てる。状態遷移はしない（回収は sweep の責務）。
- **fresh select**: ticket 付き exit は送る直前に `BrokerPositionReader.position(ticket)`（`src/trading/oms/service.py:35-40`）で再取得し、無ければ NOOP（不変条件「Exit は裸の反対売買にしない／Protection 決済済みなら NOOP」を send 時点でも保つ。`OMSService.prepare_exit` `src/trading/oms/service.py:146` と同じ判断）。
- **revalidation は rate limit 通過後、record 前**: reject された注文の分まで窓を消費しない。`allows` と `record` の間で時刻は進まない（同じ `now`）。
- **reduced も reject**: Risk が `approved_quantity < quantity` を返すのは event mode が REDUCED に変わった等の「条件変化」そのもの（§32.4）。元の数量で送ると縮小後の budget を破るので取り下げ、strategy 側の再発行に任せる。exit の decision は `approved_quantity=None` なのでこの分岐に入らない。
- **SEND は `mark_submitting`（`src/trading/oms/claim.py:49`）のコピーを返す**: queue が「送ってよい」と判断した瞬間が CLAIMED → SUBMITTING。呼び出し元は `save_state(expected_state=CLAIMED)` で persist し（失敗 = `StaleCommandStateError` なら送らない）、`mark_broker_request_started` → `order_send` と進む（この後段は本 PR のスコープ外）。

### 1.5 設定

`config/base.yaml` の `broker:` に追加（L4-7 の直後）:

```yaml
broker:
  expected_account_mode: HEDGING
  magic_number: 260813
  deviation_points: 10
  # OANDA MT5 の実測 request 上限（設計書 v2.1 §32.2）。OMS の ExecutionQueue が
  # RateLimiter 経由で守る。1 秒窓の件数なので 1 未満は設定エラー。
  rate_limit:
    per_symbol_requests_per_second: 5
    market_entries_per_second: 1
```

`src/trading/config.py:23` `BrokerConfig` に `rate_limit: RateLimitConfig = RateLimitConfig()` を追加（`RateLimitConfig` は `trading.oms.rate_limit` から import。`RiskConfig` を `trading.risk.engine` から import している既存パターン L16 と同じ）。環境 overlay（`config/demo.yaml` 等）は `broker` を上書きしていないので変更不要。

---

## 2. 変更対象ファイル（網羅）

| 種別 | パス | 内容 |
| --- | --- | --- |
| 新規 | `src/trading/oms/rate_limit.py` | `RateLimitConfig`、`RateLimiter`（1.3） |
| 新規 | `src/trading/oms/queue.py` | `QueuePriority`、`priority_for`、`QueuedCommand`、`DispatchOutcome`、`Dispatch`、`Revalidator`、`ExecutionQueue`（1.2 / 1.4） |
| 新規 | `tests/unit/test_rate_limit.py` | limiter 単体（3.1） |
| 新規 | `tests/unit/test_oms_queue.py` | queue / dispatch / revalidation（3.2） |
| 新規 | `docs/adr/ADR-030-oms-priority-queue-and-send-time-revalidation.md` | 4 節 |
| 変更 | `src/trading/config.py` | `BrokerConfig.rate_limit` 追加 + import |
| 変更 | `config/base.yaml` | `broker.rate_limit` 追加 |
| 変更 | `tests/support.py` | `make_command` に `symbol: str = "USDJPY"` 引数を追加（既定値は変えない。既存呼び出しは無変更で通る） |
| 変更 | `tests/unit/test_config.py` | `rate_limit` の読み込みと `ge=1` 検証のテスト追加（3.3） |

**共有ファイルへの追記ルール（承認条件）**: `src/trading/config.py`・`config/base.yaml`・`tests/support.py`・`tests/unit/test_config.py` は並行する別 worktree も追記する。既存ブロックの並び・既存行は崩さず、自分の追加は**独立したブロック／関数として末尾寄りに足す**（`config.py` は `BrokerConfig` へのフィールド追加と import 1 行のみ。`base.yaml` は `broker:` ブロック内への追記のみ。`support.py` は `make_command` の引数追加のみ。`test_config.py` は新しいテスト関数をファイル末尾に追加）。

**触らないファイル（厳守）**: `src/trading/portfolio/**`、`src/trading/domain/signal.py`、`src/trading/live/shadow.py`、`src/trading/storage/**`、`migrations/**`（0008 は別 worktree が使う。本 PR は migration 無し）、`src/trading/oms/{service,claim,state_machine,reconciliation}.py`、`src/trading/risk/**`、`docs/SYSTEM_SPEC.md`（v1.3 凍結。変更は ADR で）、`docs/adr/ADR-029*`（別 worktree）。

---

## 3. テスト方針（pytest、`tests/support.py` のファクトリを使う）

共通の道具:

- 時刻: `tests/support.py:27` `FixedClock`（`advance(seconds=...)`）、`T0` (L20)、`at(**kwargs)` (L23)
- コマンド: `tests/support.py:410` `make_command(state=CommandState.CLAIMED, action=..., symbol=..., claim_expires_at=..., broker_position_ticket=...)`
- intent: `tests/support.py:379` `make_intent(action=..., direction=..., symbol=...)`
- broker: `tests/unit/test_netting_and_oms.py:26` `FakeBroker` と `:45` `short_position` を**参考に**、`test_oms_queue.py` 内に同等の小さな fake を定義する（他テストファイルからの import はしない）。この fake の `position(ticket)` は**既定でどの ticket にも `BrokerPosition` を返す**（`closed: set[str]` に入れた ticket だけ `None`）。ticket 付き CLOSE を送る各テストが ALREADY_CLOSED に落ちないようにするため
- ticket 付きの CLOSE コマンドは `make_command(state=CommandState.CLAIMED, action=PositionAction.CLOSE, broker_position_ticket="1001", symbol=...)` で作る。同一 symbol に複数並べるときは ticket を変える
- RiskDecision の fake: テスト内に `class ApproveAll` / `class RejectAll` 等、`revalidate(entry, now)` が固定の `RiskDecision(decision_id=uuid4(), intent_id=entry.intent.intent_id, approved=..., approved_quantity=..., reject_codes=[...], decided_at=now)` を返す最小クラスを置く。受け取った `now` と entry を記録して assert できるようにする
- 実 RiskEngine を使う revalidation テストは `tests/unit/test_risk_engine.py:24` `make_context` / `:48` `enabled_config` / `:61` `engine` と同じ組み立てを **test_oms_queue.py 内で再現**する（他テストファイルからの import はしない）
- 実在の人物・団体名を使わない（既存ファクトリの架空値のみ）

### 3.1 `tests/unit/test_rate_limit.py`

1. per-symbol 5 req/sec: 同一 symbol で `record` を 5 回（同時刻）→ 6 回目の `allows` は False。別 symbol は True。1.000 秒進めると True、0.999 秒では False。
2. entry 1/sec: `market_entry=True` を 1 回 record → 別 symbol でも `market_entry=True` は False、`market_entry=False`（exit）は True。1 秒後に True。
3. 窓は sliding: t=0 に 3 件、t=0.5 に 2 件 record → t=1.0 では 3 件が抜けて 2 件残り → `allows` True、t=1.0 に 3 件 record すると 5 件で False、t=1.5 で 2 件抜けて True。
4. `RateLimitConfig(per_symbol_requests_per_second=0)` / `market_entries_per_second=0` は `ValidationError`。

### 3.2 `tests/unit/test_oms_queue.py`（設計書 §34.7 の 5 項目 + 設計上の不変条件）

1. **exits prioritized**: NEW_ENTRY を 2 件 enqueue した後に CLOSE を enqueue → 最初の `dispatch()` が CLOSE。EMERGENCY（`priority_for(cmd, forced_risk_reduction=True)`）は CLOSE より先。`PROTECTION_REPAIR` を明示指定した entry は NEW_ENTRY より先・CLOSE より後。
2. **決定論**: rank 1..4 を付けた 4 symbol の entry を 2 通りの順序で enqueue した 2 つの queue が、同じ dispatch 列を返す。rank 無し同 priority は enqueue 順。
3. **entry 1/sec と Arbitrator 順位**（§32.3 の「4 ペア同時 new entry」）: USDJPY / EURUSD / GBPUSD / GBPJPY の entry に rank を付けて enqueue。t=0 の `dispatch()` は rank 1 が SEND、続けて呼ぶと None。1 秒進めて rank 2、以降同様に 4 件が 1 秒間隔で rank 順に出る。
4. **per-symbol 5 req/sec**: USDJPY の CLOSE（ticket 別）を 6 件 + EURUSD の CLOSE 1 件 enqueue。t=0 で dispatch を繰り返すと USDJPY 5 件 → EURUSD 1 件（6 件目の USDJPY は飛ばされる）→ None。1 秒進めると 6 件目の USDJPY が出る。
5. **queued entry expires**: `expires_at=at(seconds=2)` の entry を enqueue、3 秒進めて `dispatch()` → `EXPIRED`、`command.state is CommandState.EXPIRED`、queue から消える。limiter は消費されない（続く entry が同時刻に SEND できる）。revalidator は呼ばれない。
6. **queued entry revalidated（fake）**: reject する revalidator → `REVALIDATION_REJECTED`、`command.state is CANCELLED`、`decision.reject_codes` が伝わる、limiter 未消費、queue から消える。approve → `SEND`、`command.state is SUBMITTING`、`submitting_at == now`。revalidator に渡る `now` が enqueue 時刻ではなく dispatch 時刻（advance 後）であることを assert。
7. **queued entry revalidated（実 RiskEngine）**: revalidator が「現在の quote」を保持する holder から `PreTradeContext` を組み立てて `RiskEngine.evaluate(entry.intent, ctx)` を返す。enqueue 後に holder の quote を spread 拡大した tick（`make_tick("158.840", "158.900", time=now)`。USDJPY の pip_size 0.01 で 6 pips > ceiling 2.0）に差し替えて dispatch → `REVALIDATION_REJECTED` かつ `"SPREAD_ACCEPTABLE" in decision.reject_codes`。差し替え前（`make_tick("158.840", "158.844", time=now)`）なら `SEND`。
   - `PreTradeContext` の組み立ては `tests/unit/test_risk_engine.py:24-45` `make_context` の値をそのまま test_oms_queue.py 内のローカル関数に写す（`now` は dispatch 時刻、`quote` は holder の tick、`requested_quantity=entry.command.quantity`、`stop_distance_pips=Decimal(10)`、`instrument_trading_enabled=True`、`instrument=usdjpy_spec()`、`account=make_snapshot("1000000")`、`snapshots=[make_snapshot("1000000", observed_at=at(hours=-25))]`、`event_mode=NORMAL`、`kill_switch=NONE`、`unknown_commands=0`、各 count 0、`symbol_exposure_units=Decimal(0)`）。
   - `RiskEngine` は `tests/unit/test_risk_engine.py:48-68` と同じく `RiskConfig(trading_enabled=True, max_units_per_symbol={"USDJPY": 10000}, absolute_max_spread_pips={"USDJPY": Decimal("2.0")})` と `MarketQuoteConversionService(InMemoryMarketData(), [usdjpy_spec()])`、Clock は queue と同じ `FixedClock` を渡す。
   - quote の `time` は dispatch 時刻に合わせる（`QUOTE_FRESH` は `now - quote.known_time` が 0〜5 秒であることを要求する。`src/trading/risk/engine.py:198-210`）。
8. **reduced は reject**: approved=True かつ `approved_quantity < command.quantity` → `REVALIDATION_REJECTED`。
9. **ALREADY_CLOSED**: ticket 付き CLOSE を enqueue、fake broker が `position(ticket)` に None を返す → `ALREADY_CLOSED`、`CANCELLED`、revalidator は呼ばれない。position が在れば通常どおり SEND。
10. **LEASE_EXPIRED**: `claim_expires_at=at(seconds=30)` のコマンドで 31 秒進めて dispatch → `LEASE_EXPIRED`、`command.state is CLAIMED`（無変更）、queue から消える。
11. **enqueue 前提条件**: `state=READY` のコマンドは `ValueError`。同じ command を 2 回 enqueue すると `ValueError`。
12. **EMERGENCY も rate limit に従う**: 同 symbol で 5 件送った直後の EMERGENCY は同時刻では None（飛ばされて待つ）。1 秒後に出る。

### 3.3 `tests/unit/test_config.py`

- `load_config("demo")` で `config.broker.rate_limit.per_symbol_requests_per_second == 5`、`market_entries_per_second == 1`。
- `tests/unit/test_config.py:87` の parametrize パターンに倣い、`RateLimitConfig` の 0 / 負値が `ValidationError` になること（`AppConfig` 経由でなく `RateLimitConfig(...)` 直接でよい）。

### 3.4 回帰

- `tests/unit/test_invariants.py` は無変更で全件 pass（`strategy` / `intelligence` 配下に `trading.oms` を import しないこと。本 PR は両ディレクトリに触らない）
- `tests/unit/test_state_machine.py`・`tests/unit/test_netting_and_oms.py` は無変更で pass

---

## 4. ADR-030（`docs/adr/ADR-030-oms-priority-queue-and-send-time-revalidation.md`）

形式は `docs/adr/ADR-023-per-instrument-parameters-and-spread-session-gates.md` に合わせる（`# ADR-030: <日本語タイトル>` / `**Status:** Accepted (2026-09-02)` / `## Context` / `## Decision` / `## Consequences`。日本語）。内容:

- Context: 複数ペアの同時 signal で broker の request 上限（symbol 5/s・market entry 1/s）に当たる。queue 待機中に signal 失効・価格変動・spread 拡大・event mode 変化・conversion staleness・exposure 変化が起こる。DB claim は `created_at` 順の 1 行取りで優先順位を表現できない。
- Decision: (1) in-memory `ExecutionQueue` を DB claim の後・SUBMITTING の前に置き、CLAIMED のコマンドだけを載せる。(2) priority 5 段と決定論的順序（priority → Arbitrator rank → enqueue 連番）。(3) sliding window 2 段の rate limiter、値は `broker.rate_limit`。EMERGENCY も limit を迂回しない（broker 側で弾かれるだけ）。(4) send 直前に expiry → lease → ticket 付き exit の fresh select → pre-trade risk 再評価の順で確認し、送らない場合は `EXPIRED` / `CANCELLED` に遷移して decision を残す。reject は `REJECTED` ではなく `CANCELLED`（CLAIMED からの合法遷移。`REJECTED` は CREATED 時点の risk 拒否と broker 拒否に予約）。(5) state machine・migration は変更しない。
- Consequences: worker は claim した行を queue に載せ、`Dispatch.command` を `save_state(expected_state=CLAIMED)` で persist してから `order_send` に進む（M6 で配線）。revalidation の `PreTradeContext` 構築は配線側の責務（queue は Risk の中身を知らない）。netting exit（ticket 無し）の send 時再 delta は queue の対象外で、`OMSService.command_for_netting` の fresh `net_exposure` 読みに依存する（follow-up 候補）。`PROTECTION_REPAIR` / `TELEMETRY` は順位のみ定義し、producer は無い。

---

## 5. 完了条件（実行可能コマンド。すべて worktree 内で）

```bash
W=/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-64-oms-priority-queue
cd "$W" && .venv/bin/ruff check .                      # → All checks passed!（--fix 不要の状態にする）
cd "$W" && .venv/bin/pytest tests/unit -q               # → 全件 pass（ベースライン 802 + 新規）
cd "$W" && .venv/bin/pytest tests/unit/test_oms_queue.py tests/unit/test_rate_limit.py \
    tests/unit/test_config.py tests/unit/test_state_machine.py \
    tests/unit/test_netting_and_oms.py tests/unit/test_invariants.py -q
cd "$W" && git status --short                           # 変更が 2 節の一覧の範囲内であること
```

最後に「変更したファイル一覧」と「実行したテストとその結果」を出力する。

---

## 6. やらないこと（スコープ外。Codex への明示）

- Portfolio Arbitrator・`CandidateSignal`・`src/trading/portfolio/**`・`src/trading/domain/signal.py`・`src/trading/live/shadow.py`・`src/trading/storage/**`・`migrations/**`・ADR-029 には触らない（別 worktree が担当）
- worker ループ / live 配線 / `order_send` 呼び出し / DB persist は実装しない（M6）
- `src/trading/oms/{service,claim,state_machine,reconciliation}.py` と `src/trading/risk/**` は変更しない（revalidation は `RiskEngine.evaluate` を hook から呼ぶだけ）
- `execution_commands` への priority 列追加や claim SQL の ORDER BY 変更はしない
- 周辺リファクタ・無関係な整形・docstring の書き換え・追加の抽象化（基底クラス、ジェネリック queue、イベントバス等）はしない
- 既存テストを緩めない（特に `test_invariants.py` / `test_state_machine.py`）
- `docs/SYSTEM_SPEC.md` は編集しない（凍結）。`docs/PROJECT_STRUCTURE.md` も編集しない
- `.env` / 秘密情報 / DSN をコードや設定に書かない

---

## 7. 規約の転記（`.claude/rules/*.md` と `AGENTS.md` から。Codex は `AGENTS.md` 以外を読まない前提）

- 金額・数量・価格は `Decimal`（float 禁止）。本 PR で扱う `quantity` / `approved_quantity` は既に Decimal。比較も Decimal 同士で行う
- domain モデルは pydantic `frozen=True` + `model_copy(update=...)`。状態遷移は `trading.oms.state_machine.transition` / `trading.oms.claim.mark_submitting` を使い、`ExecutionCommand` を直接書き換えない。queue 内部の `list` の append / remove は queue 自身のローカル状態なので可
- `datetime.now()` を直接呼ばない。時刻は `Clock.now()`（`trading.backtest.clock.Clock` Protocol）から読む。limiter は `now` を引数で受ける
- 検証はシステム境界（設定ロード = `RateLimitConfig` の `Field(ge=1)`、queue の public API の前提条件）だけ。内部関数間に防御的分岐・フォールバックを足さない
- 通貨ペア・pip size・時間足をコードにハードコードしない（テストの symbol 文字列は可）。rate limit 値は config 経由
- テストは pytest。共有ファクトリは `tests/support.py`。実在の人物・団体名を使わない。テストは実装の写経ではなく「壊したら落ちる」assert にする
- コメントは WHY を書く。WHAT の説明コメント、「〜のために追加」のようなコミット文脈依存のコメント、AI レビュー引用は書かない。コメント・docstring は日本語優先（既存の英語 docstring は維持してよい）
- ファイルは 200〜400 行目安（上限 800）。`queue.py` が 400 行を超えそうなら `Dispatch` 系の型を分けるのではなく、docstring を削って本体を短くする方向で収める
- ruff（`pyproject.toml`: line-length 100、py311）に準拠。`from __future__ import annotations` を各モジュール先頭に置く（既存ファイルと同じ）
- SQL は書かない（本 PR に storage 変更は無い）
- コミットしない。`tmp/`・`tasks/APPROVAL.md`・`tasks/PARENT-NOTES.md` は触らない
