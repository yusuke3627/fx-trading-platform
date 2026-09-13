# issue #154: 研究 run の到達範囲を summary.json と比較 CLI に出し、打ち切られた run の比較を拒否する

研究 run（`python -m trading.backtest.research`）は、リスク上限や建玉上限で途中から約定しなくなっても `summary.json` に約定した期間が出ない。`period_from` / `period_to` は指定した評価期間であり、約定した期間を表すものではない。比較 CLI（`python -m trading.backtest.ablation_compare`）には、入力条件・未決済建玉・処理中コマンドの検査があるが、末尾の長い約定空白は検査しない。到達範囲（coverage）を両方に出し、末尾空白が期間長の 10% を超える run の比較を拒否する。

この coverage は、決済記録に含まれる建玉時刻の分布を表す。tick の再生完了やリスク停止の原因を直接証明する値ではない。10% の拒否条件は issue で決めた比較基準として適用し、空白だけでリスク停止と断定しない。

---

## issue 本文（全文転記）

> # 研究 run の到達範囲が出力されず、打ち切られた測定が全期間の結果として読める
>
> ## 背景
>
> H4 の 2 腕（issue #148）は、指定期間 2024-08〜2026-08 の 25 か月のうち、有効側が 10 か月、無効側が 8 か月でしか約定していなかった。しかし `summary.json` にも比較 CLI の出力にもその事実は現れず、`period_from` / `period_to` は 25 か月を主張したままだった。比較 CLI は判定規則を機械適用して「有効のまま維持（寄与あり）、差 +35.60、CI90 [31.65, 39.46]」を出したが、**2 つの腕の標本はほぼ別の期間**（有効側 2024-08〜2025-05、無効側 2024-08〜2025-01 と 2026 年の 3 か月）で、ablation の比較として成立していなかった。
>
> `COMPARABLE_FIELDS` が検査するのは入力条件（commit / dataset_hash / period など）だけで、**実際に測れた期間**は見ていない。入力が同一でも、リスク上限や建玉上限で腕ごとに異なる打ち切り方をすれば、比較は成立しない。
>
> H5（`docs/research/2026-09-10-h5-macro-confirmation-ablation.md`）でも同種の空白があった。両腕とも 2025-02〜2025-12 の 11 か月がゼロで、件数差 203 のすべてが 4 か月（うち 218 が 2024 年 9〜11 月）から出ていた。ノートはこれを「2 年間で 239 対 442」と読んでいた。
>
> ## やること
>
> ### 1. research の `summary.json` に到達範囲を出す
>
> `src/trading/backtest/report.py` の `write_report` が書く `summary.json` に `coverage` ブロックを足す:
>
> - `first_trade_at` / `last_trade_at`（約定ゼロなら null）
> - `months_with_trades` / `months_in_period`
> - `empty_months`: 約定が 1 件も無い月の一覧（`YYYY-MM`）
> - `trailing_blackout_days`: 最後の約定から `period_to` までの日数
>
> `BacktestResult.trades` と manifest の期間から算出できる。`period_from` / `period_to` は manifest にあるので、`write_report` は両方を参照できる。
>
> ### 2. `ablation_compare` が打ち切られた run を拒否する
>
> `verify_comparable` に到達範囲の検査を足す:
>
> - **末尾の空白で拒否する**: いずれかの腕の最後の約定が `period_to` より期間長の 10% を超えて前なら拒否する。打ち切りの signature であり、曖昧さがない（H4 有効側は 15 / 25 か月 = 60% で拒否、H5 は両腕とも最終月に約定があり通過する）
> - 拒否メッセージには両腕の `first_trade_at` / `last_trade_at` / 空白月数を入れ、何が起きたか読めるようにする
>
> **月ごとの約定有無の一致は要求しない。** ablation は片腕だけが約定しない月を作り得るので、それ自体は異常ではない。
>
> ### 3. 比較出力に到達範囲を常に印字する
>
> 拒否されない場合も、`report()` の manifest 節に両腕の `first_trade_at` / `last_trade_at` / `months_with_trades` を出す。読み手が「2 年分の比較だ」と誤読できないようにする。
>
> ### 4. 棄却コードの連鎖を記録する
>
> `summary.json` の `risk_rejections` は 1 決定につき失敗した全コードを並べるため、連鎖が独立した理由のように数えられる。実測では `MAX_OPEN_POSITIONS_PER_SYMBOL` と `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` の件数が完全一致していた（8,726 / 8,726、311 / 311）。建玉上限が埋まると `headroom` が 0 になり許容数量が 0 になるため、そこから最小ロット超過も必ず立つ（`src/trading/risk/engine.py:363-378`）。
>
> **コードの出し方は変えない**（`RiskEngine` の決定記録は不変条件に近い）。`report.py` の docstring に、`risk_rejections` のコードは共起し得るので単純な件数合計を独立した理由数として読まないことを 2〜3 行で書く。
>
> ## テスト
>
> - `coverage` ブロックが正しく出ること（約定ゼロ、全期間に分布、末尾に空白がある run の 3 通り）
> - `ablation_compare` が末尾の空白で拒否すること、閾値ぎりぎりで通ること
> - 月ごとの約定有無が腕で違っても、末尾に空白が無ければ通ること（ablation の正常系を拒否しない）
> - 既存テストを通すために検査を緩めない
>
> ## やらないこと
>
> - `judge` の判定規則・`MIN_TRADES`・bootstrap・`COMPARABLE_FIELDS` の変更（事前登録した測定条件）
> - `RiskEngine` / 決定記録のロジック変更
> - `config/` の変更（issue #153 が扱う）
> - 研究ノートの変更（別 issue）
> - 過去 run の再集計ツール

