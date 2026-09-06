# issue #125: ablation 比較で両腕の execution RNG が同じ注文に同じショックを割り当てない

## 要件（issue #125 本文の要約。Codex は issue を読みに行かない前提で全文をここに置く）

### 背景

PR #124（H5: post_event_failed_breakout のマクロ確認 ablation）のレビューで指摘された、
2 つの研究リプレイを A/B 比較するときの共通課題。

### 問題

`ExecutionSimulator` は slippage / reject / partial fill の乱数を、注文の到来順に
1 本の seeded RNG（`random.Random(seed)`）から逐次引く。ablation の 2 腕は
「片方だけが約定を 1 件追加する」ことを目的にしているので、**分岐点より後の共通注文には
両腕で違う乱数が割り当たる**。

結果として `trading.backtest.ablation_compare` が測る差には、確認レッグの寄与だけでなく
execution ショックの引き直しによる差が混ざる。`ablation_compare --seed` は bootstrap の
再標本化を決めるだけで、この差は補正できない（`research --seed` が決める側）。

通常 scenario でも slippage は乱数（`slippage_sigma_pips=0.3`）なので、`--scenario` を
指定しない実行でも起きる。

### 採用する対応方針（ユーザー承認済み・議論は不要）

issue の方針 **1「注文を安定キーにした乱数割当て」** を採る。
注文ごとに決定的なキーを作り、そこから RNG を派生させて、同じ注文には常に同じショックを
割り当てる。方針 2（複数 seed での比較）は採らない。

---

## 設計（この通りに実装すること）

### 1. 安定キーの定義

`ExecutionSimulator.submit(command, ticks)` に来た注文について、次の文字列をキーにする。

```
f"{command.symbol}|{command.side.value}|{command.action.value}"
f"|{command.direction.value}|{command.created_at.isoformat()}"
```

**キーに含めない値と、その理由（コメントとして残すこと）:**

- `command_id` / `intent_id` / `idempotency_key` / `broker_position_ticket`
  — いずれも `uuid4()` かシミュレータ生成の id。run ごとに変わるので安定キーにならない。
  issue でも「キーに broker 生成 id（ticket 等）を含めない」と明記されている
- `quantity`
  — sizing は口座 equity に依存する。ablation の 2 腕は分岐後に equity がずれるので、
  quantity をキーに含めると分岐後の共通注文はすべて別キーになり、この修正の目的
  （共通注文に同じショックを割り当てる）が消える
- `stop_loss_price` / `take_profit_price`
  — 同じ理由でボラティリティ由来の値がわずかにずれうる。キーは最小限にする

`created_at` は ReplayClock 由来の tick 時刻なので、両腕で同じデータセットを流す限り一致する。

### 2. 同一キーが複数回来る場合の扱い

同じ tick 時刻に同じ side / action / direction の注文が複数回来ることはありうる。
シミュレータのインスタンスに `dict[str, int]` の出現回数カウンタを持ち、
`submit` の**最初の行**（`if not ticks:` の早期 return より前）で当該キーの出現回数 `n`
（0 始まり）を取り出してから 1 増やす。実際の派生キーは `f"{key}#{n}"` とする。

ルールは「1 回の `submit` 呼び出し = そのキーの 1 出現」に統一する。約定できずに早期 return
する注文もカウンタを進める（分岐が増えるほど両腕でカウンタがずれる余地が増えるため、
条件付きにしない）。

これにより:
- 同じ run 内の 2 件目以降の同一キー注文には、1 件目と独立したショックが当たる
- 同じデータセット + seed の 2 run では、出現順が同じなので `n` も一致し、行単位で再現する

### 3. RNG の派生

`hashlib.blake2b` で安定ハッシュを取る（`hash()` は `PYTHONHASHSEED` で run ごとに変わるので使わない）。

```python
material = f"{self._seed}|{key}#{n}".encode()
derived = int.from_bytes(hashlib.blake2b(material, digest_size=16).digest(), "big")
rng = random.Random(derived)
```

`self._seed` を保持するため、`__init__` で `random.Random(seed)` を作るのをやめ、
`self._seed = seed` を保存する形に変える（`self._rng` は削除する）。

派生のたびに `random.Random` を 1 つ作るコストは、submit 回数が tick 数に比べて桁違いに
少ないので無視できる。**キャッシュ層や RNG プールを足さない。**

### 4. 乱数の引き方

`submit` の先頭でその注文用の `rng` を 1 つ作り、以降の draw をすべてその `rng` から引く。
**注文内の draw 順は現行と変えない**:

