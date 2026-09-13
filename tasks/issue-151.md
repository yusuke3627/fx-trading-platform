# issue #151: ablation_compare を戦略・パラメータ非依存にする

`ablation_compare` CLI が H5（`post_event_failed_breakout` / `macro_confirmation_enabled`）の対象をモジュール定数で固定しているため、他の ablation を比較できない。比較対象のパラメータを `--param` で受け取る形に変え、戦略の固定検査を外す。

---

## issue 本文（全文転記）

> ## 現象
>
> H4（`failed_spike_reversal` の時間切れ決済 ablation、issue #148 で事前登録）の 2 腕を比較しようとすると、データが揃っているのに拒否される:
>
> ```
> python -m trading.backtest.ablation_compare --with reports/h4_with/<run_id> --without reports/h4_without/<run_id> --seed 42
> runs are not comparable:
> - with strategy_id must be 'post_event_failed_breakout', got 'failed_spike_reversal'
> - without strategy_id must be 'post_event_failed_breakout', got 'failed_spike_reversal'
> - without resolved_parameters.macro_confirmation_enabled must be False, got None; instrument-specific parameters override defaults
> - without param_overrides.macro_confirmation_enabled must be False
> - non-ablation param_overrides differ: with={'horizon_exit_enabled': True}, without={'horizon_exit_enabled': False}
> ```
>
> ## 原因
>
> `src/trading/backtest/ablation_compare.py:34-35` が H5 の対象をモジュール定数に固定している:
>
> ```python
> ABLATION_PARAM = "macro_confirmation_enabled"
> ABLATION_STRATEGY = "post_event_failed_breakout"
> ```
>
> `verify_comparable`（同 158-201 行）がこの 2 つを直接参照するため、他の戦略・他のパラメータの ablation を受け付けない。判定規則（`judge`）と統計（`arm_summary` / `difference_interval`）自体は対象に依存しておらず、汎用のまま使える。
>
> `strategy_id` は `COMPARABLE_FIELDS` に入っており腕どうしの一致は別途保証されるので、`ABLATION_STRATEGY` との突き合わせは重複でもある。
>
> ## やること
>
> 1. `--param <name>` を CLI の必須引数にし、`ABLATION_PARAM` の参照をこの値に置き換える
> 2. `ABLATION_STRATEGY` とそれを使う検査を削除する（腕どうしの `strategy_id` 一致は `COMPARABLE_FIELDS` が既に保証している）
> 3. 判定文言から H5 固有の「確認レッグ」を外す:
>    - `KEEP` → `"有効のまま維持（寄与あり）"`
>    - `REMOVE` → `"無効にする（絞るだけで質が上がらない）"`
>    - `UNDECIDED_SAMPLE` / `UNDECIDED_DIFFERENCE` は現行のまま
> 4. モジュール docstring の使用例を `--param` つきに直す
> 5. `docs/research/2026-09-10-h5-macro-confirmation-ablation.md` の「再現」節の比較コマンドに `--param macro_confirmation_enabled` を足す（CLI の変更でコマンドが変わるため）
>
> ## やらないこと
>
> - `judge` の判定規則・`MIN_TRADES`・bootstrap の変更（事前登録した規則を動かさない）
> - `COMPARABLE_FIELDS` の増減
> - research 側（`research.py`）の変更

（issue 本文は起票時の記録として保持する。以下の実装計画にはレビューで判明した訂正を反映する。issue 本文の行番号 `34-35` / `158-201` は issue 起票時のもの。現在の `main` では定数が 36-37 行、`verify_comparable` が 132-209 行にある。指すコードは同じ。）

---

## 背景（issue に書かれていない前提）

