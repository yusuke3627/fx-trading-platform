# issue #160: 戦略が利確を表現できない（PortfolioManager が take_profit_price を None で固定）

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-160-take-profit`
- ブランチ: `feat/issue-160-take-profit`（base は `origin/main` = `059e1f2`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #160 本文（全文転記）

> ## 背景
>
> issue #157 の候補 1（レンジ端の反転）はレンジ中央での全決済を、候補 2（ブレイク後の初回押し目）は 2R 利確を売買ルールに含む。どちらも現在の実装では表現できない。
>
> 利確の配管は端から端まで揃っている:
>
> - `ProtectionSpec.take_profit_price`（`src/trading/domain/intent.py:23`）
> - `OMSService`（`src/trading/oms/service.py:280-282`）が intent の値を execution command へ渡す
> - MT5 マッパー（`src/trading/execution/mt5/mapper.py:215-216, 236-237`）が `tp` として送る
> - `ExecutionSimulator`（`src/trading/backtest/simulator.py:232-247, 297-308`）が建玉に保持し、到達したら保護約定として決済する
>
> 欠けているのは 1 か所だけで、`PortfolioManager._entry_intent`（`src/trading/portfolio/manager.py:145`）が `take_profit_price=None` を直書きしている。`StrategySignal` にも利確を表す項目が無く、戦略から指定する経路が存在しない。
>
> 結果として、現在のすべての戦略は損切りか反対向き setup か時間切れ決済でしか決済できない。H5 の確認あり腕は 239 件中 236 件が、H4 の無効側は 581 件すべてが保護約定（損切り）だった。
>
> ## やること
>
> 1. `StrategySignal`（`src/trading/domain/signal.py`）に `take_profit_distance_pips: Decimal | None = None` を足す。値があるときは正であること（`stop_distance_pips` と同じ扱い）
> 2. `Strategy.make_signal` と `Strategy._setup_signal`（`src/trading/strategy/base.py`）に省略可能な引数として通す。既定は None で、既存戦略の振る舞いを変えない
> 3. `PortfolioManager._entry_intent`（`manager.py:130-152`）で、値があるときだけ利確価格を組む。向きは損切りと対称にする。LONG は `entry_price + distance * pip_size`、SHORT は `entry_price - distance * pip_size`
> 4. `exit_only` signal（`manager.py:74-81`）は決済なので利確を持たない。`_close_intent` は現状のまま
> 5. ADR を追加する（設計は v2.0 で凍結済み。ADR-036 の時間切れ決済が前例）。記録する内容:
>    - 決定: 戦略は利確距離を signal で表現でき、`PortfolioManager` がブローカー側の利確価格へ変換する
>    - 既存の不変条件との関係: 利確はブローカー側の保護であり、裸の反対売買ではない。保護約定で決済された建玉へシステム決済を送らない規則は変わらない
>    - 増し玉（INCREASE）の扱い: netting では新しい command の保護が建玉全体に適用される。損切りと同じ挙動で、利確だけの特別扱いはしない
>    - 既定は None で、有効化は戦略ごとのパラメータで行う。本 ADR は特定の戦略に利確を入れる決定ではない
>
> ## テスト
>
> - `PortfolioManager`: 利確距離があるとき LONG / SHORT それぞれで利確価格が対称に組まれる。無いとき `take_profit_price` は None
> - `exit_only` signal から作られる CLOSE intent に利確が付かない
> - replay: 利確に到達した建玉が保護約定として決済され、`trades.csv` に round trip として現れる（`tests/replay/test_vertical_slice.py` の probe 戦略パターン。シミュレータの利確判定は `simulator.py:297-308`）
> - `tests/unit/test_invariants.py` は触らない
>
> ## やらないこと
>
> - 既存戦略（`failed_spike_reversal` / `post_event_failed_breakout` / `monetary_policy_convergence`）への利確の追加。別 issue で測定とセットで行う
> - #157 の候補 1 / 候補 2 の戦略そのものの実装
> - トレーリングストップ、部分利確、R 倍指定のドメイン化（距離 pips で表現できる。R の計算は戦略側の仕事）
> - `RiskEngine` / OMS / シミュレータ / MT5 マッパーの変更（すでに対応済み）
> - `config/` の変更

---

## この変更の位置づけ

- issue #157 の候補 1（レンジ端の反転、レンジ中央で全決済）と候補 2（ブレイク後の初回押し目、2R 利確）の**前提**。どちらも利確が無いと売買ルールを表現できない。
- 直近の前例は PR #149（issue #148、時間切れ決済）。`Strategy` に helper を足し、既定 off で既存戦略の振る舞いを変えず、ADR を添えた。ADR は `docs/adr/ADR-036-horizon-exit.md`。**同じ書式に合わせる**。

## ユーザー判断で確定していること（変更しない）

- 利確は**距離 pips** で表現する。R 倍のドメイン化はしない。R の計算は戦略側の仕事。
- 既定は `None`。既存戦略の振る舞いを一切変えない。
- `exit_only` signal には利確を付けない。

---

## 変更範囲

### 1. `src/trading/domain/signal.py`

`StrategySignal` に項目を追加する。

```python
take_profit_distance_pips: Decimal | None = None
```

- 制約の書き方は同ファイルの `stop_distance_pips` と `expected_edge_r` を参照する（pydantic の `Field`）。
  `expected_edge_r` は `Field(default=Decimal(1), gt=0)`。今回は `None` 許容なので `Field(default=None, gt=0)` の形で、値があるときだけ正であることを要求する。
- frozen モデルであることを壊さない。

### 2. `src/trading/strategy/base.py`

- `Strategy.make_signal`（355-381 行）: キーワード引数 `take_profit_distance_pips: Decimal | None = None` を足し、`StrategySignal` へそのまま渡す。
- `Strategy._setup_signal`（306-353 行）: 同じ引数を足し、**entry 側の `make_signal` 呼び出しにだけ**渡す。
  閉鎖中の決済専用 signal（`SESSION_CLOSED_EXIT_ONLY` を付けて `exit_only=True` で出す側、343-353 行）には渡さない。決済に利確は意味を持たないため。
- `_horizon_exit`（251-284 行）は決済専用 signal なので触らない。

### 3. `src/trading/portfolio/manager.py`

`_entry_intent`（122-151 行）で利確価格を組む。現状は次のとおり。

```python
stop_offset = signal.stop_distance_pips * sizing.pip_size
if signal.desired_direction is PositionDirection.LONG:
    stop_price = sizing.entry_price - stop_offset
else:
    stop_price = sizing.entry_price + stop_offset
```

損切りと対称に、`signal.take_profit_distance_pips` が `None` でないときだけ利確価格を組む。

- LONG: `sizing.entry_price + distance * sizing.pip_size`
- SHORT: `sizing.entry_price - distance * sizing.pip_size`
- `None` のときは `ProtectionSpec.take_profit_price` を `None` のままにする。

`ProtectionSpec(...)` の `take_profit_price=None` 直書き（145 行）を、組んだ値に差し替える。

`_close_intent`（153 行〜）と `exit_only` 分岐（74-82 行）は**現状のまま**。`exit_only` は `_close_intent` しか通らないので、追加の分岐を足す必要はない。

### 4. ADR を追加する

- 番号は `docs/adr/` の既存最大の次。**作成直前に `ls docs/adr | sort | tail -3` で確認する**（確認時点では ADR-037 が最大なので `ADR-038` になる見込み）。
- ファイル名は `docs/adr/ADR-0NN-<kebab-case>.md`。内容に合う名前を付ける（例: `ADR-038-strategy-take-profit.md`）。
- 書式は `docs/adr/ADR-036-horizon-exit.md` に合わせる。見出しは `# ADR-0NN: <日本語タイトル>` / `**Status:** Accepted (YYYY-MM-DD)` / `## Context` / `## Decision` / `## Consequences`。本文は日本語。
- 日付は今日（2026-09-15）。
- SYSTEM_SPEC 本文は改訂しない（v2.0 で凍結。§1 が「発行後の仕様変更は ADR 追加で行う」と定める）。関係する節には ADR-036 と同じ形で相対リンクを張ってよい。

ADR に**必ず**書く内容（issue 本文の 5 項目）:

1. **決定**: 戦略は利確距離を signal（`take_profit_distance_pips`）で表現でき、`PortfolioManager` がブローカー側の利確価格へ変換する。
2. **既存の不変条件との関係**: 利確はブローカー側の保護であり、裸の反対売買ではない。保護約定で決済された建玉へシステム決済を送らない規則（`ExecutionSimulator.check_protection` が発火時に建玉を book から外す仕組み）は変わらない。
3. **増し玉（INCREASE）の扱い**: netting では新しい command の保護が建玉全体に適用される。損切りと同じ挙動で、利確だけの特別扱いはしない。
4. **既定は `None`**。有効化は戦略ごとのパラメータで行う。本 ADR は特定の戦略に利確を入れる決定ではない。
5. ADR-036 が「take profit は本 ADR の範囲外であり、導入しない」と書いているので、本 ADR がその範囲を引き取ることに触れる。

---

## テスト

### `tests/unit/test_portfolio_manager.py`

既存の `make_signal` ヘルパー（12-30 行）に `take_profit_pips: str | None = None` 相当の口を足し、次を追加する。

- 利確距離があるとき、LONG で `take_profit_price` が entry の**上**に、SHORT で entry の**下**に、損切りと対称な距離で組まれる。
  既存の `sizing()`（`tests/support.py`）は `entry_price=158.840` / `pip_size=0.01` なので、具体値でアサートする（既存の `test_long_stop_sits_below_entry` と同じ書き方）。
- 利確距離が無いとき `intent.protection.take_profit_price is None`。
- `exit_only` signal から作られる CLOSE intent に利確が付かない（`protection` が付かないこと、または `take_profit_price is None` を、`_close_intent` の実際の出力に合わせて確認する）。

### `tests/replay/test_vertical_slice.py`

`ScriptedStrategy`（`src/trading/backtest/engine.py:89-131`）は probe 戦略で、`stop_distance_pips` をコンストラクタ引数で受けている。ここに利確距離を通す口を足し（既定 `None` で既存呼び出しの振る舞いを変えない）、`build_engine` / `run_slice` からも渡せるようにする。

追加するテスト:

- 利確距離を付けて replay を流すと、利確に到達した建玉が**保護約定**（`origin == "PROTECTION"`、`ProtectionReason.TAKE_PROFIT`）として決済され、`result.trades` に round trip として現れる。
  `test_closed_quantities_are_recorded_as_round_trips`（230 行〜）と `test_partial_exit_keeps_remainder_tracked_and_withholds_reversal`（270 行〜）が近い書き方。
- 利確距離を付けない既存の呼び出しは、結果が従来と変わらない（既存テストが green のままであることで担保されるので、専用テストは足さなくてよい）。

合成データ（`synthetic_ticks`、seed 固定）で利確に確実に到達する距離を選ぶこと。到達しない距離だと何も検証していないテストになる。距離を決めるために必要なら、短いスクリプトで実際の tick 系列の価格レンジを確認してよい（そのスクリプトはコミットしない）。

### 触らないもの

- `tests/unit/test_invariants.py` は**触らない**。通すためにテスト側を緩めない。

---

## やらないこと（再掲）

- 既存戦略（`failed_spike_reversal` / `post_event_failed_breakout` / `monetary_policy_convergence`）への利確の追加
- issue #157 の候補 1 / 候補 2 の戦略実装
- トレーリングストップ、部分利確、R 倍指定のドメイン化
- `RiskEngine` / `OMSService` / `ExecutionSimulator` / MT5 マッパーの変更（すでに対応済み）
- `config/` の変更（他セッションが並行作業中。**一切触らない**）
- DB マイグレーション（`StrategySignal` の新項目は DB 列を必要としない。`storage/` に列を足す必要があるかは `rg -n "stop_distance_pips" src/trading/storage migrations` で確認し、必要なら報告する。列追加が必要だと判断した場合は勝手に足さず、理由を添えて返すこと）

---

## 実装上の規約

- 金額・数量・価格は `Decimal`。float を使わない。
- frozen な pydantic モデルを壊さない。新しい値を返す（`model_copy`）。
- 検証はシステム境界（設定・外部 API・Broker 応答）のみ。内部関数間に防御的分岐・フォールバックを足さない。
- WHAT を説明するコメントを書かない。コミット文脈に依存するコメント（「issue #160 のために追加」等）を書かない。
- Strategy 内で `datetime.now()` を直接呼ばない（Clock 注入）。
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない。
- 通貨ペア・pip size・時間足をハードコードしない（`InstrumentSpec` / config を経由する）。

## 完了条件

worktree 内で次がすべて通ること。

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` 無指摘
- 上記 3 ディレクトリの pytest が green（`tests/broker` は MT5 なしで自動 skip、`tests/integration` は PostgreSQL 必須のためここでは対象外）

## 影響範囲の確認

```
rg -n "take_profit" src tests migrations
rg -n "make_signal|_setup_signal" src tests
rg -n "ScriptedStrategy" src tests
```

## コミットしないもの

- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md` / `tmp/`
- 距離決めに使った一時スクリプト

このファイル（`tasks/issue-160.md`）はコミットに含める。