1. reject 判定 `rng.random() < self._costs.reject_probability`（現 106 行目）
2. 約定価格の slippage（現 161 行目 → `_execution_price`）
   - `abs(rng.gauss(0.0, sigma))`
   - `rng.random() < tail_probability` なら `rng.uniform(0.0, tail_max_pips)` を加算
3. partial fill 判定 `rng.random() < self._costs.partial_fill_probability`（現 164 行目）

`_execution_price` は `rng` を引数で受け取るように変える（呼び出しは `submit` の 1 箇所だけ）。

`check_protection` / `_protection_fill` は乱数を使わないので変更しない。

### 5. ENGINE_VERSION を上げる

`src/trading/backtest/engine.py:84` の `ENGINE_VERSION = "0.5.0"` を `"0.6.0"` にする。

理由: この定数は research run の manifest に `engine_version` として書き出され
（`src/trading/backtest/research.py` / `run.py`）、`ablation_compare.COMPARABLE_FIELDS`
にも入っている。本修正は slippage が非ゼロなすべての scenario で約定価格・PnL を変えるので、
修正前後の run は比較可能ではない。先例として ADR-016（carry 追加）でも挙動変更に合わせて
この定数を上げている（`docs/adr/ADR-016-swap-rollover-pit-broker-cost.md` に記載あり）。

`tests/unit/test_ablation_compare.py` は manifest のリテラル値として `"0.5.0"` を持つが、
定数を import していないので変更不要（両腕で同じ値であれば通る）。**触らないこと。**

### 6. ADR は不要（判断済み）

`docs/SYSTEM_SPEC.md` は execution ショックの割り当て方式を規定していない
（88 行目が stress scenario の一覧に触れるのみ）。`tests/unit/test_invariants.py` が守る
ドメイン不変条件（look-ahead 禁止、UNKNOWN 非再送、Exit の ticket 参照など）にも触れない。
**`docs/adr/` に新しい ADR を追加しないこと。**

### 7. 決定性の維持

`src/trading/backtest/engine.py` の module docstring にある
「同じ dataset + config + seed → identical fills, PnL, metrics」を壊さないこと。
キーもカウンタも run 内で決定的なので、この性質は保たれる
（`tests/replay/test_vertical_slice.py::test_same_dataset_config_seed_reproduces_identical_runs`
が回帰テストとして機能する）。

---

## 変更対象ファイル

| ファイル | 変更内容 |
| --- | --- |
| `src/trading/backtest/simulator.py` | 安定キー導出 + per-order RNG 派生。`__init__` の `_rng` を `_seed` + カウンタ dict に置換。`_execution_price` に `rng` 引数を追加。module docstring に 1 行追記（「ショックは注文ごとの安定キーから派生する」） |
| `src/trading/backtest/engine.py` | `ENGINE_VERSION` を `"0.5.0"` → `"0.6.0"`（84 行目、1 行のみ） |
| `src/trading/backtest/ablation_compare.py` | module docstring の注意書きを実態に合わせて更新（後述） |
| `tests/unit/test_simulator.py` | 回帰テストを追加（後述） |
| `tests/replay/*`（必要な場合のみ） | 数値の期待値が動いた場合のみ再ベースラインする。**アサーションを削る・緩める（`>` を `>=` にする、比較そのものを消す等）ことは禁止**。動かした場合は「なぜ動くのが正しいか」を Codex の最終出力に書くこと |
| `tasks/issue-125.md` | 本ファイル（コミット対象） |

**DB マイグレーションは不要**（`migrations/` に触らない）。

## ablation_compare の docstring 更新

現在の文言（PR #124 で入れたもの）:

```
This CLI's --seed controls only bootstrap resampling. Research seeds drive
slippage, rejects and partial fills; an extra fill shifts later shared orders,
so confirm the verdict across several matching research-seed pairs.
```

本修正で「extra fill が後続の共通注文をずらす」前提が消えるので、実態に合わせて書き直す。
書くべき内容:

- `--seed` が決めるのは bootstrap の再標本化だけ、という点は維持する
- execution ショック（slippage / reject / partial fill）は注文ごとの安定キーから派生するため、
  両腕に共通する注文には同じショックが当たる。ablation レッグが増やす約定は後続の共通注文の
  ショックをずらさない
- 残る差の源は「注文自体の同一性が変わった場合」（約定時刻や side が変わった注文は別注文として
  別のショックを受ける）であることに触れる
- 「複数の research seed を揃えて確認せよ」という指示は削除する（前提が変わったため）

英語で簡潔に（既存 docstring と同じトーン）。