---

## 背景（issue に書かれていない前提）

- **レビュー対象はこの worktree の `a94ceec` と未コミットの計画。** 実装前レビューでは実コード・既存テスト・`config/base.yaml`・CI 設定と照合した。上の issue 引用は保持し、引用内のコードに合わない説明は以下の実装方針で訂正する。
- **到達範囲は `trades.csv` の `entry_at` から算出する（ユーザー判断）。** `ablation_compare` 側で到達範囲を得る経路は (a) `summary.json` の新しい `coverage` を読む、(b) 既に読んでいる `trades.csv` の `entry_at` から算出する、の 2 つあり得るが **(b) を採る**。`coverage` を持たない過去 run（H4 / H5 の実成果物は VPS 上にあり、この変更より前の engine で書かれた）でも検査が効き、`load_run` が既に `trades.csv` を全行読んでいるため追加 I/O が無い。`summary.json` の `coverage` は読まない。
- **「約定」の時刻は `entry_at`（建玉時刻）で統一する。** 新規建玉が長く途絶えた標本を検出するため、`exit_at` ではなく `entry_at` で見る。`first_trade_at` = `entry_at` の最小値、`last_trade_at` = `entry_at` の最大値、`months_with_trades` = `entry_at` の `YYYY-MM` の種類数。`summary.json` の `coverage` と `ablation_compare` の検査・印字は同じ定義を使う。
- **`BacktestResult.trades` は決済順で並ぶ（建玉順ではない）。** `src/trading/backtest/engine.py:1039-1045` が決済時に `TradeRecord` を追加するため、先頭 / 末尾要素を first / last にしない。未決済建玉は含まれず、部分決済は同じ `entry_id` / `entry_at` を持つ複数行になる。比較 CLI の既存の `open_positions_at_end` / `pending_commands_at_end` 検査は維持する。
- **期間は半開区間 `[period_from, period_to)`、時刻はブローカーの時計のラベル。** research CLI の `--to` は exclusive。`broker_label`（`research.py:102-114`）が受け入れる `+00:00` / `Z` は実 UTC への変換指定ではなく、ブローカーの壁時計を UTC として刻んだラベルである（`docs/SYSTEM_SPEC.md` §3.1–3.2）。`entry_at` も `fill.broker_time = tick.time` 由来なので、このラベル軸で月と日数を計算し、`known_at` へ変換しない。
- **比較 CLI はファイルを読み直す独立した入口なので、ファイル入力境界で検査するのは次の 3 点だけ。** (1) `trades.csv` のヘッダーに `entry_at` があること、(2) manifest に `period_from` / `period_to` の両方があること、(3) 各 `entry_at` が `period_from <= entry_at < period_to` にあること（期間外の約定を除外したり負の日数を 0 に丸めたりすると不正な標本が通るため拒否する）。時刻の naive / 非ゼロオフセット / 不正 ISO、期間の逆転・空文字といった検査は足さない。これらのファイルは `research.py` が書き、`broker_label`（`research.py:102-114`）が `+00:00` 以外を入口で弾いているので、engine 由来の成果物では起こり得ない。手編集で壊れたファイルは Python の例外で落ちてよい。
- **`write_report` は research 以外からも呼ばれる。** `src/trading/backtest/run.py:132-149` の manifest には `period_from` / `period_to` が無い（合成 tick の scripted run）。`write_report` は manifest に `period_from` と `period_to` の両方があるときだけ `coverage` を書き、無いときの `summary.json` は現状と同じにする。`run.py` は変更しない。
- **日数の表現は manifest の `warmup_days` に合わせて float。** `research.py:558` が `warmup / timedelta(days=1)` で書いているので、`trailing_blackout_days` も `(period_to - last_trade_at) / timedelta(days=1)` の float にする。
- **閾値は月数ではなく期間の時間差で判定する。** H4 / H5 の記述は背景例として扱い、10% ちょうどは通す。最終月に約定があるだけで常に通るとは限らないため、テストでは最後の建玉日時を指定する。月ごとの約定有無は比較条件にしない。
- **棄却コードの共起と因果関係を区別する。** `MAX_OPEN_POSITIONS_PER_SYMBOL` は建玉件数（`risk/engine.py:248-255`）、`headroom` は `max_units_per_symbol` と保有数量（同 342-363 行）で決まる。`config/base.yaml` でも別の設定である。件数上限に達しても数量の余裕があれば最小ロット違反は出ない。両方の上限に達した場合などに共起し得る、と注記する。
- 既存の `ablation_compare` テストの `backtest_result`（`tests/unit/test_ablation_compare.py:99-132`）は 2026-08-13 の時刻を、manifest は 2026-01-01〜2026-02-01 を使っており不整合。さらに `PERIOD_FROM + timedelta(hours=index + 1)` へ直すだけでは、2〜3 件の既存正常系に約 99.6〜99.7% の末尾空白が生じて拒否される。既定 fixture は**期間内かつ末尾空白 10% 以下**に揃え、空白を検査するケースだけ明示的な建玉時刻を渡す。