- 対象は **真偽値パラメータの True / False 比較**。数値・文字列パラメータの任意の値どうしの比較は対象外。CLI の `--param` は名前だけを受け取り、research CLI の `--param KEY=VALUE` とは異なる。
- この変更は **H4**（`failed_spike_reversal` の `horizon_exit_enabled` ablation。判定規則は issue #148 で事前登録済み）の 2 腕を比較するために必要。実測は VPS で完了しており、CLI が受け付けないだけの状態。**実成果物は VPS 上にあり、この worktree からは参照できない。** 実データでの比較は本作業の完了条件に含めず、単体テストで H4 と同じ manifest 形状が通ることを固定するところまでを対象とする。
- **`--param` は必須引数にする（既定値を置かない）。** 既定値を残すと H5 固有の前提が汎用ツールに残り続けるため。代わりに H5 研究ノートの再現コマンドを更新する（やること 5 項目目）。
- **判定規則（`judge`）・`MIN_TRADES`・bootstrap・`COMPARABLE_FIELDS` は一切変更しない。** 事前登録した規則を動かすことになる。

---

## 変更対象ファイル

| ファイル | 変更内容 |
| --- | --- |
| `src/trading/backtest/ablation_compare.py` | `ABLATION_PARAM` / `ABLATION_STRATEGY` 削除、`param` 引数の配線、`KEEP` / `REMOVE` 文言、docstring |
| `tests/unit/test_ablation_compare.py` | 新シグネチャへの追随とテスト追加 |
| `docs/research/2026-09-10-h5-macro-confirmation-ablation.md` | 「再現」節の比較コマンドに `--param macro_confirmation_enabled` を追加 |

DB マイグレーションなし。config 追加なし。UI なし（CLI のみ）。

---

## 実装 1: `src/trading/backtest/ablation_compare.py`

### 1-1. 定数

- 36 行の `ABLATION_PARAM = "macro_confirmation_enabled"` を**削除**する
- 37 行の `ABLATION_STRATEGY = "post_event_failed_breakout"` を**削除**する
- 31-32 行の文言を差し替える:
  ```python
  KEEP = "有効のまま維持（寄与あり）"
  REMOVE = "無効にする（絞るだけで質が上がらない）"
  ```
- `UNDECIDED_SAMPLE` / `UNDECIDED_DIFFERENCE` / `MIN_TRADES` / `COMPARABLE_FIELDS` / `MANIFEST_FIELDS` は変更しない

### 1-2. `verify_comparable` のシグネチャ

現行 132 行:

```python
def verify_comparable(with_: RunArtifacts, without: RunArtifacts) -> None:
```

を次に変える（`param` は必須の位置引数。既定値を置かない）:

```python
def verify_comparable(with_: RunArtifacts, without: RunArtifacts, param: str) -> None:
```

引数の配線に加え、H5 固有だった欠損値の扱いを訂正する:

- 158-163 行の `strategy_id` ブロック（`if strategy_id != ABLATION_STRATEGY:` とその `reasons.append`）を**丸ごと削除**する。`strategy_id` は `COMPARABLE_FIELDS`（46 行）に入っているので、腕どうしの一致は先頭のループ（143-149 行）が引き続き保証する
- 164-174 行・188-206 行に現れる `ABLATION_PARAM` を、すべて引数 `param` に置き換える。エラーメッセージの補間（`f"{arm} resolved_parameters.{ABLATION_PARAM} must be ..."` 等）も `param` を使う形にする
- `resolved_enabled = resolved.get(param)` とし、両腕とも記録された実効値を要求する。with は `is True`、without は `is False` の検査を維持し、欠損・null・数値の 0/1・文字列を拒否する。with の欠損を True と見なしてはいけない。`Strategy._horizon_exit` は既定 False、`config/base.yaml` の H4 設定も False であり、H5 の既定 True を他のパラメータに適用できないため。`research.main` は `params_for(symbol).values` を manifest に保存しているので、CLI 引数の指定だけではなく、この記録を判定根拠にする。古い成果物で対象値が記録されていなければ、推定で補わず実効値を記録した再実行を求める。
- docstring（133-141 行）は内容が今も正しいので原則そのまま。`ablated parameter` の語が `param` 引数を指すことが読み取れれば十分で、無理に書き換えない

### 1-3. `report` のシグネチャ

現行 276 行:

