# issue #64（前半）: Portfolio Arbitrator — 同時 signal の選択を所有する

このファイルは Codex が会話履歴なしで自己完結に実装できるよう書いている。
`AGENTS.md` の規約が本ファイルより優先される。**OMS（`src/trading/oms/`）には触らない**
（issue #64 の後半「OMS priority queue / rate limiter」は別ブランチで進行中）。

## 0. 要件（issue #64 本文の該当部分を転記）

- `CandidateSignal`（strategy_id / symbol / direction / expected_edge_r / confidence /
  stop_distance_pips / generated_at / expires_at）。Strategy は raw candidate 生成まで。
- `PortfolioArbitrator`: 8-step 裁定（validity → pair gate → account currency risk →
  existing portfolio → currency/structural → dynamic redundancy → rank → greedy 再評価）。
  決定論（入力順序に非依存）。
- 重複 factor の初期 policy は strongest signal wins（`REJECTED_REDUNDANT_FACTOR_EXPOSURE`）。
  risk budget split は OOS / Monte Carlo で改善確認後のみ（本 PR ではやらない）。
- 係数は backtest で固定。LLM が live で変更しない。
- reject reason / candidate 記録の decision trail 拡張（`strategy_signals` /
  `position_intents` / `risk_decisions` は 0001 で作成・0006 で account スコープ済み。
  arbitration の列・表の追加は `migrations/0008_*.sql`）。
- テスト: 設計書 34.6（決定論 / duplicate USD factor / accept 後の limit 再計算 /
  expired reject / correlation penalty / triangle hard cap / existing portfolio alters ranking）。
- ADR: 「Portfolio Arbitrator が同時 signal 選択を所有」→ **ADR-029**（番号固定）。

設計書: `docs/research/2026-08-25-fx-multicurrency-system-design-v2.1.md`
§25「Portfolio Arbitrator」(L1303-)、§26「同時Signal Arbitration」、§27「重複Signalの初期Policy」、
§34.6 (L1879-)。関連 ADR: `docs/adr/ADR-011-*.md`、`ADR-013-*.md`（L37, L50 に「Arbitrator #64
が引き取る」）、`ADR-027-multi-symbol-shadow-cycle.md`（「arbitrator の差し込み前に全 candidate
を size する二段階構造」）。

## 1. 設計の確定事項（この方針で実装する。迷ったらここに戻る）

### 1.1 差し込み口と責務分担

- 差し込み口は `src/trading/live/shadow.py` の `ShadowRunner.evaluate_once`（L144-293）。
  **size（`self._portfolio.intents_from_signal` で `sized` を作る段、L246-270）と
  grade（`self._risk.evaluate`、L272-292）の境目**に Arbitrator を挟む。
- Arbitrator は **Risk を置き換えない・複製しない**。設計書 §26 Step 2〜5（pair gate /
  account currency risk / existing portfolio / currency-structural）は既に
  `RiskEngine.evaluate`（`src/trading/risk/engine.py:143-296`、`_portfolio_checks` L384-)
  と `CurrencyExposureService`（`src/trading/portfolio/exposure.py`）が持つ。
  Arbitrator の固有価値は次の 5 つだけ:
  1. **validity**: `expires_at <= now` → `REJECTED_EXPIRED`、`trading_enabled=False` →
     `REJECTED_TRADING_DISABLED`（ランク付けせず、factor も claim しない）
  2. **rank**: `priority = expected_edge_r × confidence − existing_exposure_penalty_r ×
     (候補の通貨 leg のうち、既存 book と同方向の leg の本数)`。降順 + 安定 tiebreak
  3. **redundancy（strongest wins）**: 通貨 leg `(currency, direction)` を「factor」とし、
     先に受理した候補が claim した leg を 1 本でも共有する候補は
     `REJECTED_REDUNDANT_FACTOR_EXPOSURE`
  4. **triangle hard cap**: triangle（3 ペアの通貨集合が 3 通貨で pairwise に 1 通貨を共有、
     例 GBPUSD / USDJPY / GBPJPY）を構成するペアのうち、既存 book + 当 cycle 受理分 + 候補
     自身で同時に持つ **distinct symbol 数** が `max_pairs_per_triangle` を超える候補は
     `REJECTED_TRIANGLE_CAP`
  5. **greedy 再評価**: 受理するたび running book（既存 + 受理済み候補の exposure）を伸ばし、
     各受理候補に「その候補を grade すべき book」`book_before` を持たせる。Risk は
     `book_before` から作った `PortfolioRiskSnapshot` と position 件数で grade するので、
     **既存の `PORTFOLIO_RISK_LIMIT` / `CURRENCY_EXPOSURE_LIMIT` /
     `MAX_OPEN_POSITIONS_*` が「accept 後の limit 再計算」になる**（新しい limit 計算は書かない）
- 受理候補どうしの ranking penalty は不要: 同方向 leg を共有する候補は 3 で reject されるので、
  priority は「既存 book に対して 1 回」計算すれば決定論的に同じ結果になる。
- **exit（`PositionAction.CLOSE`）は裁定を経ない**（risk 削減注文を止めない。設計書 §28
  「Exit / risk reducing order は entry flow とは別」）。先に既存 book で grade する。
- Arbitrator は clock を持たない。`select(..., now)` の `now` を expiry 判定と `decided_at`
  に使う（`datetime.now()` 禁止）。
- Arbitrator は conversion service に依存しない（book の leg 方向は units の符号だけで決まる。
  金額評価は Risk / `CurrencyExposureService` の仕事）。

### 1.2 shadow での trading_enabled は「as-if 有効」