---

## 変更対象ファイル一覧

| ファイル | 変更 |
| --- | --- |
| `src/trading/backtest/run_coverage.py` | **新規**。到達範囲の算出（`RunCoverage` と `run_coverage()`）。`report.py` と `ablation_compare.py` の両方が使う |
| `src/trading/backtest/report.py` | `summary.json` に `coverage` を追加。モジュール docstring に `risk_rejections` の共起についての注記 |
| `src/trading/backtest/ablation_compare.py` | `RunArtifacts.entry_ats` 追加、CSV・manifest の時刻と期間の境界検証、`verify_comparable` に末尾空白の検査、`report()` に到達範囲の印字、閾値定数 |
| `tests/unit/test_run_coverage.py` | **新規**。`run_coverage()` の単体テスト |
| `tests/unit/test_ablation_compare.py` | fixture の期間・末尾空白の整合、`RunArtifacts` の引数追加、入力不備 / 末尾空白 / 閾値境界 / 月ズレ許容 / 約定ゼロ / 過去形式 / summary と印字のテスト |

マイグレーション: **なし**（DB スキーマは触らない）。

---

## 実装計画

### 1. `src/trading/backtest/run_coverage.py`（新規）

```python
@dataclass(frozen=True)
class RunCoverage:
    first_trade_at: datetime | None
    last_trade_at: datetime | None
    months_with_trades: int
    months_in_period: int
    empty_months: tuple[str, ...]          # "YYYY-MM"、期間順
    trailing_blackout_days: float | None   # 約定ゼロなら None


def run_coverage(
    entry_ats: Iterable[datetime], period_from: datetime, period_to: datetime
) -> RunCoverage:
    ...
```

- `months_in_period`: `period_from` の月初（`replace(day=1, hour=0, minute=0, second=0, microsecond=0)`）から 1 か月ずつ進め、**月初 `< period_to` である月**を数える。半開区間なので `period_to` が月初ちょうど（例 `2026-02-01T00:00:00+00:00`）ならその月は含まない（2026-01-01〜2026-02-01 は 1 か月、2024-08-01〜2026-09-01 は 25 か月）。
- `entry_ats` は一度だけ走査する。`write_report` はジェネレーターを渡すため、月集合の作成後に同じ Iterable を `min()` / `max()` で再走査しない。走査中に月の集合と最小・最大時刻を更新する。計算量は約定行数と期間月数に比例し、入力のソートは不要。
- `months_with_trades`: 同じ走査で得た `f"{at:%Y-%m}"` の集合の要素数。
- `empty_months`: 期間内の月ラベルのうち、`entry_ats` の月に含まれないものを期間順に並べた tuple。
- `first_trade_at` / `last_trade_at`: 全要素の最小値 / 最大値。`entry_ats` が空なら両方 `None`。
- `trailing_blackout_days`: `(period_to - last_trade_at) / timedelta(days=1)`。`entry_ats` が空なら `None`。
- 引数は正の期間と、その期間内の `+00:00` の建玉時刻を前提とする。research は生成時、比較 CLI は下記のファイル入力境界でこの条件を保証する。内部の算出関数に naive の補正や期間外データの切り捨てを入れない。
- 月の加算は `datetime` の `year` / `month` を進めるだけでよい（外部ライブラリを足さない）。

### 2. `src/trading/backtest/report.py`

- `write_report`（`report.py:23-83`）で、`manifest` に `period_from` と `period_to` の両方があるときだけ `run_coverage((t.entry_at for t in result.trades), datetime.fromisoformat(manifest["period_from"]), datetime.fromisoformat(manifest["period_to"]))` を計算し、`summary.json` の `risk_rejections` の後に `coverage` を足す:

  ```json
  "coverage": {
    "first_trade_at": "2024-08-05T01:00:00+00:00",   // 約定ゼロなら null
    "last_trade_at":  "2025-05-20T10:00:00+00:00",   // 約定ゼロなら null
    "months_with_trades": 10,
    "months_in_period": 25,
    "empty_months": ["2025-06", "2025-07", ...],
    "trailing_blackout_days": 468.58                  // 約定ゼロなら null
  }
  ```

  既存の `_scalar`（`report.py:90-95`）は tuple を `str()` にしてしまうので、`empty_months` は `list(...)` に、datetime は `isoformat()` に明示変換して dict を組む（`asdict(coverage)` を `_jsonable` に通すだけでは `empty_months` が文字列になる）。
- manifest に期間が無いとき（`run.py` の scripted run）は `coverage` キーを書かない。既存の `summary.json` の他のキー（`symbol` / `metrics` / `risk_rejections`）は変えない。
- モジュール docstring（`report.py:1-12`）に、coverage は決済記録に含まれる建玉時刻の分布であることを記す。`risk_rejections` については「1 決定に失敗した全コードを記録するため、コードは共起し得る。建玉件数上限と数量上限の両方に達した場合などに最小ロット違反も現れる。コード別件数の合計を独立した棄却理由数と解釈しない」という趣旨を 2〜3 行で書く。件数上限から `headroom = 0` が必ず導かれるとは書かず、`RiskEngine` 側は触らない。

### 3. `src/trading/backtest/ablation_compare.py`

- 閾値定数をモジュール定数として 1 か所に置く（`MIN_TRADES` の近く、`ablation_compare.py:36`）:

  ```python
  # 末尾空白（最後の建玉から period_to まで）が期間長のこの割合を超える腕は、
  # 途中から建玉しなくなった打ち切り run とみなして比較しない。実測では H4
  # （failed_spike_reversal の時間切れ決済 ablation）の有効側が 25 か月中 15 か月
  # = 60% の末尾空白で止まっており、H5 は両腕とも最終月まで建玉があった。
  # その間を分ける値として 10% を採る。空白だけで停止原因は判定しない。
  TRAILING_BLACKOUT_MAX_RATIO = 0.1
  ```

  由来（H4 の 60%）は必ずコメントに残す。issue 番号だけを書いた「〜で定めた」という形にしない。

- `RunArtifacts`（`ablation_compare.py:72-76`）に `entry_ats: list[datetime]` を **既定値なしで** 追加する（4 番目の位置引数）。
- `load_run`（`ablation_compare.py:90-128`）の既存の行ループで `entry_at` を読み、`RunArtifacts` に渡す。
  - ヘッダー段階で `entry_id` と `entry_at` を検査する。既存の `no entry_id` メッセージと検査順は維持し、`entry_id` はあるが `entry_at` が無い場合は `trades.csv has no entry_at; re-run with the current engine` の趣旨で拒否する。データ 0 行でもヘッダーを検査する。履歴上 `entry_at` は `entry_id` より前から存在する（`4945335^` の `report.py`）ため、同時追加という前提は使わない。
  - 各行の `entry_at` は `datetime.fromisoformat(row["entry_at"])` で読む。それ以上の形式検査（空値・naive・オフセット）は足さない（上記「背景」）。
  - partial close は行ごとに時刻を集めてよい。同じ `entry_id` の正常な決済行は同じ `entry_at` であり、重複は最小・最大・月集合を変えない。PnL の `entry_id` 集約、carry 加算、CSV 行数と `metrics.trades` の一致検査は維持する。
- manifest から期間を読む小さな関数（例 `_period(manifest: dict) -> tuple[datetime, datetime] | None`。`period_from` / `period_to` のどちらかが無ければ `None`、あれば `datetime.fromisoformat` した組）を置き、`verify_comparable` と `report()` から使う。`try` / `except` で例外を `reasons` に変換する構造にはしない。
- `verify_comparable`（`ablation_compare.py:131-200`）:
  - 既存の入力条件・未決済建玉・処理中コマンド・パラメータ検査を保つ。既存の `for arm, run in (("with", with_), ("without", without))` ループ内で、腕ごとに次の順で検査する:
    1. `_period(run.manifest)` が `None` なら `"{arm} manifest has no period_from/period_to; re-run with the current engine"` の趣旨を `reasons` に足し、その腕の以降の検査はしない
    2. `run.entry_ats` のうち `period_from <= entry_at < period_to` を外れるものがあれば、件数と最初の 1 件を含めて `reasons` に足し（開始ちょうどは通す。開始前・終端ちょうど・終端後は外れ）、その腕の末尾空白検査はしない
    3. 上記を通った腕だけ `run_coverage(run.entry_ats, period_from, period_to)` を求め、末尾空白を検査する
  - 片腕でも 1 か 2 に該当すれば比較自体は必ず拒否される（`reasons` が空でないため）。
  - 既存の `for arm, run in (("with", with_), ("without", without))` ループ内で、**その腕に約定が 1 件以上あり**、かつ `period_to - last_trade_at > TRAILING_BLACKOUT_MAX_RATIO * (period_to - period_from)`（`timedelta` 同士の比較。`timedelta * float` は Python が直接サポートする）なら `reasons` に足す。等号は通す（「10% を超えて前」）。
  - 約定ゼロの腕は末尾空白の検査をしない。既存どおり `judge` が `UNDECIDED_SAMPLE`（標本不足）を返す（`judge` は変更しない）。
  - 拒否メッセージの内容（形式は既存の `reasons` の 1 行に合わせる。例）:

    ```
    with last_trade_at=2025-05-20T10:00:00+00:00 is 468.6 days before period_to=2026-09-01T00:00:00+00:00 (61.6% of the period, limit 10%); the trailing gap exceeds the comparison limit. coverage: with first_trade_at=2024-08-05T01:00:00+00:00 last_trade_at=2025-05-20T10:00:00+00:00 empty_months=15/25; without first_trade_at=2024-08-02T08:00:00+00:00 last_trade_at=2026-08-27T03:00:00+00:00 empty_months=17/25
    ```

    必須なのは「どの腕が」「最後の約定がいつで `period_to` からどれだけ前か（日数と割合）」「両腕それぞれの `first_trade_at` / `last_trade_at` / 空白月数（`empty_months` の件数 / `months_in_period`）」。文言は既存メッセージの英語に合わせる。
    片腕の coverage が上記 1 か 2 で算出できない場合は、その腕を `coverage unavailable` と示す（理由は当該腕の `reasons` 行に出ている）。片腕が約定ゼロなら first / last は `None`、空白月数は全月として表示し、相手腕の末尾空白拒否を妨げない。