```python
def report(with_: RunArtifacts, without: RunArtifacts, seed: int) -> str:
    verify_comparable(with_, without)
```

を次に変える:

```python
def report(with_: RunArtifacts, without: RunArtifacts, param: str, seed: int) -> str:
    verify_comparable(with_, without, param)
```

`param` は `seed` の前に置く（`seed` は既存テストが `seed=42` のキーワードで渡しているため、位置を後ろにしても壊れない）。`report` の残りの本体（`arm_summary` / `difference_interval` / 表の組み立て / `judge`）は変更しない。

### 1-4. `main()`

現行 326-333 行に `--param` を足す。**必須引数にする（`default` を置かない）**:

```python
parser.add_argument("--param", required=True, help="boolean ablation parameter name (with=True, without=False)")
```

`print(...)` 行は `report(load_run(args.with_run), load_run(args.without_run), args.param, args.seed)` にする。`--with` / `--without` / `--seed` の定義は変えない（`--seed` の `default=42` はそのまま）。

### 1-5. モジュール docstring（1-12 行）

1 行目のサマリー行が H5 固有（"one strategy confirmation leg"）なので、対象非依存の表現にする。3-6 行の使用例に `--param` を足す。例:

```python
"""Compare two research runs that differ only in one ablated parameter.

    python -m trading.backtest.ablation_compare \
        --with reports/h5_with/<run_id> \
        --without reports/h5_without/<run_id> \
        --param macro_confirmation_enabled \
        --seed 42

This CLI's --seed controls only bootstrap resampling. ...
"""
```

8-11 行の `--seed` に関する段落（execution shock の説明）は事実として今も正しいので**変更しない**。

---

## 実装 2: `tests/unit/test_ablation_compare.py`

既存テストのうち `verify_comparable` / `report` を呼ぶものはすべて新シグネチャへ追随させる。**検査を緩めて既存テストを通すのは禁止**（不変条件と同じ扱い）。

### 2-1. ヘルパー `manifest()`（133-165 行）の一般化

現在 `strategy_id` と `macro_confirmation_enabled` をリテラルで持っている。パラメータ名と戦略 ID を差し替えられるようにする。既存の呼び出し（全 15 箇所前後）が引数なしで今までどおり動くよう、追加する引数には既定値を置く（テストヘルパーなので既定値可。本体コードの `--param` とは扱いが違う）:

```python
def manifest(
    run_id: str,
    param_overrides: dict,
    *,
    resolved_enabled: bool | None = None,
    param: str = "macro_confirmation_enabled",
    strategy_id: str = "post_event_failed_breakout",
) -> dict:
    if resolved_enabled is None:
        resolved_enabled = param_overrides.get(param, True)
    return {
        ...
        "strategy_id": strategy_id,
        ...
        "resolved_parameters": {param: resolved_enabled},
    }
```

`backtest_result()`（97-130 行）の `TradeRecord.strategy_id` は manifest の検査に関与しないので変更不要。

### 2-2. 既存テストの追随

- `verify_comparable(...)` の呼び出し全箇所に第 3 引数 `"macro_confirmation_enabled"` を渡す
- `report(with_run, without_run, seed=42)` は `report(with_run, without_run, "macro_confirmation_enabled", seed=42)` にする
- `test_verify_comparable_requires_the_ablation_strategy`（335-355 行）は検査そのものを削除するので**このテストを削除**し、2-3 の `strategy_id` 不一致テストで置き換える（「両腕とも `failed_spike_reversal` なら通る」が新しい正しい振る舞いなので、現行テストは残せない）
- `judge` / `difference_interval` / `load_run` のテストは変更不要

### 2-3. 追加・拡張するテスト

1. **別の戦略・別のパラメータの 2 腕が比較を通ること**
   `failed_spike_reversal` / `horizon_exit_enabled` で `manifest()` を組み、with 腕は `param_overrides={"horizon_exit_enabled": True}` かつ `resolved_enabled=True`、without 腕は `param_overrides={"horizon_exit_enabled": False}` かつ `resolved_enabled=False` にして、`verify_comparable(..., "horizon_exit_enabled")` が例外を出さないことを固定する。これが issue の現象（H4 の 2 腕が拒否される）の回帰テストになる。
   **with 腕で override を省略した fixture を成功例にしない。** `config/base.yaml` の `failed_spike_reversal.parameters.defaults.horizon_exit_enabled` は False なので、省略は「無効」を意味し、H4 の with 腕の実態（`param_overrides` に明示的な True）と食い違う。両腕とも値を明示した fixture にすること。
   `load_config` や `with_param_overrides` は使わない。`verify_comparable` は manifest だけを読み config には触れないので、設定ロードを持ち込むと検査対象と無関係な結合が増える。

2. **`--param` で指定したパラメータが without 腕で False でなければ拒否されること**
   without 腕の `param_overrides` に対象パラメータが無い（または True）状態で `verify_comparable(..., "horizon_exit_enabled")` を呼び、`SystemExit` かつメッセージに `horizon_exit_enabled` を含むことを確認する。既存の `test_verify_comparable_requires_disabled_without_arm`（276-289 行）の別パラメータ版に相当するので、名前を衝突させないこと。

3. **腕どうしで `strategy_id` が異なれば `COMPARABLE_FIELDS` の検査で拒否されること**
   with 腕 `post_event_failed_breakout` / without 腕 `failed_spike_reversal` で `verify_comparable` を呼び、`SystemExit` かつメッセージに `strategy_id` を含むことを確認する。**`ABLATION_STRATEGY` を削除してもこの保証が残ることを固定するのが目的**なので、削ってはいけない。

4. **with 腕でも実効値が記録されていなければ拒否されること**
   1 のテストの with 腕から `resolved_parameters` の `horizon_exit_enabled` キーだけを削除し、対象キー名を含む `SystemExit` を確認する。**これが 1-2 で入れる振る舞いの変更（欠損を True と見なさない）そのものの回帰テスト**なので必ず入れる。ヘルパーの `resolved_enabled` は必ず値を埋めるので、欠損ケースは生成後の manifest から `del` して作る。
   値が `0` / `"true"` 等の非 bool になるケースは `is True` / `is False` の同一性検査が一律に弾くので、型ごとの parameterize は足さない。
   既存の H5 の instrument override 拒否（`test_verify_comparable_rejects_instrument_override_of_without_arm`）と非対象 override の不一致・非破壊の検査はそのまま保持する。

5. **`--param` が必須であること**
   `monkeypatch` で `sys.argv` に `--with` / `--without` だけを与えて `main()` を呼び、`SystemExit.code == 2` と stderr に `--param` が出ることを確認する。`--help` の目視確認では必須指定の保証にならない。
   あわせて **`main()` の正常経路を 1 本**通す: `tmp_path` に `write_report` で H4 形状の両腕成果物を作り、`--param horizon_exit_enabled --seed 43` を渡して `capsys` で `seed=43` と `verdict:` が出ることを確認する。`param` と `seed` を取り違えて配線するミスはこれでしか落ちない。`report` / `verify_comparable` は mock しない。seed 既定値の parameterize は足さない。

テストは日本語コメント不要。実在する人物・団体名を使わない（架空値のみ）。

---

## 実装 3: `docs/research/2026-09-10-h5-macro-confirmation-ablation.md`

「再現」節（88-92 行あたり）のコードブロック内、3 行目の比較コマンドにだけ `--param macro_confirmation_enabled` を足す:

```
python -m trading.backtest.ablation_compare --with reports/h5_with3/<run_id> --without reports/h5_without3/<run_id> --param macro_confirmation_enabled --seed 42
```

同じブロックの `research` コマンド 2 行は変更しない（`research.py` は対象外）。

---

## やらないこと（スコープ外。手を出さない）