shadow の instruments（`config/base.yaml`）は USDJPY 以外 `trading_enabled: false`。Arbitrator
が本物のフラグで reject すると EURUSD / GBPUSD / GBPJPY の signal が Risk に届かず、shadow の
目的（M6「USDJPY execute, 他 shadow」の証拠収集、ADR-012 / ADR-027）が壊れる。
よって **shadow runner は Arbitrator へ渡す候補を全て `trading_enabled=True` にする**
（backtest が `instrument_trading_enabled=True` を渡す `src/trading/backtest/engine.py:1078-1080`
と同じ理由）。Risk の `INSTRUMENT_TRADING_ENABLED` は従来どおり本物のフラグを報告する。

### 1.3 `CandidateSignal` の作り方（Strategy API の変更は最小）

- `StrategySignal`（`src/trading/domain/signal.py`）に **`expected_edge_r: Decimal =
  Field(default=Decimal(1), gt=0)`** を追加（既定 1R = 中立。strategy が推定を持つまで
  priority は confidence だけで決まる）。`Strategy.make_signal`
  （`src/trading/strategy/base.py:230-252`）に keyword `expected_edge_r: Decimal = Decimal(1)`
  を足して素通しする。3 strategy は変更しない。
- `expires_at` は Strategy から受け取らず、`CandidateSignal.from_signal` で
  `generated_at + timedelta(seconds=expected_horizon_seconds)` と定義する（signal はその
  horizon より長く有効ではない）。
- `confidence = Decimal(str(signal.conviction))`（conviction は float のまま）。

### 1.4 decision trail

- 新テーブル `arbitration_decisions`（migration `0008`）。1 行 = sized entry intent 1 件の裁定
  （受理・却下とも）。却下候補は `risk_decisions` 行を持たない。
- `DecisionRepository.record(...)` に `arbitration: ArbitrationDecision | None = None` を
  追加（受理候補: signal + intent + arbitration + risk decision を 1 トランザクション。
  exit は None）。`record_arbitration(account_id, signal, intent, arbitration)` を追加
  （却下候補: signal + intent + arbitration を 1 トランザクション）。
- 却下候補も trail に残す（PR #108 の「捨てると setup dedupe が消費される」問題の再発防止）。

## 1.5 並行ブランチとの衝突回避（必ず守る）

別ブランチが同時に `src/trading/config.py`・`config/base.yaml`・`tests/support.py`・
`tests/unit/test_config.py` に追記している。これらのファイルでは **既存ブロックの並びを崩さず、
自分の追加は独立したブロック／関数として末尾側に足す**（既存行の並べ替え・整形・改名をしない）。
`AppConfig` へのフィールド追加は `risk:` の直後の 1 行、`base.yaml` は `risk:` ブロック直後の
独立ブロック、`tests/support.py` は `FakeDecisionRepository` 内の変更だけ、`test_config.py` は
末尾に関数追加、とする。

## 2. 変更対象ファイル（網羅）

新規:
- `src/trading/domain/arbitration.py` — モデルと reason code 定数
- `src/trading/portfolio/arbitrator.py` — `ArbitratorConfig`、`PortfolioArbitrator`
- `migrations/0008_arbitration_decisions.sql`
- `docs/adr/ADR-029-portfolio-arbitrator-owns-signal-selection.md`
- `tests/unit/test_arbitrator.py`

変更:
- `src/trading/domain/signal.py` — `expected_edge_r`
- `src/trading/strategy/base.py` — `make_signal` の keyword 追加
- `src/trading/live/shadow.py` — Arbitrator の配線、`ShadowDecision`、`describe`、`main`
- `src/trading/storage/repository.py` — `DecisionRepository` Protocol
- `src/trading/storage/postgres.py` — `PostgresDecisionRepository`、`_row_to_signal`、`recent()`
- `src/trading/config.py` — `AppConfig.arbitrator`
- `config/base.yaml` — `arbitrator:` セクション
- `src/trading/backtest/engine.py` — `_portfolio_risk` docstring の 1 文（L1084-1088）のみ
- `docs/PROJECT_STRUCTURE.md` — Invariants（Structure）に 1 行
- `tests/support.py` — `FakeDecisionRepository`
- `tests/unit/test_shadow_runner.py` — `build()` に `risk_overrides` / `arbitrator` 配線、テスト追加
- `tests/unit/test_config.py` — arbitrator 係数の読み込みテスト
- `tests/integration/test_decision_repository.py` — arbitration 行と `expected_edge_r` の round trip

マイグレーション: **あり**（`0008_arbitration_decisions.sql`。0009 は別ブランチが使う）。

## 3. 実装仕様

### 3.1 `src/trading/domain/arbitration.py`（新規）

`src/trading/domain/exposure.py` / `domain/risk.py` と同じ流儀（pydantic frozen、docstring は
日本語可）。

```python
"""同時 signal 裁定のモデル（設計書 v2.1 §25–27、ADR-029）。

裁定を行う service は portfolio 層（`portfolio/arbitrator.py`）にあり、storage と
live runner は本モデルだけを読む。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading.domain.exposure import OpenPositionExposure
from trading.domain.position import PositionDirection
from trading.domain.signal import StrategySignal

ACCEPTED = "ACCEPTED"
REJECTED_EXPIRED = "REJECTED_EXPIRED"
REJECTED_TRADING_DISABLED = "REJECTED_TRADING_DISABLED"
REJECTED_REDUNDANT_FACTOR_EXPOSURE = "REJECTED_REDUNDANT_FACTOR_EXPOSURE"
REJECTED_TRIANGLE_CAP = "REJECTED_TRIANGLE_CAP"


class CandidateSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: UUID
    strategy_id: str
    symbol: str
    position_direction: PositionDirection
    expected_edge_r: Decimal
    confidence: Decimal
    stop_distance_pips: Decimal
    generated_at: datetime
    expires_at: datetime

    @classmethod
    def from_signal(cls, signal: StrategySignal) -> CandidateSignal:
        # signal はその horizon より長く有効ではない。
        return cls(
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            position_direction=signal.desired_direction,
            expected_edge_r=signal.expected_edge_r,
            confidence=Decimal(str(signal.conviction)),
            stop_distance_pips=signal.stop_distance_pips,
            generated_at=signal.generated_at,
            expires_at=signal.generated_at
            + timedelta(seconds=signal.expected_horizon_seconds),
        )


class ArbitrationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal: CandidateSignal
    # 受理されたとき book に加わる exposure。sized intent の数量（±target_quantity、
    # 未 size なら 0）・entry 価格（LONG=ask / SHORT=bid）・stop を provider が詰める。
    exposure: OpenPositionExposure
    trading_enabled: bool = True


class ArbitrationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    arbitration_id: UUID
    signal_id: UUID
    accepted: bool
    reason_code: str
    # validity で落ちた候補は rank / priority を持たない。
    rank: int | None
    priority: Decimal | None
    detail: str | None = None
    decided_at: datetime
    # 受理候補を Risk が grade する book（既存 + 先に受理した候補）。永続化しない。
    book_before: tuple[OpenPositionExposure, ...] = ()


class ArbitrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    # priority 順（rank 昇順）。
    accepted: tuple[ArbitrationDecision, ...]
    # rank 昇順、rank 無し（validity 却下）は末尾。同順位は tiebreak 順。
    rejected: tuple[ArbitrationDecision, ...]
```

### 3.2 `src/trading/portfolio/arbitrator.py`（新規）

```python
"""Portfolio Arbitrator: 同時 signal の選択（設計書 v2.1 §25–27、ADR-029）。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from trading.domain.arbitration import (...)
from trading.domain.exposure import OpenPositionExposure
from trading.domain.instrument import InstrumentSpec
from trading.domain.money import Currency
from trading.domain.position import PositionDirection


class ArbitratorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # 係数は backtest で校正して固定する（設計書 §26 Step 7）。LLM / runtime から変更
    # しない。現在値は校正前の仮置き（ADR-013 の数値と同じ扱い）。
    # 既存 book と同方向の通貨 leg 1 本あたり、priority（edge_r × confidence）から引く量。
    existing_exposure_penalty_r: Decimal = Decimal("0.10")
    # triangle を構成する 3 ペアのうち同時に保有してよい distinct symbol 数。
    # 2 = 三辺を同時には持たない。
    max_pairs_per_triangle: int = 2


Leg = tuple[Currency, PositionDirection]


class PortfolioArbitrator:
    def __init__(self, config: ArbitratorConfig) -> None: ...

    def select(
        self,
        candidates: Sequence[ArbitrationCandidate],
        book: Sequence[OpenPositionExposure],
        now: datetime,
    ) -> ArbitrationResult: ...
```

（instruments の一覧は受け取らない。triangle の判定に効くのは book か候補に現れている
pair だけなので、spec は `book` と `candidates` の exposure から集める。）

`select` のアルゴリズム（この順・この規則で。全て Decimal、float 不可）:

1. **validity**: `signal.expires_at <= now` → `REJECTED_EXPIRED`（detail 例
   `expires_at=<iso> now=<iso>`）、`not trading_enabled` → `REJECTED_TRADING_DISABLED`
   （detail `instrument policy does not allow trading <symbol>`）。いずれも `rank=None`、
   `priority=None`。残りを valid とする。
2. **既存 book の leg 方向** `_book_leg_directions(book) -> dict[Currency, PositionDirection]`:
   通貨ごとに `net[base] += signed_units`、`net[quote] -= signed_units * mark_price` を集計し、
   正なら LONG・負なら SHORT・0 なら無し（金額換算はしない。符号だけ）。
3. **候補の legs** `_legs(spec, direction) -> frozenset[Leg]`: LONG → `{(base, LONG),
   (quote, SHORT)}`、SHORT → `{(base, SHORT), (quote, LONG)}`。spec は
   `candidate.exposure.spec`、direction は `candidate.signal.position_direction`
   （exposure の符号ではなく signal の方向。未 size で units=0 の候補も leg を持つ）。
4. **priority** = `signal.expected_edge_r * signal.confidence -
   config.existing_exposure_penalty_r * overlap`、`overlap` = 候補 legs のうち 2 の
   book 方向と一致する本数（0〜2）。
5. **並び順**: key `(-priority, strategy_id, symbol, str(signal_id))` 昇順。入力順序に依存しない。
6. **greedy**: `claimed: dict[Leg, CandidateSignal]`、`book_now = list(book)`、
   `triangles = _triangles(specs)`（`specs` は book と候補の `exposure.spec` を symbol で
   重複排除した dict）を持ち、並び順に `rank = 1, 2, ...` を振りながら:
   - `taken = sorted(leg for leg in legs if leg in claimed)` が非空 → 
     `REJECTED_REDUNDANT_FACTOR_EXPOSURE`。detail は先頭 leg について
     `f"{currency} {direction} already taken by {winner.strategy_id}/{winner.symbol}"`。
   - triangle 判定: 候補 symbol を含む各 triangle について
     `held = (triangle & {e.spec.symbol for e in book_now}) | {symbol}`、
     `len(held) > config.max_pairs_per_triangle` なら `REJECTED_TRIANGLE_CAP`
     （detail `f"triangle {'/'.join(sorted(triangle))} held={sorted(held)} cap={cap}"`）。
   - それ以外は受理: `ArbitrationDecision(accepted=True, reason_code=ACCEPTED, rank, priority,
     book_before=tuple(book_now))` を作り、`book_now.append(candidate.exposure)`、
     `claimed` に legs を登録。
   - 却下候補は `rank` と `priority` を持つ（何番目に検討されたかは trail の情報）。
7. `_triangles(specs: Mapping[str, InstrumentSpec]) -> list[frozenset[str]]`:
   `combinations(specs.values(), 3)` のうち、3 spec の通貨集合の和が 3 通貨で、かつ任意の
   2 spec が共有する通貨がちょうど 1 の組。pair 名で決めず通貨から導く（ADR-013 の方針）。
   platform の 4 ペアでは `{GBPUSD, USDJPY, GBPJPY}` だけが該当する。
8. `arbitration_id = uuid4()`、`decided_at = now`。
9. 戻り値: `accepted` は rank 昇順、`rejected` は `(rank is None, rank or 0, strategy_id,
   symbol, str(signal_id))` でソート。

### 3.3 `src/trading/domain/signal.py` / `src/trading/strategy/base.py`

- `StrategySignal` に `expected_edge_r: Decimal = Field(default=Decimal(1), gt=0)` を
  `conviction` の直後に追加。コメント: 「期待 edge（R 倍数）。strategy が推定を持つまでは
  1R = 中立で、裁定の priority は confidence だけで決まる」。
- `Strategy.make_signal(... , expected_edge_r: Decimal = Decimal(1))` を追加し
  `StrategySignal(expected_edge_r=expected_edge_r, ...)` に渡す。他の引数・呼び出し元は変更しない。

### 3.4 `src/trading/live/shadow.py`

- import 追加: `from trading.domain.arbitration import ArbitrationCandidate, ArbitrationDecision,
  CandidateSignal`、既存の `from trading.domain.position import PositionDirection` に
  `PositionAction` を足す、`from trading.portfolio.arbitrator import PortfolioArbitrator`、
  既存の `from uuid import uuid4` に `UUID` を足す。
- `ShadowDecision` を次にする（既存フィールドの順を保つ）:
  ```python
  @dataclass(frozen=True)
  class ShadowDecision:
      signal: StrategySignal
      intent: PositionIntent
      # Risk の grade。Arbitrator が退けた候補は Risk に届かないため None。
      decision: RiskDecision | None
      # entry 候補の裁定。exit（CLOSE）は裁定を経ないため None。
      arbitration: ArbitrationDecision | None = None
  ```
- `ShadowRunner.__init__` に keyword-only 引数 `arbitrator: PortfolioArbitrator` を追加
  （`exposure` の直後、`features` の前）。`self._arbitrator` に保持。
- `evaluate_once` の書き換え（L228-293）。仮想 book の組み立て（L228-243）は
  `base_book: list[OpenPositionExposure]` として変数に出し、`portfolio_risk` の即時計算は
  やめる（grade 時に book から作る）。sizing ループ（L245-270）は変えない。その後:
  ```python
  exits: list[tuple[CollectedSignal, PositionIntent]] = []
  entries: dict[UUID, tuple[CollectedSignal, PositionIntent]] = {}
  candidates: list[ArbitrationCandidate] = []
  for item, intent in sized:
      if intent.action is PositionAction.CLOSE:
          exits.append((item, intent))
          continue
      entries[item.signal.signal_id] = (item, intent)
      candidates.append(self._candidate(item.signal, intent, quotes[intent.symbol]))
  arbitration = self._arbitrator.select(candidates, base_book, now)

  # 並び: exit → 受理候補（rank 順）→ 却下候補（rank 順）。
  results: list[ShadowDecision] = []
  # exit は risk 削減なので裁定を経ず、既存 book で grade する。
  for item, intent in exits:
      decision = self._grade(item, intent, quotes[intent.symbol], account, history, now, base_book)
      self._decisions.record(self._account_id, item.signal, intent, decision)
      results.append(ShadowDecision(signal=item.signal, intent=intent, decision=decision))
  # 受理候補は priority 順に、先に受理した候補を含む book で grade する — これが
  # accept ごとの limit 再計算（PORTFOLIO_RISK_LIMIT / CURRENCY_EXPOSURE_LIMIT /
  # MAX_OPEN_POSITIONS_*）になる。
  for verdict in arbitration.accepted:
      item, intent = entries[verdict.signal_id]
      decision = self._grade(item, intent, quotes[intent.symbol], account, history, now, verdict.book_before)
      self._decisions.record(self._account_id, item.signal, intent, decision, arbitration=verdict)
      results.append(ShadowDecision(signal=item.signal, intent=intent, decision=decision, arbitration=verdict))
  # 却下候補は Risk に届かないが trail には残す（捨てると setup の dedupe だけが消費される）。
  for verdict in arbitration.rejected:
      item, intent = entries[verdict.signal_id]
      self._decisions.record_arbitration(self._account_id, item.signal, intent, verdict)
      results.append(ShadowDecision(signal=item.signal, intent=intent, decision=None, arbitration=verdict))
  return ShadowCycle(at=now, decisions=tuple(results), blocked=blocked)
  ```
- `_candidate(self, signal, intent, quote) -> ArbitrationCandidate`:
  `entry_price = quote.ask if LONG else quote.bid`、`quantity = intent.target_quantity or Decimal(0)`、
  `signed_units = quantity if LONG else -quantity`、
  `stop = intent.protection.stop_loss_price if intent.protection else None`、
  `OpenPositionExposure(spec=self._instruments[signal.symbol].spec, signed_units=..., mark_price=entry_price, stop_loss_price=stop)`、
  `ArbitrationCandidate(signal=CandidateSignal.from_signal(signal), exposure=..., trading_enabled=True)`。
  `trading_enabled=True` の理由コメント（§1.2）を付ける。