- `report()`（`ablation_compare.py:267-315`）: 先頭で `verify_comparable` が通っているので、両腕とも `_period` が `None` でなく `entry_ats` は期間内にある前提で `run_coverage` を呼んでよい。`for label, run in (("with", with_), ("without", without))` の manifest 節（同 282-287 行）の直後、各腕の `MANIFEST_FIELDS` 行に続けて同じ書式 `f"  {field:<22} {value}"` で次を印字する:

  ```
  with:
    run_id                 ...
    ...
    param_overrides        {...}
    first_trade_at         2024-08-05T01:00:00+00:00
    last_trade_at          2025-05-20T10:00:00+00:00
    months_with_trades     10/25
  ```

  `first_trade_at` / `last_trade_at` は `isoformat()`、約定ゼロなら `None`。`months_with_trades` は `"{months_with_trades}/{months_in_period}"`。`MANIFEST_FIELDS` / `COMPARABLE_FIELDS` の tuple は変更しない（到達範囲は manifest の項目ではない）。
- `judge` / `MIN_TRADES` / `arm_summary` / `difference_interval` / `COMPARABLE_FIELDS` / `MANIFEST_FIELDS` / `KEEP` 等の文言は変更しない。

### 4. テスト

`tests/unit/test_run_coverage.py`（新規）:

- 約定ゼロ: `first_trade_at` / `last_trade_at` / `trailing_blackout_days` が `None`、`months_with_trades == 0`、`empty_months` が期間の全月。
- 全月に約定: `empty_months == ()`、`months_with_trades == months_in_period`、`trailing_blackout_days` が最後の約定から `period_to` までの日数（float）。
- 末尾に空白: 例 2026-01-01〜2026-07-01（6 か月）で 1〜2 月にだけ約定 → `empty_months == ("2026-03", "2026-04", "2026-05", "2026-06")`、`months_with_trades == 2`、`trailing_blackout_days` が想定値。
- `period_to` が月初ちょうどのときその月を数えない（2026-01-01〜2026-02-01 は `months_in_period == 1`）。
- 年またぎ・うるう年・月途中の開始 / 終了を扱う（例: 2024-12-31〜2025-02-01 は 2 か月、2024-02-01〜2024-03-01 は 29 日、終了が月途中ならその月を含む）。
- `entry_ats` を空ジェネレーターと、重複・建玉順でない値を含むジェネレーターで渡し、first / last・月集合・日数が正しいことを確認する。リストを複数回走査できることに依存したテストにしない。

`tests/unit/test_ablation_compare.py`:

