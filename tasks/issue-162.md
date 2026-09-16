# issue #162: netting の縮小 command に新規側 intent の保護価格を載せない

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-162-netting-reduce-protection`
- ブランチ: `fix/issue-162-netting-reduce-protection`（base は `origin/main` = `d70fd67`）
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## issue #162 本文（全文転記）

> ## 背景
>
> PR #161（戦略シグナルへの利確距離の追加）のレビューで Codex から P2 として指摘された内容。PR #161 の変更が原因ではなく、`OMSService` の netting 経路に元からある構造的なギャップなので、別 issue として切り出す。
>
> ## 問題
>
> netting 口座で、ある戦略の反対向き OPEN intent が既存の broker net を縮小する場合（例: net SHORT に対する LONG signal）、`OMSService.command_for_netting` は command を `REDUCE` / `SHORT` に付け替える（`src/trading/oms/service.py` の `abs(resulting_net) < abs(current_net)` 分岐）。
>
> 一方で `OMSService._command` は `intent.protection` の `stop_loss_price` / `take_profit_price` をそのままコピーする。結果として、LONG intent 用に計算した保護価格（entry より下の SL、entry より上の TP）が、残存する SHORT ポジション向けの `REDUCE` command に載る。SHORT にとってはどちらも逆側の価格なので、実口座へ送れば拒否か誤った保護指定になる。
>
> ## 現状の到達可能性
>
> 現時点では実害に至る経路がない。記録のため整理しておく。
>
> - `config/base.yaml` の `expected_account_mode` は `HEDGING`。`BacktestEngine` は `account_mode is not AccountMode.HEDGING` で `ValueError` を投げるため、netting 経路はまだどこからも動かない
> - `ExecutionCommand` を MT5 の注文リクエストへ変換する live 経路はまだ無い（`mapper.market_order_request` の呼び出し元は `preflight.py` とテストのみ）
> - backtest simulator は `REDUCE` / `CLOSE` command の `stop_loss_price` / `take_profit_price` を読まない（FIFO で数量を減らすだけ）
>
> つまり netting 口座を有効化する時点で顕在化する latent な問題。
>
> ## 利確固有の問題ではない
>
> 同じ取り違えは `stop_loss_price` でも起きる。LONG intent の SL は entry より下にあり、残存 SHORT に対しては同様に逆側になる。この挙動は PR #161 以前から `main` にあり、PR #161 は `src/trading/oms/service.py` を変更していない。利確を足したことで同じギャップが TP にも及ぶようになっただけで、新しい種類の不具合ではない。
>
> ## 対応方針（案）
>
> `command_for_netting` が `REDUCE` / `CLOSE` へ再分類した command については、新規側 intent の保護をそのまま載せない。次のどちらかを選ぶ設計判断が要る。
>
> 1. 再分類された command では保護を外す（`stop_loss_price` / `take_profit_price` を `None` にする）
> 2. 残存する broker position の保護を維持する（別途 SLTP modify として扱う）
>
> あわせて、netting 経路を対象にした replay / unit テストを追加する。現在の replay は HEDGING 専用なのでこの経路を踏めない。

---

## 採用する方針（確定。変更しない）

issue の **候補 1** を採用する。`command_for_netting` が `REDUCE` / `CLOSE` に付け替えた command では `stop_loss_price` と `take_profit_price` を `None` にする。

理由: netting 口座の縮小 deal は残存 position の保護を運ばない。残存 position の SL/TP を変えるのは別の SLTP modify 操作で、本 issue の範囲外（候補 2 は採用しない）。

---

## 現状のコード（確認済み）

`src/trading/oms/service.py`

- `command_for_netting`（72-149 行）: `resulting_net` を求めたあと、133-139 行の `if abs(resulting_net) < abs(current_net):` で `action` を `CLOSE` / `REDUCE` に、`direction` を現在 net の向きに付け替える。その後 140-149 行で `self._command(intent, ...)` を呼ぶ。
- `_command`（247-289 行）: `intent.protection` の `stop_loss_price` / `take_profit_price` を無条件に command へコピーする（277-282 行）。`command_for_entry` と `command_for_hedging_exit` も同じ `_command` を使う。
- `command_for_netting` の呼び出し元は `src/` に無く、テストだけ（`rg -n "command_for_netting" src tests`）。`BacktestEngine` が呼ぶのは `command_for_entry` と `command_for_hedging_exit` で、`command_for_netting` は呼ばない。

`tests/support.py`

- `make_intent`（440-469 行）: `protected=True` のとき `ProtectionSpec(stop_loss_price=Decimal("159.50"), take_profit_price=None, ...)` を固定で付ける。SL の値と TP を外から指定する口は無い。

---

## 変更範囲

### 1. `src/trading/oms/service.py`

`command_for_netting` の再分類分岐（133-139 行）で付け替えた command に限り、保護を載せない。

推奨する形（差分が最小で、`_command` が `intent.protection` を暗黙にコピーする構造を明示化できる）:

- `_command` にキーワード引数 `protection: ProtectionSpec | None` を足し、277-282 行の `intent.protection` 参照をこの引数に置き換える。
- `command_for_entry` と `command_for_hedging_exit` は `protection=intent.protection` を渡す（振る舞いは従来どおり）。
- `command_for_netting` は `protection = intent.protection` を持っておき、再分類分岐の中で `protection = None` にしてから `_command(..., protection=protection)` を渡す。

`ProtectionSpec` は `trading.domain.intent` にある（frozen pydantic モデル）。

再分類されない command（flat から建てる OPEN、同方向の INCREASE）は従来どおり intent の保護を載せる。

分岐に短い WHY コメントを 1〜2 行添える（netting の縮小 deal は残存 position の保護を運ばない。intent の保護は新規側の向きで計算されているため残存側には逆向きになる）。周辺コメントは英語なので英語で書いてよい。「issue #162 のため」のようなコミット文脈のコメントは書かない。

### 2. `tests/support.py`

`make_intent` に省略可能な引数を足し、保護の値を外から指定できるようにする。既定値は現在の固定値と同じにして既存テストの振る舞いを変えない。

```python
stop_loss: str = "159.50",
take_profit: str | None = None,
```

`protected=True` のときの `ProtectionSpec` にこの値を使う（`Decimal(stop_loss)`、`Decimal(take_profit) if take_profit else None`）。

### 3. `tests/unit/test_netting_and_oms.py`

既存の `FakeBroker` と `make_intent` を使い、次を追加する。テスト名は既存の `test_netting_*` に揃える。

(a) **再分類 REDUCE で保護が外れる**
    net `-80000`、intent は `OPEN` / `LONG` で SL・TP 両方あり（LONG 向きの値。例: `stop_loss="158.00"`, `take_profit="159.50"`）、`desired_net=-60000`。
    期待: `action is REDUCE`、`direction is SHORT`、`side is BUY`、`quantity == 20000`、`stop_loss_price is None`、`take_profit_price is None`。

(b) **再分類 CLOSE で保護が外れる**
    zero を跨ぐケース: net `1000`、intent は `CLOSE`（`make_intent(action=PositionAction.CLOSE, direction=PositionDirection.SHORT, take_profit=...)` のように SL・TP 両方あり）、`desired_net=-1000`。
    期待: `action is CLOSE`、`direction is LONG`、`side is SELL`、`quantity == 1000`、`stop_loss_price is None`、`take_profit_price is None`。
    （`OPEN` intent で zero を跨ぐと `command_for_netting` は `None` を返す（reversal は flat 待ち）ので、(b) の intent は `CLOSE` にする。flatten（`desired_net=0`）の形と parametrize でまとめてもよい。）

(c) **再分類されない command は保護を保持する（回帰）**
    - flat から建てる OPEN: net `0`、intent `OPEN` / `SHORT`（SL・TP あり）、`desired_net=-1000` → `action is OPEN`、`stop_loss_price == Decimal("159.50")`、`take_profit_price == Decimal(<指定した TP>)`。
    - 同方向の INCREASE: net `-80000`、intent `INCREASE` / `SHORT`（SL・TP あり）、`desired_net=-110000` → `action is INCREASE`、`side is SELL`、`quantity == 30000`、SL・TP が intent の値のまま。
    parametrize で 1 本にまとめてよい。

既存テスト（特に `test_netting_shrink_delta_is_a_reduce_command`、`test_netting_command_side_matches_action_and_direction`）は変えない。

---

## やらないこと

- 候補 2（残存 position の SLTP modify）
- `BacktestEngine` の HEDGING 限定の解除
- `command_for_hedging_exit` の挙動変更（hedging の CLOSE intent は `PortfolioManager._close_intent` が `protection=None` で作るので、そもそも保護を運ばない）
- simulator / strategy / portfolio / storage / migrations / config の変更
- ADR の追加（ドメイン不変条件の変更ではない）
- replay テストの追加（netting は replay で踏めないため unit のみ）
- `tests/unit/test_invariants.py` の変更（通すためにテスト側を緩めない）
- 周辺リファクタ・無関係な整形

---

## 実装上の規約

- 金額・数量・価格は `Decimal`。float を使わない。
- frozen な pydantic モデルを壊さない。新しい値を返す。
- 検証はシステム境界のみ。内部関数間に防御的分岐・フォールバックを足さない。
- WHAT を説明するコメントを書かない。
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない。
- Strategy / LLM 層から OMS へ到達する経路を作らない。`UNKNOWN` の再送や `SUBMITTING` の re-claim に触れない。

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
rg -n "command_for_netting|command_for_entry|command_for_hedging_exit|_command\(" src tests
rg -n "make_intent\(" tests | rg "protected"
```

## コミットしないもの

- `tmp/`（Codex のログとセッション ID）
- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md`

このファイル（`tasks/issue-162.md`）はコミットに含める。
