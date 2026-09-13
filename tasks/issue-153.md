# issue #153: 研究リプレイに実運用の損失停止が掛かり測定が打ち切られる（backtest 環境で口座水準の停止を外す）

この計画は、会話履歴を持たない実装者（Codex）が単独で実装できるように書いている。
1 節は依頼時に受領した issue 本文の記録として残す。実コードとの照合で補正した説明・手順は 2 節以降に記載しており、実装時はそちらを優先する。

- リポジトリ: `yusuke3627/fx-trading-platform`
- ブランチ: `fix/issue-153-backtest-risk-halts`（origin/main `a94ceec` から作成）
- 作業ディレクトリ: この worktree のみ。メインリポジトリは編集しない
- コミットはしない（コミット・PR は Claude 側が行う）

## 1. 要件（issue #153 本文の全文転記）

### 背景

H4（`failed_spike_reversal` の時間切れ決済 ablation、issue #148）の 2 腕を 2024-08〜2026-08 の 25 か月で流したところ、**有効側は 2025-05 で取引が止まり、残り 15 か月の約定がゼロ**だった。棄却理由の内訳は `HWM_DRAWDOWN_WITHIN_LIMIT` が 6,376 件で支配的。

`RiskEngine` は含み損益込みの資産が最高値から `high_water_mark_drawdown_halt_pct`（3.00）下がると新規建玉を承認しない（`src/trading/risk/engine.py:273-276`）。最高値は単調非減少で、新規建玉ができなければ資産も動かないため、一度閾値を割ると期間終端まで復帰しない。実測の最終資産 970,793 は最高値から 3.007% 下で、閾値をわずかに超えたまま固定されていた。

H5（`docs/research/2026-09-10-h5-macro-confirmation-ablation.md`）の 4 本も最大ドローダウンが 30,115〜30,251 に揃っており、同じ壁に当たっている。

`config/base.yaml` はこの 3 つの停止閾値について「Beginner upper bounds for entering micro live; never a backtest optimization target」と明記している。運用開始のための上限であって、エッジ測定を打ち切るために置いたものではない。現状はその意図とずれている。

### 決定（ユーザー判断済み）

**研究リプレイでは口座水準の損失停止を適用しない。** 戦略のエッジそのものを測り、実運用のリスク上限下でどうなるかはエッジが確認できてから別に測る。

### やること

1. `config/backtest.yaml` の `risk` に研究用の値を置き、口座水準の 3 つの損失停止を実質無効化する:
   - `daily_loss_halt_pct: 100.00`
   - `rolling_24h_loss_halt_pct: 100.00`
   - `high_water_mark_drawdown_halt_pct: 100.00`
2. `docs/adr/` に ADR を追加する（設計は v2.0 で凍結済みのため本文改訂ではなく ADR）。記録する内容:
   - 決定: backtest 環境では口座水準の損失停止（daily / rolling24h / HWM drawdown）を適用しない
   - 理由: 上記の背景。実測で H4 の有効側が 25 か月中 10 か月で打ち切られ、H5 の両腕も同じ閾値で止まった
   - 適用範囲: 口座水準の損失停止のみ。**銘柄あたり建玉上限（`max_open_positions_per_symbol`）、最小ロット超過（`MINIMUM_BROKER_SIZE_EXCEEDS_RISK`）、イベントモード、spread gate、session gate は研究でも掛けたまま**にする。これらは戦略の性質と執行可能性そのものであって、リスク許容度の設定ではない
   - 終端の歯止め: 損失停止を外しても、sizing が equity 比のため資産が減ると許容数量が縮み、最小ロット（1,000 通貨）を割った時点で `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` が取引を止める。無制限に建て続ける経路は残らない
   - 影響: 過去の研究 run（H5 を含む）は打ち切られた測定であり、本 ADR 以降の run と直接比較できない
3. テスト（`tests/unit/test_config.py` の作法に合わせる）:
   - `backtest` 環境をロードすると 3 つの停止閾値が緩和値になること
   - `shadow` / `demo` / `micro_live` / `production` の各環境では `base.yaml` の値（0.75 / 1.00 / 3.00）のままであること

### やらないこと

- `config/base.yaml` / `shadow.yaml` / `demo.yaml` / `micro_live.yaml` / `production.yaml` の変更
- `RiskEngine` / `limits.py` のロジック変更（設定値だけで足りる）
- 建玉上限・最小ロット・イベントモードの緩和
- 研究ノートの変更（別 issue）

## 2. 依頼者からの補足

- この変更は H4 の再測定（2 腕を 25 か月で流し直す。1 腕 14 時間）を始めるための前提。急ぐが、範囲は issue のとおりに絞る
- ADR 番号は **ADR-037**（`docs/adr/` の既存最大は `ADR-036-horizon-exit.md`）。作成直前に `ls docs/adr | sort | tail -3` で重複がないことを確認する
- ADR の書式は `docs/adr/ADR-036-horizon-exit.md` に合わせる（下記 4 節末尾に転記）
- 閾値の型は `Decimal`（`RiskConfig` の定義を参照）
- UI は無い
- 3 閾値を `100.00` にする方針と `src/` を変更しない範囲は維持する。ただし `100.00` は完全な無効化フラグではないこと、低資産時の注文抑止は RiskEngine の拒否コードとして記録されない経路もあることを ADR に明記する（5 節）
- H4 の集計値は issue #153 の報告であり、この worktree には元 run の成果物がない。H5 の参照ノートから確認できるのは 2 本の run と最大ドローダウンの値までで、停止原因・停止期間は記載がない。ADR では「issue #153 の報告」として記し、独立に再集計した値とは書かない

## 3. 変更対象ファイル

| ファイル | 変更 |
| --- | --- |
| `config/backtest.yaml` | `risk:` セクションに 3 つの停止閾値（100.00）を追加し、研究用の緩和である旨のコメントを付ける |
| `docs/adr/ADR-037-backtest-account-level-loss-halts.md` | 新規。内容は 5 節 |
| `tests/unit/test_config.py` | テスト 2 本を追加（6 節） |

- マイグレーション: なし
- コード（`src/`）の変更: なし。設定値・ADR・テストのみ
- 上記 3 ファイル以外は変更しない

## 4. 参考にすべき既存実装

読み込みと合成:

- `src/trading/config.py:165-172` `_deep_merge` — base の dict と overlay の dict を再帰的に合成する。`risk` のようにネストした dict は key 単位で上書きされるので、overlay に 3 キーだけ書けば他の `risk` 値は base のまま残る
- `src/trading/config.py:175-188` `load_config(environment, config_dir)` — `base.yaml` と `<environment>.yaml` を読んで合成し `AppConfig` を返す。`ENVIRONMENTS = ("backtest", "demo", "shadow", "micro_live", "production")`（`src/trading/config.py:22`）
- `src/trading/config.py:156` `AppConfig.risk: RiskConfig`

閾値の定義と使われ方:

- `src/trading/risk/engine.py:65-67` `RiskConfig` の 3 フィールド。型は `Decimal`、既定は `"0.75"` / `"1.00"` / `"3.00"`（単位はパーセント。0.75 = 0.75%）。`100.00` を禁止する上限 validator はない
- `src/trading/risk/engine.py:264-276` `DAILY_LOSS_WITHIN_LIMIT` / `ROLLING_24H_LOSS_WITHIN_LIMIT` / `HWM_DRAWDOWN_WITHIN_LIMIT` の 3 チェック。いずれも「損失率 < 閾値」で通す
- `src/trading/risk/limits.py:44-69` 損失率は基準資産に対する `(baseline - equity) / baseline * 100`。daily は JST 日初、rolling は 24 時間前、HWM は最高資産を基準にする。正の基準資産に対して equity がゼロなら 100%、負なら 100% 超になる。基準がないか非正なら 0% を返す
- `src/trading/risk/engine.py:306-381` sizing は equity 比。stop 距離・換算・数量刻み・銘柄の数量上限・イベントモードで許容数量が決まり、broker 最小数量未満の注文を `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` で拒否する。判定は注文ごとで、永続的な停止状態を保持する実装ではない
- `src/trading/portfolio/manager.py:104-108` RiskEngine の前にも sizing があり、数量を `volume_step` で丸めた結果が 0 以下なら intent を作らない。`src/trading/backtest/engine.py:833-853` は intent が空なら RiskEngine を呼ばないため、この場合は最小ロットの拒否コードも記録されない

実際の呼び出し経路:

- `src/trading/backtest/research.py:400,487-488` と `src/trading/backtest/run.py:94,112-113` は `load_config(args.env)` の `config.risk` を `BacktestEngine` に渡し、同 engine の `_wire` が `RiskEngine(self._risk_config, ...)` を作る。`--env backtest` の研究 CLI と合成データ CLI の両方に効く。直接 `RiskConfig()` を作る呼び出し元の既定値は変わらない

設定ファイル:

- `config/base.yaml:25-26` コメント "Beginner upper bounds for entering micro live; never a backtest optimization target. Percentages are in percent (0.05 = 0.05%)."
- `config/base.yaml:63-65` の 3 値（0.75 / 1.00 / 3.00）
- `config/backtest.yaml` — 現在の `risk:` は `trading_enabled: true` のみ。先頭コメントは「trading_enabled は Risk Engine のゲートなので backtest では有効にする」の趣旨。ここに 3 キーを足す
- `config/shadow.yaml` / `demo.yaml` / `micro_live.yaml` / `production.yaml` — いずれも `risk:` セクションはあるが 3 つの停止閾値は書いていない（base の値がそのまま効く）。**変更しない**

設計文書:

- `docs/SYSTEM_SPEC.md` §1（anchor `#s1`、8 行目付近）: 発行後の仕様変更は本文改訂ではなく ADR 追加で行う
- `docs/SYSTEM_SPEC.md` §4.3（anchor `#s4-3`、223-229 行目）: 「損失制限は永続化した account_snapshots から、JST暦日、rolling 24h、high-water-mark drawdown の3窓で評価する。〔…〕初期上限は config/base.yaml の daily 0.75%、rolling24h 1.00%、HWM 3.00% とし、初心者向け Micro Live 上限を backtest 最適化の対象にしない。」
- `docs/SYSTEM_SPEC.md` §9（anchor `#s9`、885 行目）: Backtest と再現性
- `docs/research/2026-09-10-h5-macro-confirmation-ablation.md`「データ」「結果」: 確認あり／なしの 2 run と最大ドローダウン（30,115.26 / 30,250.84）を記載。棄却理由や最終約定時刻の記載はない

テストの作法:

- `tests/unit/test_config.py:1-16` import と `CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"`。`Decimal` は既に import 済み
- `tests/unit/test_config.py:67-77` `test_micro_live_overlay_caps_and_enables` — `load_config("micro_live", CONFIG_DIR)` して `config.risk.*` を assert する形。「Base parameters survive the overlay merge.」のように、なぜその assert があるかを短いコメントで添える
- `tests/unit/test_config.py:95-99` `test_backtest_enables_risk_gate_for_simulated_orders` — backtest overlay の既存テスト。新しいテストはこの直後に置く
- `tests/unit/test_config.py:40-47` `test_position_caps_stay_single_in_live_overlays` — 複数環境を `for env in (...)` で回す既存の書き方

ADR の書式（ADR-036 と同じ）:

- 1 行目 `# ADR-037: <日本語タイトル>`
- 3 行目 `**Status:** Accepted (2026-09-14)`
- 見出しは `## Context` / `## Decision` / `## Consequences` の 3 つ（英語見出し、本文は日本語）
- Decision は番号付きで、各項目の 1 文目を太字にする
- SYSTEM_SPEC への参照は `[§4.3](../SYSTEM_SPEC.md#s4-3)` の形式

## 5. ADR-037 に書く内容

ファイル名: `docs/adr/ADR-037-backtest-account-level-loss-halts.md`

タイトル: `# ADR-037: backtest 環境では口座水準の損失停止を適用しない`

### Context に含めること

- [§4.3](../SYSTEM_SPEC.md#s4-3) は損失制限を daily / rolling 24h / HWM drawdown の 3 窓で評価し、初期上限 0.75% / 1.00% / 3.00% を「初心者向け Micro Live 上限」として `config/base.yaml` に置き、backtest 最適化の対象にしないと定める。`config/base.yaml:25-26` のコメントも同じ趣旨
- この 3 値は `config/backtest.yaml` で上書きされておらず、研究リプレイにもそのまま掛かっていた
- H4（issue #148）について issue #153 が報告した実測: 2024-08〜2026-08 の 25 か月で、時間切れ決済の有効側は 2025-05 で取引が止まり、残り 15 か月の約定がゼロ。棄却理由は `HWM_DRAWDOWN_WITHIN_LIMIT` が 6,376 件で支配的。最終資産 970,793 は最高値から 3.007% 下で閾値をわずかに超えたまま固定されていた
- なぜ復帰しないか: HWM 判定は評価のたびに現在 equity と最高値から計算され、停止を保持する状態はない。建玉が残っていれば含み損益の回復で再承認され得るが、flat で新規建玉が拒否され続けると資産が動かないため、一度閾値を割ると期間終端まで拒否が続く（`src/trading/risk/engine.py:273-276`、`src/trading/risk/limits.py:67-69`）
- H5（`docs/research/2026-09-10-h5-macro-confirmation-ablation.md`）の run も最大ドローダウンが 30,115〜30,251 に揃っており、issue #153 は同じ閾値で止まったと報告している（ノート自体には棄却理由の記載がない）
- これらの上限は運用開始のための上限であって、エッジ測定を打ち切るために置いたものではない。現状はその意図とずれている
- SYSTEM_SPEC は v2.0 で凍結されており、[§1](../SYSTEM_SPEC.md#s1) は発行後の仕様変更を本文改訂ではなく ADR 追加で行うと定める

### Decision に含めること（番号付き）

1. **研究リプレイ（backtest 環境）では口座水準の損失停止を適用しない。** `config/backtest.yaml` の `risk` に `daily_loss_halt_pct` / `rolling_24h_loss_halt_pct` / `high_water_mark_drawdown_halt_pct` を `100.00` で置き、実質無効化する。戦略のエッジそのものを測り、実運用のリスク上限下でどうなるかはエッジが確認できてから別に測る。損失率の計算と比較は残るため、正の基準資産に対して equity がゼロ以下になった場合だけは `< 100` を満たさず拒否するが、これは無効化のための便宜値であってリスク許容度ではない
2. **適用範囲は口座水準の損失停止（daily / rolling 24h / HWM drawdown）の 3 つだけ。** 銘柄あたり建玉上限（`max_open_positions_per_symbol`）・portfolio 建玉上限、銘柄の数量上限、1 取引あたりのリスク比率、portfolio stop-risk・通貨 exposure の上限、最小ロット超過（`MINIMUM_BROKER_SIZE_EXCEEDS_RISK`）、イベントモード、spread gate、session gate は研究でも掛けたままにする。これらは戦略の性質と執行可能性そのものであって、リスク許容度の設定ではない
3. **`RiskEngine` / `limits.py` のロジックは変えない。** 設定値だけで足りる。`base.yaml` の 3 値と、shadow / demo / micro_live / production の各 overlay も変えない（これらの環境では 0.75 / 1.00 / 3.00 のまま）
4. **終端の歯止めは既存の sizing と数量制約に委ねる。** sizing は equity 比（`src/trading/risk/engine.py:306`）なので、資産が減ると許容数量が縮む。PortfolioManager の刻み丸めで数量が 0 以下なら intent を作らず（`src/trading/portfolio/manager.py:104-108`）、RiskEngine に届いた注文でも許容数量が broker 最小数量（USDJPY は 1,000 通貨）未満なら `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` で拒否する。どちらの経路でも、損失停止を外したまま無制限に建て続けることはない。ただし判定は注文ごとで、同じ equity でも stop 距離やイベントモードが変われば発注可能になる。単一の資産額で永久停止する保証や、必ず最小ロットの拒否コードが記録されるという説明はしない
5. **[§4.3](../SYSTEM_SPEC.md#s4-3) の「初心者向け Micro Live 上限を backtest 最適化の対象にしない」は維持する。** 本 ADR は上限を研究用に別の値へ最適化するのではなく、研究の測定対象から外す。live 系 overlay の値は変わらない

### Consequences に含めること

- 過去の研究 run（H4 の 2 腕、H5 を含む）は旧閾値で打ち切られた測定であり、本 ADR 以降の run と直接比較できない。比較する場合は両腕を修正後の同じ設定で流し直す
- 研究リプレイの成績は「口座水準の損失停止を掛けず、既存の sizing・執行制約を維持した条件のエッジ」を示す。実運用の上限下での成績（停止の発生頻度・停止後の機会損失）は、エッジが確認できた戦略について別に測る
- `100.00` は各窓の基準資産に対する閾値であり、初期資金からの累積損失率に一律に掛かる値ではない。ゼロ以下の資産や他の gate を迂回する値でもない。本決定を、追加の無効化フラグや RiskEngine の分岐変更に広げない
- 研究ノート（`docs/research/`）の追記は別 issue で行う。本 ADR では変更しない

## 6. テスト方針

`tests/unit/test_config.py` の `test_backtest_enables_risk_gate_for_simulated_orders` の直後に 2 本を追加する。関数名は例。既存のテストと同じく、非自明な期待値にだけ短い日本語コメントを添える。

1. `test_backtest_relaxes_account_level_loss_halts`
   - `load_config("backtest", CONFIG_DIR)` して `config.risk.daily_loss_halt_pct == Decimal("100")`、`rolling_24h_loss_halt_pct == Decimal("100")`、`high_water_mark_drawdown_halt_pct == Decimal("100")` を assert する（`Decimal` の `==` は数値比較なので `100.00` と `100` は等しい）
   - 適用範囲の歯止めとして、同じ config で `max_open_positions_per_symbol == 1`、`max_units_per_symbol["USDJPY"] == 1000`、`event_mode_default is EventRiskMode.REDUCED` が base のまま残っていることも assert する（`EventRiskMode` は `trading.domain.risk` から import する。既存 import に無ければ追加する）
   - その他の risk 設定も維持することを、risk の変更が `trading_enabled` だけである demo overlay と比較して確認する。両者の `risk.model_dump(exclude={"trading_enabled", "daily_loss_halt_pct", "rolling_24h_loss_halt_pct", "high_water_mark_drawdown_halt_pct"})` が等しいことを assert し、portfolio 上限・1 取引のリスク比率・spread 等の意図しない緩和を検出する
2. `test_live_and_shadow_overlays_keep_account_level_loss_halts`
   - `for env in ("shadow", "demo", "micro_live", "production")` で `load_config(env, CONFIG_DIR)` し、3 値が `Decimal("0.75")` / `Decimal("1.00")` / `Decimal("3.00")` であることを assert する

RiskEngine の承認・拒否の動作テストは追加しない（issue の範囲外。`src/` を変えないため既存の `tests/unit/test_risk_engine.py` で足りる）。既存テストは変更しない。`tests/unit/test_invariants.py` を緩めない。

## 7. 完了条件（実行可能なコマンド）

この worktree で、すべて `.venv/bin/` のコマンドを使う（venv は作成・インストール済み）。

```bash
.venv/bin/ruff check .                                  # 無指摘で終わること
.venv/bin/pytest tests/unit/test_config.py -q            # 追加した 2 本を含め green
.venv/bin/pytest tests/unit tests/replay tests/failure -q   # 既存も含め green（broker/integration は対象外）
```

実装順は backtest overlay、ADR、テストの追加、上記チェックとする。

## 8. やらないこと（issue の「やらないこと」に加えて）

- `docs/SYSTEM_SPEC.md` の本文改訂（ADR で扱う）
- ADR の索引ファイルの追加・更新（`docs/adr/` に索引は無い。作らない）
- `src/` 配下の変更全般
- 既存テストの変更・削除、`tests/unit/test_risk_engine.py` 等への追加
- 3 節の対象ファイル以外の整形・リファクタ・コメント修正
- `git commit` / `git push`
- `tasks/` / `tmp/` 配下の新規ファイル作成

## 9. プロジェクト規約の転記（`.claude/rules/` から）

- やり取り・コメント・ADR は日本語で書く（既存の英語 docstring は維持してよい）
- 金額・比率の閾値は `Decimal`。テストの期待値も `Decimal("...")` で書く
- テストデータに実在する人物・団体名を使わない
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない
- lint は `ruff`（設定は `pyproject.toml`）。自動修正を使う場合も 3 節の対象ファイルに限定し、無関係なファイルを整形しない
- WHAT を説明するだけのコメント、「issue #153 対応で追加」のようなコミット文脈に依存するコメントは書かない。YAML のコメントは「なぜ研究では外すのか」を 2〜3 行で書く
- 作業はこの worktree 内だけ。メインリポジトリ（`/Users/yusuke/Products/fx-trading-platform`）を編集しない

## 10. 完了報告に含めること

- 変更したファイル一覧
- 実行したテスト（コマンドと結果）
- 計画から逸脱した点（あれば理由つき）
- UI 変更: なし

## 11. 計画レビュー時点の確認結果（2026-09-14、Codex による）

- HEAD `a94ceec` の実コード・5 環境の設定・関連テストを照合した。3 キー以外を維持して `100.00` を型検証でき、研究 CLI から RiskEngine まで渡される経路を確認した
- 現行コードで `tests/unit/test_config.py tests/unit/test_risk_engine.py tests/unit/test_limits.py` を実行し 79 passed（変更前の基準結果）
- 設定ファイルを変更せず、メモリ上の提案設定で RiskEngine を評価した。4% 損失時の承認、0 / -1 の資産での拒否、最小数量制約と REDUCED の維持を確認した
- 同じ equity 500,000 でも stop 距離 30 pips では最小数量の拒否、10 pips では承認となった。equity 50,000・10 pips では PortfolioManager が intent を作らなかった。最高値 1,000,000 を維持したまま equity が 969,000 から 975,000 に回復すると、旧 HWM 閾値の拒否は解除された
- H4 / H5 の元 run と長時間リプレイは未確認。ADR では issue #153 の報告として記す