- fixture の整合: `PERIOD_FROM = datetime(2026, 1, 1, tzinfo=UTC)` / `PERIOD_TO = datetime(2026, 2, 1, tzinfo=UTC)` を同じテストモジュールに置き、manifest の既定値にも使う。既存の少数取引 fixture は `entry_at = PERIOD_TO - timedelta(hours=len(pnls) - index + 1)`、`exit_at = entry_at + timedelta(hours=1)` とすれば、時刻が期間内に収まり最終建玉は終了 2 時間前になる。既存 round-trip と `test_main_compares_another_parameter_with_requested_seed` が元の PnL・carry・seed・判定のアサーションを保って通るようにする。
- `backtest_result` に `entry_ats: list[datetime] | None = None`、`manifest` に `period_from` / `period_to` のキーワード引数を足す。明示した時刻と取引数の対応は `zip(..., strict=True)` など既存の作法で保つ。部分決済 fixture は `entry_id` だけでなく `entry_at` も共有させ、集約後の標本数は 2 件のまま、coverage も建玉時刻の集合と一致することを確認する。
- `RunArtifacts(...)` の全呼び出しを検索し、約定なしの直接構築には `entry_ats=[]` を追加する。PnL があるケースには対応する時刻を渡し、空配列で末尾空白検査を回避しない。既存のアサーションと不変条件は変えない。
- `summary.json` の `coverage`（issue の 3 通り）: `write_report` で書いた `summary.json` を読み、約定ゼロ（`first_trade_at` / `last_trade_at` / `trailing_blackout_days` が `null`、`empty_months` が全月）、全月に分布（`empty_months == []`）、末尾に空白（`empty_months` と `trailing_blackout_days` の値）を検証する。期間の無い manifest（`period_from` / `period_to` を削ったもの）では `summary.json` に `coverage` キーが無いことも 1 本で固定する。
- `load_run` の round-trip で、CSV の `entry_ats`、`summary.json.coverage`、比較出力の first / last・約定月数が一致すること。月が違う `entry_at` / `exit_at` を含め、決済時刻で集計しないことも固定する。
- 末尾の空白で拒否: 期間 2026-01-01〜2026-07-01、拒否側の建玉は 1 月 15 日と 2 月 15 日、通過側の最終建玉は 6 月 30 日 12:00 とする。with / without のどちらが拒否側でも検査が効き、`SystemExit` に腕名・両腕の first / last・拒否側の `empty_months=4/6`・日数と割合が出ることをパラメータ化して確認する。
- 閾値ぎりぎりで通る: 期間 10 日（2026-01-01〜2026-01-11）で最後の建玉が 2026-01-10（10% ちょうど）なら通る。同じ入力で一方の最終建玉を 1 秒前へずらすと拒否される、を対にする。
- 月ごとの約定有無が腕で違っても通る: 期間 2026-01-01〜2026-07-01 で with 腕は 1・2・6 月、without 腕は 1・3・6 月に建玉を置き、両腕の最終建玉を 6 月 30 日 12:00 にする。`verify_comparable` が通り、`report()` の各腕に `months_with_trades` が `3/6`、first / last が正しく出ること。空白の数は固定文字列の手入力ではなく既存の書式で検証する。
- 約定ゼロ: 既存の空標本の `verify_comparable` 正常系を維持する。追加で、片腕ゼロ・両腕ゼロの実ファイルを `write_report` → `load_run` → `report` に通し、first / last が `None`、約定月数が `0/N`、最終判定が `UNDECIDED_SAMPLE` であることを確認する。相手腕に 10% 超の末尾空白がある場合は、約定ゼロの腕があっても拒否されること。
- 過去形式: `summary.json` から `coverage` だけを取り除いても、CSV 由来の末尾空白を拒否し、正常な標本は表示できること（1 本）。VPS の成果物を再集計するツールは作らない。
- CSV 境界: `entry_id` があり `entry_at` が無いヘッダーを 0 行で拒否する（1 本）。既存の `no entry_id` テストは維持する。`entry_at` の値形式（空セル・naive・オフセット）のテストは書かない（検査しないため）。
- manifest と期間境界: `period_from` / `period_to` のどちらかが無い腕を拒否する（両腕とも無くても `COMPARABLE_FIELDS` の一致だけで通らないことを 1 本で確認）。正常な末尾約定を別に置いたうえで、検査対象の建玉が開始ちょうどなら通り、開始前・終了ちょうど・終了後なら拒否することをパラメータ化して確認する。
- 複合した不備: 片腕の期間が無く、もう片腕に末尾空白がある場合も、例外で落ちず `runs are not comparable` と両方の理由が出ること（1 本）。

テストデータに実在する人物・団体名を使わない（既存 fixture の `"USDJPY"` / `"post_event_failed_breakout"` 等はそのまま）。

---

## 参考にすべき既存実装

- `src/trading/backtest/report.py:23-83` — `write_report`。`summary.json` を組んでいる箇所は 28-40 行
- `src/trading/backtest/report.py:86-99` — `_jsonable` / `_scalar` / `_dumps`
- `src/trading/backtest/engine.py:185-207` — `TradeRecord`（`entry_at` / `exit_at` は tz-aware datetime）
- `src/trading/backtest/engine.py:210-218` — `BacktestResult`
- `src/trading/backtest/engine.py:1039-1045` — `trades` は決済時に追加される（決済順）
- `src/trading/backtest/research.py:102-114` — `broker_label`（`+00:00` 固定）、`372-377` — `--to` は exclusive、`556-558` — manifest の `period_from` / `period_to` / `warmup_days`
- `src/trading/backtest/run.py:132-149` — 期間を持たない manifest（`coverage` を書かない側の呼び出し元）
- `src/trading/backtest/ablation_compare.py:36-70` — 定数、`72-76` — `RunArtifacts`、`90-128` — `load_run` の行ループ、`131-200` — `verify_comparable` の `reasons` の組み方、`261-264` — `_manifest_value`、`282-287` — `report()` の manifest 節
- `src/trading/risk/engine.py:248-255` / `342-378`、`config/base.yaml:46-55` — 建玉件数上限と数量上限は別条件。docstring 注記の根拠（触らない）
- `tests/unit/test_ablation_compare.py:87-96` — `run_metrics`、`99-132` — `backtest_result`、`135-169` — `manifest`、`172-199` — `write_report` → `load_run` → `report` の round-trip テストの作法
- `tests/support.py:29-33` — `T0` / `at()`（tz-aware UTC）

---

## 完了条件（実行可能なコマンド）

worktree の絶対パス `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-154-run-coverage` で実行する。

```bash
.venv/bin/ruff check .                                   # 無指摘
.venv/bin/pytest tests/unit/test_run_coverage.py tests/unit/test_ablation_compare.py -q
.venv/bin/pytest tests/unit tests/replay tests/failure -q # ローカルで常時実行する範囲
```

最後のコマンドに broker / integration テストは含まれない。コミット・PR を担当する工程では、既存の lefthook（pre-commit: ruff、pre-push: `.venv/bin/pytest -q`）と CI を通す。CI の通常 job は `pytest -q`、integration job は専用 PostgreSQL に全 migration を適用して `pytest tests/integration -q` を実行する（`.github/workflows/ci.yml`）。今回の変更のために設定や DB スキーマは変えない。

実装後は新規出力に coverage が加わり、比較 CLI は新旧いずれの summary でも CSV から検査するため、過去成果物への backfill は不要。成果物を出力する側と比較する側を同時に更新する必要はないが、拒否条件を効かせるには更新後の比較 CLI を使う。

### この計画レビューで確認したこと・未確認事項

- `a94ceec` の既存 `tests/unit/test_ablation_compare.py` は **26 passed**。提案されていた月初 fixture の末尾空白率と、件数上限だけに達した場合は最小ロット違反が出ないことを、既存コードと一時的な Python 実行で確認した。
- 新規 coverage の実装・追加テストはまだ無いため、その成功を示す結果ではない。上記の lint・テスト一式は実装後の完了条件。
- H4 / H5 の VPS 上の実成果物はこのレビューでは未確認。引用中の件数・日付と実データの一致、更新後 CLI での H4 拒否 / H5 通過は未検証として残す。CSV を使う方針と 10% の閾値は計画の決定を維持する。

---

## やらないこと

- `judge` の判定規則・`MIN_TRADES`・bootstrap（`BOOTSTRAP_SAMPLES` / `BOOTSTRAP_LEVEL`）・`COMPARABLE_FIELDS`・`MANIFEST_FIELDS` の変更（事前登録した測定条件）
- 月ごとの約定有無の一致を腕どうしに要求すること（ablation の正常系を拒否しない）
- `summary.json` の `coverage` を `ablation_compare` が読むこと（`trades.csv` から算出する）
- `RiskEngine`（`src/trading/risk/engine.py`）と決定記録のロジック変更
- `run.py` / `research.py` / `engine.py` の変更
- `config/` の変更（issue #153 が扱う）、`docs/` 配下（ADR・研究ノート）の変更（issue #153 / #155 が並行して触っている）
- 過去 run の再集計ツール
- 周辺のリファクタ・無関係な整形・追加の抽象化。`git commit` もしない（コミットと PR は Claude 側が行う）

---

## プロジェクト規約の転記（`.claude/rules/*.md` 由来）

- 編集はこの worktree 内だけで行う。`/Users/yusuke/Products/fx-trading-platform`（メインリポジトリ）には触らない
- 変更範囲は `src/trading/backtest/` と `tests/` に限定する
- 検索は `grep` ではなく `rg` を使う
- 金額・数量・価格に float を使わない（本件で float になるのは日数の `trailing_blackout_days` と割合の閾値だけ）
- dataclass は frozen、引数・共有オブジェクトを破壊しない（`RunArtifacts` / `RunCoverage` は frozen）
- 検証はシステム境界（ファイル入力 = manifest / trades.csv）だけで行い、内部関数間に防御的分岐やフォールバックを足さない
- コメントは日本語を優先してよいが、既存の英語 docstring / メッセージがあるファイルではその言語に合わせてよい。WHAT を説明するコメントや「issue #154 のために追加」のようなコミット文脈のコメントは書かない（閾値の由来のコメントは WHY なので可）
- テストデータに実在する人物・団体名を使わない
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない（本件は不変条件に触れない）
- UI の変更はない