## 既存テストの期待値が動きうる理由（動いた場合の説明材料）

1. 乱数ストリームが「run 全体で 1 本」から「注文ごとに 1 本」に変わるため、同じ seed でも
   個々の注文に当たる値が変わる。これは修正の目的そのもの
2. `random.Random.gauss` は 2 値を同時に生成して片方を `gauss_next` にキャッシュする。
   注文ごとに新しい `Random` を作ると毎回キャッシュ無しの経路を通るので、
   共有ストリーム時代の「偶数番目の注文はキャッシュ値を使う」挙動が消える

どちらも「同じ dataset + config + seed の 2 run が行単位で一致する」という決定性
（`engine.py` の module docstring）は壊さない。壊れていないことは
`tests/replay/test_vertical_slice.py::test_same_dataset_config_seed_reproduces_identical_runs`
が検出する。

## この修正で消えない差（docstring と PR 本文に書く残存事項）

ablation の 2 腕で**注文そのものの同一性が変わった**場合は、依然として別のショックが当たる。
例: 確認レッグの有無でポジション保有時間が変わり、次のエントリーの `created_at` がずれる。
これは execution RNG の引き直しではなく注文が別物になったということなので、正しい挙動。

## テスト方針

`tests/unit/test_simulator.py` に追加する。既存テストは緩めない。

1. **`test_shared_order_keeps_its_shock_when_another_arm_adds_a_fill`**（本 issue の中核）
   - `costs = CostModel(latency_ms=0.0, slippage_sigma_pips=0.8)`、`ticks = [make_tick("158.840", "158.844")]`
   - 腕 A: `ExecutionSimulator(costs, usdjpy_spec(), seed=42)` に、先に「片腕だけの追加注文」
     （例: `side=SELL, direction=SHORT, action=OPEN`）を submit してから共通注文を submit
   - 腕 B: 同じ seed の別インスタンスに共通注文だけを submit
   - 共通注文は腕ごとに `make_command(...)` を**別々に生成**する（`make_command` は毎回新しい
     `command_id` / `idempotency_key` を振るので、キーが id 非依存であることも同時に示せる）
   - 両腕の fill price が一致すること
2. **`test_repeated_identical_orders_draw_independent_shocks`**
   - 同一引数の注文を 1 つの simulator に 2 回 submit し、価格が異なること（出現回数カウンタが
     効いていること）を確認する。さらに同じ seed の別インスタンスで同じ 2 回を繰り返すと
     同じ 2 価格が同じ順で出ること（再現性）も確認する
   - seed 固定で決定的なので flaky にならない。もし seed=42 で 2 価格が偶然一致したら、
     別の seed（43, 7 など）に変えてよい
3. 既存の `test_same_seed_reproduces_fill_price` はそのまま通ること

## 完了条件（実行可能なコマンドで）

worktree のルートで、すべて green になること:

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `tests/broker` は MT5 なし環境で自動 skip される（skip は正常）
- `tests/integration` は PostgreSQL 必須なので実行対象外

## やらないこと（スコープ外。手を出さない）

- issue #126（部分決済行を独立標本として数えている件）
- `config/backtest.yaml` の既定値・scenario 設定の変更
- 研究スタディの変更: `src/trading/backtest/policy_event_study.py` /
  `intervention_event_study.py` / `shock_trigger_study.py`
- `migrations/` の追加・変更
- `docs/adr/` への ADR 追加（上記 6 で不要と判断済み）
- `docs/research/` の既存ノートの書き換え（過去 run の verdict の再測定は別作業）
- `tests/unit/test_ablation_compare.py` の manifest リテラル `"0.5.0"` の書き換え
- 周辺リファクタ、無関係な整形、追加の抽象化（乱数ストリームを feature 別に分けるなどの拡張はしない）
- `random.Random` 以外の乱数実装への差し替え

## プロジェクト規約（`.claude/rules/*.md` 由来。Codex 向けに転記）

- コミットしない（コミットと PR は Claude 側が行う）
- 金額・数量・価格は `Decimal`（indicator 計算のみ float 可）。slippage の pips 計算は
  現行どおり float → `Decimal(str(round(...)))` の変換を維持する
- frozen dataclass / pydantic モデルを破壊しない（`replace` / `model_copy` のパターンを維持）
- 検証はシステム境界のみ。内部関数間に防御的分岐を足さない
- WHAT を説明するコメントを書かない。「○○のために追加」などコミット文脈依存のコメントも書かない
- テストデータに実在の人物・団体名を使わない
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない
