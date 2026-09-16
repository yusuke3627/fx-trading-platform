# issue #178: ablation_compare を真偽値以外のパラメータの腕へ広げる

GitHub issue: #178（`gh issue view 178` で全文を読める）
ブランチ: `feat/issue-178-ablation-param-values`

## 目的

`python -m trading.backtest.ablation_compare` は、有効側（`--with`）の腕は `True`、無効側（`--without`）の腕は
`False` と決め打ちで照合するため、真偽値以外のパラメータを変えた 2 本の run を比較できない。
#157 候補 1（H6）の事前登録にある「`entry_band_fraction` を `0.2` → `1.0` にする腕」がこれで拒否される。
CLI が使えないと `arm_summary` / `difference_interval` / `judge` を手で呼ぶことになり（H4 の測定 #163 で実際にそうなった）、
事前登録した判定規則を機械的に当てられなくなる。

`--with-value` / `--without-value` を任意引数として足し、腕ごとに期待値を明示できるようにする。
既定は現在の挙動どおり `True` / `False` で、既存の研究ノート（`docs/research/2026-09-10-h5-*.md`、
`docs/research/2026-09-16-h4-*.md`）に書かれた再現コマンドはそのまま通る。

## 変更範囲

### 1. `src/trading/backtest/ablation_compare.py`

#### 1-a. 期待値の照合規則（1 か所に置く）

期待値 `expected` と manifest の値 `actual` を突き合わせる小さな関数を 1 つ置き、下記 1-b / 1-c / 1-d の
全てがそれを使う。規則:

- `expected` が `bool` なら `actual is expected`
- それ以外は `type(actual) is type(expected)` を確かめたうえで `actual == expected`

理由: Python では `True == 1` が成立するので、真偽値の腕に `1` が入っていても `==` では通ってしまう。
逆に `entry_band_fraction` の期待値 `1.0`（float）に対して manifest が `1`（int）なら、YAML と `--param` の型が
食い違っている状態なので、黙って通さず拒否する（`isinstance(True, int)` が真なので、bool の分岐を先に置くこと）。

manifest.json は `json.loads` で読むため、`true`/`1`/`1.0`/`"1"` は bool/int/float/str として区別されて届く。

#### 1-b. `verify_comparable(with_, without, param, *, with_value=True, without_value=False)`

- 型注釈は `ParamValue`（`trading.strategy.parameters.ParamValue = float | int | str | bool`）
- 冒頭で **両腕の期待値が同じ（1-a の規則で一致する）なら拒否する**（比較になっていない）。
  メッセージには param と両方の値を `!r` で入れる
- 現在の
  ```python
  expected_enabled = arm == "with"
  resolved_enabled = resolved.get(param)
  if resolved_enabled is not expected_enabled:
  ```
  を、腕ごとの期待値（`with` → `with_value`、`without` → `without_value`）との照合に置き換える。
  拒否メッセージは現在と同じ粒度を保つ: どちらの腕（`with` / `without`）の `resolved_parameters.{param}` が、
  何を期待して（`!r`）、何だったか（`!r`）が分かる形。末尾の
  "instrument-specific parameters override defaults" の注記は維持してよい
- **`param_overrides` の検査も同じ一般化が必要**（issue 本文には明記されていないが、ここを直さないと
  `entry_band_fraction` の腕は依然として拒否される）。現在の
  ```python
  if param in with_overrides and with_overrides[param] is not True:
      reasons.append(f"with param_overrides.{param} must be omitted or True")
  if without_overrides.get(param) is not False:
      reasons.append(f"without param_overrides.{param} must be False")
  ```
  を次の意味に置き換える（1-a の規則で照合）:
  - `with` 側: `param_overrides[param]` が **省略されているか、`with_value` と一致**すること
    （既定値のまま流した腕は override を持たない）
  - `without` 側: `param_overrides[param]` が **存在し、`without_value` と一致**すること
    （無効側／変更側は必ず `--param` で上書きして流す、という現在の要件を保つ）
  - メッセージは現在の書式を踏襲し、期待値を `!r` で埋める（例: `with param_overrides.{param} must be omitted or 0.2`、
    `without param_overrides.{param} must be 1.0`）
- 非 ablation の `param_overrides` が両腕で一致すること（`param` を除いた比較）、`COMPARABLE_FIELDS`、
  `git_commit`、建玉・未完了コマンド、期間、trailing blackout の各検査は変えない

#### 1-c. `report(with_, without, param, seed, *, with_value=True, without_value=False)`

- 同じ 2 引数を `verify_comparable` へ渡す
- 出力の **冒頭に 1 行**、「どのパラメータをどの値からどの値へ変えた比較か」を出す。値は `!r` で出し、
  `0.2` / `1.0` / `1` / `'1'` / `True` が見分けられるようにする。1 行目の形式は実装者が決めてよいが、
  `with:\n` / `without:\n` という部分文字列を含めないこと（既存テストが `rendered.split("with:\n", 1)` で節を切り出している）
- **既存の出力行の名前・書式・値は一切変えない**（既存の研究ノートが行名を参照している）。
  `with:` / `without:` 節、`metric` 表、`difference of means ...` 2 行、`verdict:` 行は現状のまま

#### 1-d. `main`

- `--with-value` / `--without-value` を任意引数として足す。既定は `True` / `False`
- 値の解釈は `research.py` の `parse_param_override` と **同じ規則**（`true`/`false` は大文字小文字を問わず `bool`、
  `[+-]?\d+` は `int`、`float()` で読めれば `float`、それ以外は文字列）。
  **規則を 2 か所に書かない。** 推奨: `research.py` の `parse_param_override` から「値だけを解釈する部分」を
  `parse_param_value(text: str) -> ParamValue` として切り出し、`parse_param_override` はそれを呼ぶ形にして、
  `ablation_compare` は `from trading.backtest.research import parse_param_value` で使う（`argparse` の `type=` に渡す）。
  `parse_param_override` の外から見た挙動（戻り値・`ArgumentTypeError`）は変えない。
  `research` の import が `ablation_compare` 側で問題になる場合だけ、`trading/strategy/parameters.py` など
  `ParamValue` の隣に置く案に切り替えてよい（その場合も `parse_param_override` はその関数を呼ぶこと）
- `--param` の help（現在 "boolean ablation parameter name (with=True, without=False)"）を、
  真偽値以外も比較できる旨と既定値が分かる文に直す

### 2. モジュール docstring

真偽値以外のパラメータも比較できること、期待値は `--with-value` / `--without-value` で腕ごとに明示すること、
既定は `True` / `False` であることを 1〜2 文足す。既存の使用例と seed / block interval の説明は残す。

### 3. テスト（`tests/unit/test_ablation_compare.py`）

既存の `manifest` ヘルパー・`run_metrics`・`backtest_result`・`write_report` を使った組み立てに倣う。
`manifest` の `resolved_enabled: bool | None` は真偽値以外を入れられるよう型注釈を `ParamValue | None` に広げる
（引数名の変更は必須ではない。既存テストの呼び出しを大量に書き換えない）。

追加するテスト:

- `entry_band_fraction` を `with=0.2`（override 省略、`resolved_parameters` は `0.2`）/ `without=1.0`
  （`param_overrides` と `resolved_parameters` が `1.0`）にした腕の対が `verify_comparable` を通ること
- 同じ対を `report(..., with_value=0.2, without_value=1.0)` に通し、1 行目に param と `0.2` / `1.0` が出て、
  既存の `verdict:` 行などが最後に残ること
- 真偽値の腕（既定の `True`/`False`）で `resolved_parameters` に `1` / `0` が入っていたら拒否すること（`True == 1` を通さない）
- float の期待値 `1.0` に対して `resolved_parameters` が int の `1` なら拒否すること
- 両腕の期待値が同じ（例: `with_value=0.2, without_value=0.2`）なら拒否すること
- 期待値と食い違ったときのメッセージに、腕（`with` / `without`）・期待値・実際の値が含まれること
- `without` 側の `param_overrides[param]` が期待値と違う値（例: 期待 `1.0` に対して `0.5`）なら拒否すること
- `parse_param_value` の型: `"0.2"` → `float`、`"true"` → `bool`、`"3"` → `int`、`"abc"` → `str`
  （`type(...) is ...` で確かめる。`tests/unit/test_research_runner.py` の
  `test_parse_param_override_preserves_scalar_types` の書き方に倣う）
- `main` が `--with-value 0.2 --without-value 1.0` を解釈して `report` へ渡すこと
  （`monkeypatch.setattr(sys, "argv", ...)` で `main()` を呼び、出力 1 行目か、`report` を monkeypatch して受け取った引数で確かめる）

変えないこと: 既存の `judge` / `arm_summary` / block bootstrap / `load_run` / `write_report` のテストと、
既存の `verify_comparable` テスト（`--with-value` 省略時の `True` / `False` 要求はこれらが担保している）。

### 4. `tests/unit/test_research_runner.py`

`parse_param_override` の既存テストは無変更で通ること。`parse_param_value` を `research.py` に置く場合、
このファイルにテストを足す必要はない（3 で `ablation_compare` 側から確かめる）。

## やらないこと

- `judge` の判定規則、`MIN_TRADES`、`COMPARABLE_FIELDS`、`TRAILING_BLACKOUT_MAX_RATIO`、bootstrap（i.i.d. / block とも）の変更
- 複数パラメータを同時に変える腕の対応（1 要素ずつ変えるのが事前登録の前提）
- 既存の研究ノート（`docs/research/*.md`）の再計算・書き換え
- `research.py` の CLI 引数の変更（値解釈の切り出しはしてよいが、外から見た挙動は変えない）
- `docs/SYSTEM_SPEC.md` の編集（v2.0 で凍結）
- 周辺のリファクタ・無関係な整形・コメント追加

## 参照

- 規約: `AGENTS.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- 実装: `src/trading/backtest/ablation_compare.py`（`verify_comparable` / `report` / `main`）、
  `src/trading/backtest/research.py`（`parse_param_override`、319 行付近）、
  `src/trading/strategy/parameters.py`（`ParamValue`）
- テスト: `tests/unit/test_ablation_compare.py`、`tests/unit/test_research_runner.py`
- CLI の利用例: `docs/research/2026-09-16-h4-failed-spike-reversal-base-edge.md` 155 行付近、
  `docs/research/2026-09-10-h5-macro-confirmation-ablation.md` 180 行付近（これらのコマンドは無変更で通ること）

## 検証

worktree の `.venv` を使う（作成済み、`.[dev,db]` 導入済み）:

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit/test_ablation_compare.py tests/unit/test_research_runner.py -q
.venv/bin/pytest tests/unit tests/replay tests/failure -q
```

動作確認（CLI の引数解釈だけ。実データの run は不要）:

```bash
.venv/bin/python -m trading.backtest.ablation_compare --help
```

## 完了条件

- 上記 3 つの pytest コマンドが全て通り、`ruff check .` が指摘ゼロ
- `--with-value` / `--without-value` を省略した既存の呼び出し（`verify_comparable(with_, without, param)` /
  `report(with_, without, param, seed)` / 既存の CLI コマンド）の挙動が変わらない
- コミットはしない。変更ファイル一覧、実行した検証と結果、未確認項目を返す

## 未確定事項

- `parse_param_value` の置き場所（推奨は `research.py`。上記 1-d）
- `report` 冒頭 1 行の正確な書式（制約は上記 1-c）