- `judge` の判定規則・`MIN_TRADES`・bootstrap（`difference_interval` / `BOOTSTRAP_SAMPLES` / `BOOTSTRAP_LEVEL`）の変更
- `COMPARABLE_FIELDS` / `MANIFEST_FIELDS` の項目の増減
- `src/trading/backtest/research.py` / `run.py` / `report.py` の変更
- 研究ノートの「再現」節以外の書き換え。特に 5 行目の判定結果と 17-22 行の判定規則テーブルは**測定当時の記録**なので、`KEEP` / `REMOVE` の文言変更に合わせて書き換えない（テーブルは元々 H5 の文脈で言い換えた表現で、定数のリテラル引用ではない）
- `tasks/*.md` の過去の計画ファイル（`tasks/h5-macro-confirmation-ablation.md` 等）の書き換え
- 周辺リファクタ・無関係な整形・型注釈の一括追加
- `--param` に既定値を与えること（必須引数にする方針は確定済み）

---

## プロジェクト規約（`.claude/rules/*.md` からの転記。Codex が読む `AGENTS.md` 以外の分）

- 全文検索は `grep` ではなく `rg` を使う（`.claude/rules/change-management.md`）
- 金額・数量・価格に `float` を使わない（`Decimal`）。本変更では新規の数値計算を足さないので該当箇所は無いはず
- 引数や共有オブジェクトを破壊しない。`verify_comparable` は現行どおり `dict(...)` でコピーしてから `pop` する（`test_verify_comparable_rejects_other_override_mismatch_without_mutation` がこれを固定している）
- 検証はシステム境界（CLI 引数・外部成果物の読み込み）だけで行い、内部関数間に防御的分岐を足さない
- ファイルは 200〜400 行を目安にするが、本変更で分割は不要
- **コミットしない。** コミット・push・PR 作成は Claude 側が担当する（`Fixes #151` / `Closes #151` / `@codex review` の規約が Claude 側の手順にしかないため）
- UI 変更は無いのでスクリーンショット関連の作業は不要

---

## 完了条件（実行可能なコマンド）

worktree は `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-151-ablation-compare-generic`。

```bash
.venv/bin/pytest tests/unit/test_ablation_compare.py
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
.venv/bin/python -m trading.backtest.ablation_compare --help   # --param が必須として表示される
```

- `ruff check .` が無指摘
- `pytest tests/unit tests/replay tests/failure` が green（`tests/broker` は MT5 なし環境で自動 skip、`tests/integration` は DB 必須のため対象外）
- 2-3 の 5 本（H4 形状の 2 腕が通る / without の False 要求 / `strategy_id` 不一致の拒否 / with の実効値欠損の拒否 / `--param` 必須と `main()` の正常経路）がすべて通る

VPS 上の実 H4 成果物での比較は、この worktree から成果物を参照できないため完了条件に含めない。

## 完了報告に含めること

- 変更したファイル一覧
- 実行したテストとその結果
- 計画から逸脱した点（あれば理由つき）


## 計画レビューでの訂正（2026-09-13）

初稿は `verify_comparable` の欠損値の扱いを現行のまま（with 腕は未記載なら True 扱い）にしていた。これは `macro_confirmation_enabled` の既定が True であることに依存した H5 固有の前提で、汎用化した後は誤りになる。根拠:

- `src/trading/strategy/base.py:265` は `params.param("horizon_exit_enabled", False)` と既定 False で読む
- `config/base.yaml` は `failed_spike_reversal` / `post_event_failed_breakout` の両方で `horizon_exit_enabled: false`
- `src/trading/backtest/research.py:554` は `dict(params_for(symbol).values)` を manifest に保存するので、config にあるパラメータは常に実効値として記録される

つまり既定 False のパラメータでは「未記載＝無効」であり、これを with 腕の有効として受け入れると、無効だった run を with 腕として比較してしまう。よって 1-2 で両腕とも実効値の明示を要求する形に訂正し、2-3 の H4 成功 fixture も override を明示する形に直した。この訂正は実 H4 成果物の比較を妨げない（with 腕は `param_overrides` に明示的な True を持ち、resolved にも True が記録される）。