- `_grade(self, item, intent, quote, account, history, now, book) -> RiskDecision`:
  `portfolio_risk = self._exposure.snapshot(book, now)`、`context = self._pretrade_context(...)`、
  `return self._risk.evaluate(intent, context)`。
- `_pretrade_context` の引数を `(signal, intent, quote, account, history, now, horizon, book, portfolio_risk)`
  にし、position 件数と exposure を **ledger ではなく book から** 導く:
  `symbol_open_positions_count = sum(1 for e in book if e.spec.symbol == symbol)`、
  `portfolio_open_positions_count = len(book)`、
  `symbol_exposure_units = sum((e.signed_units for e in book if e.spec.symbol == symbol), Decimal(0))`。
  既存コメント（「virtual ledger は ... zero」）は「book は ledger の仮想 position（fill は
  届かないため通常空）と当 cycle で先に受理した候補」の趣旨に書き換える。
- `describe(result)`: `result.decision is None` のとき
  `f"{arbitration.decided_at.isoformat()} {strategy_id} {symbol} {action} {direction} qty={target_quantity} ARBITRATED_OUT {arbitration.reason_code}"`。
  それ以外は従来の文字列に、`arbitration` があれば `rank={rank}` を `APPROVED/REJECTED` の
  直後に足す。
- `main()`: `ShadowRunner(..., exposure=CurrencyExposureService(conversion),
  arbitrator=PortfolioArbitrator(config.arbitrator), features=features)`。
- module docstring の 2 段落目「Strategies evaluate ..., Portfolio sizes ..., Risk grades」に
  「the Arbitrator picks which sized entries are graded, in priority order」の一文を足す。

### 3.5 `src/trading/storage/repository.py`

`DecisionRepository` に:
```python
    def record(
        self,
        account_id: str,
        signal: StrategySignal,
        intent: PositionIntent,
        decision: RiskDecision,
        arbitration: ArbitrationDecision | None = None,
    ) -> None: ...

    # Arbitrator が退けた entry 候補: Risk には届かないので risk_decisions 行を持たない。
    # signal / intent / arbitration を 1 トランザクションで書く。
    def record_arbitration(
        self,
        account_id: str,
        signal: StrategySignal,
        intent: PositionIntent,
        arbitration: ArbitrationDecision,
    ) -> None: ...
```
`record` の既存コメントに「entry 候補は裁定結果を伴う（exit は None）」を足す。
import: `from trading.domain.arbitration import ArbitrationDecision`。

### 3.6 `src/trading/storage/postgres.py`

- `_row_to_signal`（L1034）に `expected_edge_r=row["expected_edge_r"]`。
- `PostgresDecisionRepository.record`（L1093）: 既存の intent INSERT（L1103-1140）を
  `_insert_intent(account_id, signal, intent)` に切り出す（SQL は変えない）。
  シグネチャに `arbitration: ArbitrationDecision | None = None` を追加し、intent INSERT の後、
  risk_decisions INSERT の前に `if arbitration is not None: self._insert_arbitration(account_id, intent, arbitration)`。
- `record_arbitration`: `_insert_signal` → `_insert_intent` → `_insert_arbitration` → `commit()`。
- `_insert_arbitration(self, account_id, intent, arbitration)`:
  ```sql
  INSERT INTO arbitration_decisions (
      id, account_id, intent_id, accepted, reason_code, rank, priority, detail, decided_at
  ) VALUES (
      %(id)s, %(account_id)s, %(intent_id)s, %(accepted)s, %(reason_code)s,
      %(rank)s, %(priority)s, %(detail)s, %(decided_at)s
  )
  ```
  （プレースホルダ必須。文字列連結禁止。`book_before` は書かない。）
- `_insert_signal`（L1164）: 列 `expected_edge_r` と `%(expected_edge_r)s` を追加。
- `recent()`（L1195-）: SELECT に `s.expected_edge_r,` を追加（`s.conviction` の直後）。
  戻り値の形（3-tuple）は変えない。

### 3.7 `migrations/0008_arbitration_decisions.sql`（新規。0001〜0007 は書き換えない）

```sql
-- 0008_arbitration_decisions.sql
-- Portfolio Arbitrator の裁定記録（ADR-029）。sized entry intent 1 件につき 1 行で、
-- 受理・却下の別と reason code、priority 順位を残す。却下された候補は Risk に届かない
-- ため risk_decisions 行を持たず、この表だけが「なぜ grade されなかったか」を語る。
-- strategy_signals.expected_edge_r は候補の期待 edge（R 倍数）。strategy が推定を
-- 持つまでは 1R = 中立で、既存行もその意味で backfill される。

BEGIN;

ALTER TABLE strategy_signals ADD COLUMN expected_edge_r NUMERIC NOT NULL DEFAULT 1;

CREATE TABLE arbitration_decisions (
    id           UUID PRIMARY KEY,
    account_id   TEXT NOT NULL,
    intent_id    UUID NOT NULL UNIQUE REFERENCES position_intents (id),
    accepted     BOOLEAN NOT NULL,
    reason_code  TEXT NOT NULL,
    -- validity（expiry / trading 不可）で落ちた候補は順位を持たない。
    rank         INTEGER,
    priority     NUMERIC,
    detail       TEXT,
    decided_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_arbitration_account_decided
    ON arbitration_decisions (account_id, decided_at DESC);

COMMIT;
```

### 3.8 `src/trading/config.py` / `config/base.yaml`

- `from trading.portfolio.arbitrator import ArbitratorConfig`、
  `AppConfig.arbitrator: ArbitratorConfig = ArbitratorConfig()`（`risk` の直後）。
- `config/base.yaml` の `risk:` ブロックの直後に:
  ```yaml
  arbitrator:
    # 同時 signal の裁定係数（設計書 v2.1 §26 Step 7、ADR-029）。backtest で校正して
    # 固定する値で、LLM や runtime から変更しない。現在値は校正前の仮置き。
    # 既存 book と同方向の通貨 leg 1 本あたり、priority（edge_r × confidence）から引く量。
    existing_exposure_penalty_r: 0.10
    # triangle（GBPJPY ≒ GBPUSD × USDJPY）を構成する 3 ペアのうち同時に保有してよい
    # pair 数。2 = 三辺を同時には持たない。
    max_pairs_per_triangle: 2
  ```
- 他の環境 overlay（`config/*.yaml`）は変更しない。

### 3.9 `src/trading/backtest/engine.py`

`_portfolio_risk` の docstring（L1084-1088）の後半を次に差し替える（コードは変えない）:
「in-flight（pending）の entry はまだ book に無く、その stop-risk は合計に含まれない —
逐次処理の近似。同時 signal の裁定は Portfolio Arbitrator（ADR-029）が shadow / live の
経路で担い、単一銘柄の backtest には配線していない。」

### 3.10 `docs/PROJECT_STRUCTURE.md`

「Invariants（Structure）」のコードブロック内、
`Portfolio Manager owns cross-strategy exposure aggregation.` の直後に
`Portfolio Arbitrator owns simultaneous-signal selection.` を 1 行追加。

### 3.11 `docs/adr/ADR-029-portfolio-arbitrator-owns-signal-selection.md`（新規）

`docs/adr/ADR-027-multi-symbol-shadow-cycle.md` と同じ体裁（見出し `# ADR-029: ...`、
`**Status:** Accepted (2026-09-02)`、`## Context` / `## Decision` / `## Consequences`）。
日本語。内容は本ファイル §1 の確定事項を ADR の文体で書く。必ず含める点:
- Arbitrator は size と grade の間に入り、Risk を置き換えず、Risk の limit 計算を
  「受理済み候補を含む book」で呼ぶことで greedy 再評価を実現する
- 重複 factor の初期 policy は strongest signal wins（leg `(currency, direction)` 単位）。
  risk budget split は OOS / portfolio backtest / Monte Carlo で改善確認後のみ
- priority 式と係数（`existing_exposure_penalty_r`、`max_pairs_per_triangle`）は config に
  固定し backtest で校正する。LLM / runtime は変更しない。現在値は仮置き
- triangle は通貨から導出し、cap は distinct symbol 数
- exit は裁定を経ない
- shadow は trading_enabled を as-if 有効で渡す（理由: §1.2）
- 決定論（入力順序非依存、tiebreak）
- `expected_edge_r` は StrategySignal の既定 1R。`expires_at` は horizon から導出
- greedy 再評価の book は「Arbitrator が受理した候補」を含める（Risk が承認した候補ではない）。
  理由: shadow は `risk.trading_enabled=false` で全候補が Risk reject になるため、Risk 承認起点
  では再評価が一度も観測できない。**副作用として live では、Risk が rank 1 を落とした cycle
  でも rank 2 は rank 1 を含む book で評価される（fail-close 側の保守的評価）。** これを
  Consequences に明記する
- Consequences: backtest（単一銘柄）には配線しない／rolling correlation に基づく dynamic
  redundancy（設計書 §26 Step 6）は returns series の provider が無いため未実装で、
  既存 book との構造的 overlap penalty で代替／OMS priority queue は別 PR／
  `arbitration_decisions` 表と `recent()` の関係（recent は従来どおり risk decision 起点）／
  上記の live での fail-close 副作用

### 3.12 テスト

#### `tests/support.py`

- `FakeDecisionRepository` に `self.arbitrations: list[tuple[str, StrategySignal, PositionIntent, ArbitrationDecision]] = []`。
  `record(..., arbitration=None)` は従来どおり `trails` に 4-tuple を追加し、`arbitration` が
  あれば `arbitrations` にも追加。`record_arbitration` は `record_signal` を呼んでから
  `arbitrations` に追加（`trails` には入れない）。既存テストの `store.trails == [...]` を壊さない。

#### `tests/unit/test_arbitrator.py`（新規）

`tests/support.py` の `usdjpy_spec / eurusd_spec / gbpusd_spec / gbpjpy_spec`、`T0`、`at` を使う。
候補は helper `candidate(spec, direction, confidence, *, strategy_id="test_strategy",
units="1000", price, stop=None, generated_at=T0, expected_horizon_seconds=300, trading_enabled=True)`
で作る（`CandidateSignal` を直接組む。`signal_id=uuid4()`）。既存 book は
`OpenPositionExposure(spec, signed_units, mark_price, stop_loss_price)`。`now = T0`。
設計書 34.6 との対応:

1. **決定論**: 5 候補（重複 factor 混在）を `random.Random(seed)` で 3 通りに shuffle しても、
   `[(d.signal_id, d.rank, d.reason_code) for d in accepted + rejected]` が同一。
2. **duplicate USD factor selects strongest**: EURUSD SHORT (0.8) / GBPUSD SHORT (0.6) /
   USDJPY LONG (0.7) → EURUSD だけ受理（rank 1）。他 2 件は
   `REJECTED_REDUNDANT_FACTOR_EXPOSURE`、rank 2・3、detail に `USD LONG` と
   `test_strategy/EURUSD` を含む。
3. **leg を共有しない候補は全て受理**: EURUSD LONG / USDJPY LONG → 両方受理、rank 1・2、
   rank 2 の `book_before` に rank 1 の exposure が含まれ、rank 1 の `book_before` は空。
   （= risk limits recomputed after each accept の Arbitrator 側）
4. **expired candidate rejected**: `expected_horizon_seconds=300` で `now = at(minutes=6)` →
   `REJECTED_EXPIRED`、`rank is None`、`priority is None`。
5. **trading 不可の候補は factor を claim しない**: EURUSD SHORT (0.9, trading_enabled=False) と
   USDJPY LONG (0.5) → EURUSD は `REJECTED_TRADING_DISABLED`、USDJPY は受理 rank 1。
6. **existing portfolio alters ranking**（correlation / overlap penalty）: book に USDJPY LONG。
   候補 EURUSD SHORT (0.7、USD LONG が book と同方向) と GBPJPY SHORT (0.65、重なりなし) →
   GBPJPY が rank 1（priority 0.65）、EURUSD が rank 2（priority 0.60）。両方受理。
7. **penalty は同方向 leg だけに掛かる**: book に USDJPY LONG、候補 EURUSD LONG (0.7、USD SHORT は
   逆方向) → priority 0.7 のまま。
8. **triangle hard cap**: book に GBPUSD LONG と USDJPY SHORT。
   候補 GBPJPY LONG → `REJECTED_TRIANGLE_CAP`、detail に `GBPJPY/GBPUSD/USDJPY`。
   `ArbitratorConfig(max_pairs_per_triangle=3)` なら受理。
9. **triangle は当 cycle の受理分も数える**: book 空、候補 GBPUSD LONG (0.9) / USDJPY LONG (0.8) /
   GBPJPY SHORT (0.7)。leg は GBP+ USD− / USD+ JPY− / GBP− JPY+ で重複しないため redundancy では
   落ちない → 先の 2 件は受理、GBPJPY は `REJECTED_TRIANGLE_CAP`（rank 3）。
10. **priority と tiebreak**: 同 priority なら `(strategy_id, symbol, signal_id)` 昇順で rank が決まる。

#### `tests/unit/test_shadow_runner.py`

- `build()` に `risk_overrides: dict | None = None`（`RiskConfig(trading_enabled=...,
  event_mode_default=..., **(risk_overrides or {}))`）と `arbitrator=None`
  （既定 `PortfolioArbitrator(ArbitratorConfig())`）を追加し、`ShadowRunner(..., arbitrator=...)` に渡す。
- 追加テスト:
  a. **accept ごとに portfolio stop-risk が再計算される**: `two_pairs()` +
     `risk_overrides={"portfolio_stop_risk_budget_pct": Decimal("0.08"),
     "max_units_per_symbol": {"USDJPY": 100000, "EURUSD": 100000}}`。
     equity 1,000,000: USDJPY SHORT は 10,000 units × 0.05 JPY = 500 JPY、EURUSD SHORT は
     6,000 units × 0.0005 USD ≒ 476 JPY。予算 800 JPY。priority は同点（0.7）で symbol 順に
     EURUSD が rank 1 → `"PORTFOLIO_RISK_LIMIT" not in results["EURUSD"].decision.reject_codes`、
     `"PORTFOLIO_RISK_LIMIT" in results["USDJPY"].decision.reject_codes`、
     `results["USDJPY"].arbitration.rank == 2`。
     （`max_units_per_symbol` が無いと `_size_check` が `SYMBOL_LIMIT_CONFIGURED` で早期 return
     して `_portfolio_checks` に到達しないため必須。）
  b. **accept ごとに position 件数が再計算される**: 同じ 2 pair で
     `risk_overrides={"max_open_positions_portfolio": 1, "max_units_per_symbol": {...}}` →
     rank 2 の USDJPY だけ `MAX_OPEN_POSITIONS_PORTFOLIO`。
  c. **同一 cycle の重複 factor は strongest wins で trail に残る**: 新 strategy
     `RedundantSignallingStrategy`（USDJPY SHORT conviction 0.7 と EURUSD LONG conviction 0.9 を
     返す。USD SHORT を共有）。`FakeDecisionRepository` を渡し、EURUSD は
     `decision is not None`・`arbitration.accepted`、USDJPY は `decision is None`・
     `arbitration.reason_code == REJECTED_REDUNDANT_FACTOR_EXPOSURE`。
     `store.trails` は EURUSD の 1 件、`store.arbitrations` は 2 件、`store.signals` は 2 件。
  d. **exit は裁定を経ない**: `build()` 後に `ledger.record(VirtualPosition(...))` で
     `test_signaller` の USDJPY LONG 1,000 units を仮想 book に入れる（`tests/unit/test_virtual_ledger.py`
     の `snapshot()` helper を参考に。`build()` から ledger を取り出せるよう、`build()` の戻り値は
     変えずに `runner._ledger` を使ってよい）。SHORT signal で CLOSE + OPEN が出る。CLOSE は
     `arbitration is None` かつ `decision is not None`、OPEN は `arbitration is not None`。
  e. **裁定結果が trail と一緒に記録される**: `quote_and_account()` の 1 signal で
     `store.trails` 1 件、`store.arbitrations` 1 件（`accepted=True, rank=1`）。
  f. `describe()` が却下候補で `ARBITRATED_OUT` と reason code を含む。
- 既存テストは緩めない。`test_every_graded_decision_is_recorded` の
  `store.trails == [(ACCOUNT, result.signal, result.intent, result.decision)]` はそのまま通ること。

#### `tests/unit/test_config.py`

`test_base_config_fixes_arbitrator_coefficients`: 既存テストと同じく
`load_config("shadow", CONFIG_DIR)` で読み、
`config.arbitrator.existing_exposure_penalty_r == Decimal("0.10")`、
`config.arbitrator.max_pairs_per_triangle == 2`（YAML の float は pydantic が Decimal に
変換する。既存の `max_risk_per_trade_pct: 0.05` と同じ経路）。

#### `tests/integration/test_decision_repository.py`（DB が無い環境では自動 skip）

- fixture の cleanup に `DELETE FROM arbitration_decisions WHERE intent_id IN (SELECT id FROM
  position_intents WHERE strategy_id = %s)` を risk_decisions の DELETE の前に追加。
- `test_an_arbitration_verdict_is_written_with_the_trail`: `record(..., arbitration=verdict)` 後、
  `SELECT accepted, reason_code, rank, priority, detail FROM arbitration_decisions WHERE intent_id = %s`
  が round trip（priority は `Decimal("0.7")`）。
- `test_a_candidate_the_arbitrator_rejected_has_no_risk_decision`: `record_arbitration` 後、
  `risk_decisions` に行が無く、`arbitration_decisions` に `accepted = false` の行がある。
- `test_expected_edge_r_round_trips`: `expected_edge_r=Decimal("1.5")` の signal が
  `recent()` で同じ値で戻る。

## 4. 完了条件（実行可能コマンド）

`W=/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-64-portfolio-arbitrator`

- `cd "$W" && .venv/bin/ruff check .` が無変更で通る（line-length 100）
- `cd "$W" && .venv/bin/pytest tests/unit -q` が全 green。特に:
  - `tests/unit/test_arbitrator.py`
  - `tests/unit/test_shadow_runner.py`
  - `tests/unit/test_config.py`
  - `tests/unit/test_invariants.py`（変更しない。通ること）
  - `tests/unit/test_risk_engine.py`、`tests/unit/test_portfolio_manager.py`、
    `tests/unit/test_strategy_dedupe.py`、`tests/unit/test_live_wiring.py`（回帰）
- `cd "$W" && .venv/bin/pytest tests/replay tests/failure -q` が green
- `tests/integration` は `TRADING_DB_DSN` 未設定なら skip でよい（Claude 側で使い捨て DB が
  用意できれば実行する）

## 5. やらないこと

- `src/trading/oms/` は一切触らない（priority queue / rate limiter は別ブランチ）
- risk budget split（重複 factor の複数保有）は実装しない
- rolling correlation / covariance に基づく dynamic redundancy（設計書 §26 Step 6）は実装しない
- Risk の limit 計算を Arbitrator に複製しない。`RiskEngine` / `CurrencyExposureService` の
  ロジックは変更しない
- backtest engine への配線はしない（docstring の 1 文のみ）
- 3 strategy（`src/trading/strategy/**`）は変更しない
- `recent()` の戻り値の形は変えない
- 既存 migration（0001〜0007）を書き換えない
- 周辺リファクタ・無関係な整形・追加の抽象化・feature flag・後方互換シムを足さない
- 既存テストを通すためにテスト側を緩めない（`test_invariants.py` は特に）
- コミットしない（Claude 側が行う）

## 6. 適用順（運用メモ。Codex の作業対象ではない）

- Mac と VPS の DB は独立しているので、**両方に `0008` を適用してから**新しい shadow runner を
  起動する。新コードは `strategy_signals.expected_edge_r` 列と `arbitration_decisions` 表に
  INSERT するため、未適用 DB では最初の cycle で失敗する。
- 旧コードは新スキーマ上で動く（列は `DEFAULT 1`、新テーブルは未使用）ので、適用 → 再起動の
  順なら停止時間は不要。
- `ALTER TABLE ... ADD COLUMN ... DEFAULT 1` は PostgreSQL 11 以降ではメタデータ更新のみで、
  稼働中の runner は lock 解放を待つだけ（0006 のような DELETE を伴わない）。

## 7. 規約の転記（`.claude/rules/*.md` / グローバル規約に依存する分）

- 金額・数量・価格・priority は **Decimal**（float は indicator 計算のみ）。`conviction` は既存の
  float のまま `Decimal(str(...))` で受ける
- モデルは **pydantic frozen**（`model_config = ConfigDict(frozen=True)`）。引数や共有オブジェクトを
  破壊しない（`book` は list にコピーしてから伸ばす）
- Arbitrator / Strategy で **`datetime.now()` を呼ばない**（`now` は引数で受ける）。
  `tests/unit/test_invariants.py` が `strategy/` `intelligence/` 配下の `datetime.now(` /
  `trading.storage` / `trading.oms` / `trading.execution` 参照を禁止している
- Strategy / LLM 層から Broker・OMS・DB へ到達させない。`StrategyContext` にフィールドを足さない
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない。裁定は PositionDirection のみ扱う
- SQL は **プレースホルダ**（`%(name)s`）で組む。文字列連結禁止
- migration は **連番追記のみ**（lefthook の `no-migration-rewrite` が既存ファイルの変更を拒否する）
- テストデータに実在する人物・団体名を使わない（`tests/support.py` の架空値を使う）。
  共有ファクトリは `tests/support.py`
- 通貨ペア・pip size のハードコード禁止。leg・triangle は `InstrumentSpec` の
  `base_currency / quote_currency` から導く
- コメントは WHAT ではなく WHY。「〜のために追加」のようなコミット文脈依存の文言や、
  AI レビューの引用を残さない。既存の英語 docstring は維持してよく、新規コメントは日本語で可
- ruff（line-length 100、py311）に準拠。型注釈を付ける
