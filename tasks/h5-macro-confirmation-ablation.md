# H5: post_event_failed_breakout のマクロ確認 ablation

## 背景（なぜやるか）

intraday 戦略 `post_event_failed_breakout`（`src/trading/strategy/intraday/post_event_failed_breakout.py`）は、失敗ブレイクアウトのテクニカル条件に**マクロ確認**を組み合わせている。

- `_short_macro_gate`（`post_event_failed_breakout.py:168-177`）: 米 2 年金利の 5 日ドリフト下向き OR 米指標の下振れサプライズ OR BOJ 声明スコアがタカ派
- `_long_macro_gate`（`post_event_failed_breakout.py:179-191`）: 米 2 年金利の 5 日と 1 日ドリフトが上向き AND 介入リスクが閾値以下

この確認レッグは、棄却済みの仮説 H1（政策収斂に 5〜20 営業日先の予測力がある）と同じ金利差情報を使う。H1 棄却後、この確認が約定の質を上げているかは未検証（仮説 H5）。本作業は、マクロ確認あり／なしで同じ期間を研究リプレイし、寄与を分離して測れるようにする。**実際のリプレイは VPS で行い、本作業はその道具立てだけを作る。**

## 変更 1: 戦略パラメータ `macro_confirmation_enabled`（bool、既定 true）

対象: `src/trading/strategy/intraday/post_event_failed_breakout.py`

- `_evaluate`（`post_event_failed_breakout.py:91`）で `macro_enabled = bool(params.param("macro_confirmation_enabled", True))` を読む。既存の `gate_eps = float(params.param("macro_gate_threshold", 0.0))`（`:99`）と同じ読み方。`ResolvedStrategyParameters.param` は `src/trading/strategy/parameters.py:55`。
- 2 つの gate 関数にキーワード引数 `enabled: bool = True` を足す（既定 True にして既存テスト `tests/unit/test_strategy_feature_gates.py:57-84` の呼び出しはそのまま通す）。
  - `_short_macro_gate(ctx, eps, *, enabled=True)`: `enabled` が False なら **True を返す**（判定を通過扱い）。
  - `_long_macro_gate(ctx, eps, intervention_max, *, enabled=True)`: `enabled` が False なら **US2Y の 5 日・1 日ドリフト条件だけを通過扱い**にし、介入リスクの上限判定（`intervention is not None and intervention < intervention_max`）は残す。介入リスクはマクロ確認ではなくリスクゲートなので ablation の対象外。
- `_evaluate` 内の呼び出し 2 箇所（`:126` の `self._short_macro_gate(ctx, gate_eps)` と `:150` の `self._long_macro_gate(ctx, gate_eps, intervention_max_for_long)`）に `enabled=macro_enabled` を渡す。**変更はこの引数追加だけ**。`detect_failed_breakout(...)` の引数、`_new_setup`、`make_signal` の呼び出し、reason_codes、conviction は一切触らない（PR #122 が同じ箇所を変更中で、マージ時の衝突を最小にするため）。
- モジュール冒頭 docstring（`:1-20`、「Macro confirmation reads what the platform actually measures...」の段落の直後）に、`macro_confirmation_enabled` が H5 ablation 用で、false のときはマクロ確認だけを外し介入リスク上限は残る旨を 1〜2 行で追記する。
- `strategy_version` は変えない（false は研究リプレイでしか使わず、既定 true の挙動は不変）。

対象: `config/base.yaml`

- `strategies.post_event_failed_breakout.parameters`（`config/base.yaml:166-172`）は **flat 形式**（`defaults:` キー無し。`StrategyParameters._wrap_flat_parameters`（`parameters.py:19-24`）が defaults に包む）。そこへ `macro_confirmation_enabled: true` を 1 行足す（既定値の可視化）。
- 他 env（`config/backtest.yaml:15`、`config/micro_live.yaml:13`、`config/shadow.yaml:10`）は `enabled` / `status` しか持たず parameters を独自定義していないので追加不要（`rg -n "post_event_failed_breakout" config/` で確認済み）。

## 変更 2: 研究リプレイ CLI のパラメータ上書き

対象: `src/trading/backtest/research.py`

- `main()`（`research.py:334`）の argparse に `--param KEY=VALUE`（`action="append"`, `default=[]`, `metavar="KEY=VALUE"`, `type=parse_param_override`）を追加する。
- 新関数 `parse_param_override(text: str) -> tuple[str, ParamValue]`（`ParamValue` は `trading.strategy.parameters.ParamValue = float | int | str | bool`）。`=` を含まない入力は `argparse.ArgumentTypeError`。VALUE の型解釈:
  - `true` / `false`（大文字小文字不問）→ bool
  - 整数表記（`^[+-]?\d+$`）→ int
  - `float()` で解釈できる小数表記 → float
  - それ以外 → str
- 新関数 `with_param_overrides(config: AppConfig, strategy_id: str, overrides: Mapping[str, ParamValue]) -> AppConfig`。
  - `strategy = config.strategies[strategy_id]`
  - `parameters = StrategyParameters(defaults={**strategy.parameters.defaults, **overrides}, instruments=strategy.parameters.instruments)`（コンストラクタ経由で validator を通す。`StrategyConfig.params_for` のコメント（`src/trading/strategy/base.py:111-114`）どおり raw dict を渡さない）
  - `strategy.model_copy(update={"parameters": parameters})` → `config.model_copy(update={"strategies": {**config.strategies, strategy_id: new_strategy}})` を返す。**元の config / strategy_config は破壊しない**（frozen モデル）。
  - 上書き先に存在しない KEY はそのまま defaults に追加する（strategy が読まなければ無視される。検証は CLI 引数の型解釈だけ）。
  - docstring に優先順を書く: `--param` は `parameters.defaults` を上書きする層で、`parameters.instruments.<symbol>` の同名キー（instrument 別 override）はさらにその上に載る（instrument override が最終的に勝つ）。
- `main()` では `load_config(args.env)` の直後、`strategy_config = config.strategies.get(args.strategy)` の**前**に `overrides = dict(args.param)` を作り、`config = with_param_overrides(config, args.strategy, overrides)` で差し替える（strategy が無い env での KeyError を避けるため、`config.strategies.get(args.strategy) is None` の SystemExit 判定を先に行ってから上書きしてもよい。どちらでも可、順序だけ守る）。これで `strategy_config`、engine、`config_sha256`（`research.py:535`）のすべてに上書きが反映される。
- manifest（`research.py:508-537`）に `"param_overrides": overrides` を追加する。標準出力の JSON（`research.py:540`）にも `"param_overrides": overrides` を含める（`{"run_dir": ..., "param_overrides": ..., **result.metrics}`）。
- モジュール docstring（`research.py:1-40`）に `--param` の 1 段落（用途と優先順）を足す。

## 変更 3: 比較に必要な出力（round-trip ごとの純損益）

既存の出力を確認した結果: `reports/<run_id>/trades.json` は **fill 単位**（`FillRecord`、`src/trading/backtest/report.py:39-41`）で、round-trip の概念は engine に無い（`_RunState`（`engine.py:233-270`）は ticket ごとに `entry_price` / `entry_mid` を持つが entry 時刻も決済ごとの損益も残していない）。したがって round-trip 記録を新設する。

対象: `src/trading/backtest/engine.py`

- `FillRecord`（`engine.py:167-180`）の直後に frozen dataclass `TradeRecord` を追加: `strategy_id: str`, `symbol: str`, `entry_at: datetime`, `exit_at: datetime`, `direction: str`, `quantity: Decimal`, `entry_price: Decimal`, `exit_price: Decimal`, `net_pnl: Decimal`, `reason: str`。
  - `net_pnl` は `signed_pnl(direction, entry, exit_price, quantity)`（`engine.py:273-279`、約定価格ベースなので spread / slippage 込み）。**carry は含めない**（carry は ticket 単位で boundary ごとに計上され、遅着 tick で遡及訂正される（`engine.py:529-645`）ため決済時点では確定しない。`metrics.carry_total` に別掲のまま）。単位は quote 通貨で、engine の `realized` / `net_pnl` と同じ扱い（USD/JPY の quote = JPY = 口座通貨）。docstring にこの 2 点を書く。
  - `reason` は決済の理由コード: command 決済は `pending.action`（`CLOSE` / `REDUCE`）、保護決済は `PROTECTION_CLOSE:<protection_reason>`（例 `PROTECTION_CLOSE:STOP_LOSS`。`fill.protection_reason` は `src/trading/domain/fill.py:25-28` の `ProtectionReason`。simulator の保護 fill は必ず reason を載せる（`src/trading/backtest/simulator.py:319`））。
- `BacktestResult`（`engine.py:183-191`）に `trades: list[TradeRecord]` を追加（`fills` の直後）。唯一のコンストラクタ呼び出しは `_result`（`engine.py:1197`、キーワード引数）。
- `_RunState`（`engine.py:233`）に `entry_at: dict[str, datetime]`（`entry_price` と並べる）と `trades: list[TradeRecord]` を追加。
- `_fill_pending`（`engine.py:894`）の ENTRY 側（`:938-942`）で `state.entry_at[ticket] = fill.broker_time` を記録。
- `_settle_close`（`engine.py:964`）に keyword `reason: str` を追加し、`state.realized += ...` の直後に `TradeRecord` を `state.trades` へ append する（`entry_at=state.entry_at[ticket]`, `exit_at=at`, `entry_price=entry`, `exit_price=price`, `net_pnl=signed_pnl(direction, entry, price, quantity)`）。部分決済は決済 fill ごとに 1 行（同じ ticket の残量は次の決済で別行になる）。ticket が完全に閉じたときの pop（`:1010-1012`）に `state.entry_at.pop(ticket, None)` を並べる。
- `_settle_close` の呼び出し 2 箇所に `reason` を渡す: `_fill_pending` EXIT（`:914-926`）は `reason=pending.action`、`_apply_protection`（`:672-684`）は `reason=f"PROTECTION_CLOSE:{fill.protection_reason.value}"`。
- `_result`（`engine.py:1157`）の metrics に追加: `"trades": str(len(state.trades))`、`"expectancy": str(sum(net_pnl) / len(trades))`（round-trip が 0 件なら `"NaN"`）。**既存キー（`net_pnl` / `max_drawdown` / `fills` / `risk_rejections` ほか）は変えない。** `BacktestResult(..., trades=state.trades, ...)` を渡す。
- `ENGINE_VERSION` は上げない（fill・PnL・metrics 既存値は不変。追加は記録のみ）。

対象: `src/trading/backtest/report.py`

- `write_report`（`report.py:21`）で `run_dir / "trades.csv"` を書く。ヘッダ行: `strategy_id,symbol,entry_at,exit_at,direction,quantity,entry_price,exit_price,net_pnl,reason`。時刻は `isoformat()`、Decimal は `str()`。標準ライブラリ `csv` を使い、`lineterminator="\n"` を指定する（Windows の既定 `\r\n` で Mac 側の読み込みと差が出ないように）。読む側（`ablation_compare.load_run`）は `open(..., newline="", encoding="utf-8")`。
- モジュール docstring の一覧（`report.py:6-10`）に `trades.csv -- round-trip record (one row per closed quantity)` を足す。`trades.json`（fill 単位）はそのまま。

## 変更 4: 2 run の比較 CLI `trading.backtest.ablation_compare`（小さく）

新規: `src/trading/backtest/ablation_compare.py`

```
python -m trading.backtest.ablation_compare --with <run_dir_A> --without <run_dir_B> [--seed 42]
```

- `--with` / `--without` は `research` が書いた run_dir（`manifest.json` / `summary.json` / `trades.csv` を含むディレクトリ）。`--seed` の既定は 42。
- `load_run(run_dir: Path) -> RunArtifacts`（frozen dataclass: `manifest: dict`, `metrics: dict[str, str]`, `pnls: list[Decimal]`）。`trades.csv` の `net_pnl` 列を Decimal で読む。ファイルが無ければ `SystemExit` にメッセージ（境界＝ファイル読込。それ以外の防御分岐は足さない）。`param_overrides` は `manifest.get("param_overrides", {})` で読む。
- `arm_summary(pnls: Sequence[Decimal], max_drawdown: Decimal, seed: int) -> ArmSummary`（frozen dataclass: `count: int`, `total: Decimal`, `mean: Decimal`（0 件なら `Decimal("NaN")`）, `hit_rate: float`（`net_pnl > 0` の割合、0 件なら nan）, `max_drawdown: Decimal`, `low: float`, `high: float`）。CI90 は `trading.backtest.policy_event_study.bootstrap_interval`（`policy_event_study.py:302-316`、`BOOTSTRAP_SAMPLES = 2000`、`BOOTSTRAP_LEVEL = 0.90`）を `[float(p) for p in pnls]` で再利用。`max_drawdown` は `summary.json` の `metrics["max_drawdown"]` から。
- `difference_interval(with_pnls: Sequence[float], without_pnls: Sequence[float], seed: int) -> tuple[float, float]`: 両腕を**独立に**再標本化して `mean(with*) - mean(without*)` の分布を `BOOTSTRAP_SAMPLES` 回作り、`bootstrap_interval` と同じ percentile の取り方で CI90 を返す（`random.Random(seed)` 1 本で両腕を引く）。どちらかの腕が 2 件未満なら `(nan, nan)`。
- **事前固定した判定規則** `judge(with_: ArmSummary, without: ArmSummary, difference: tuple[float, float]) -> str`（純関数。上から順に最初に該当した行を返す。`shock_trigger_study.judge`（`src/trading/backtest/shock_trigger_study.py:402-412`）と同じ流儀）:

  | 順 | 条件 | 判定（返す文字列の定数名） |
  |---|---|---|
  | 1 | `with.mean > without.mean` かつ 差の CI90 が 0 を外す（`low > 0`） | `KEEP` = `確認レッグを維持（寄与あり）` |
  | 2 | `without.count >= 2 * with.count` かつ `without.mean >= with.mean` | `REMOVE` = `確認レッグを外す（絞るだけで質が上がらない）` |
  | 3 | いずれかの腕の `count < 10`（`MIN_TRADES = 10`） | `UNDECIDED_SAMPLE` = `判定不能（標本不足）。維持したまま再測定` |
  | 4 | それ以外 | `UNDECIDED_DIFFERENCE` = `判定不能（差が検出できない）。維持したまま再測定` |

  行 1 で「差の CI90 が 0 を外す」は with − without の区間なので `low > 0` のみ（`high < 0` は with が劣る側で行 1 の前提 `with.mean > without.mean` と両立しない）。差の CI が NaN（片腕 2 件未満）なら `low > 0` は False。**行 1・2 は両腕とも `count > 0` のときだけ評価する**（0 件の腕は `mean` が `Decimal("NaN")` で、Decimal の大小比較は `InvalidOperation` を出す）。0 件の腕があれば行 3 に落ちる。
- `report(with_: RunArtifacts, without: RunArtifacts, seed: int) -> str`（固定幅テキスト。`main` は `print(report(...))` するだけ）:
  1. 冒頭に両 run の manifest から `run_id` / `git_commit` / `git_dirty` / `dataset_hash` / `feature_dataset_hash` / `period_from` / `period_to` / `param_overrides` を腕ごとに並べる
  2. 表: 行 = `trades` / `net_pnl_total` / `expectancy(mean)` / `hit_rate` / `max_drawdown` / `mean CI90 [low, high]`、列 = `with` / `without`
  3. `difference of means (with - without): <値> CI90 [low, high] seed=<seed>` の 1 行
  4. `verdict: <judge の結果>` の 1 行
- `main()`: argparse（`--with` は `dest="with_run"`。`with` は予約語）→ `load_run` ×2 → `print(report(...))`。DB 接続は不要。

## テスト（合成データ。`tests/support.py` のファクトリ。実在人物名不使用）

### 戦略（`tests/unit/test_strategy_feature_gates.py` に追記）

- gate 関数レベル: `_short_macro_gate(ctx_with({}), eps=0.0, enabled=False)` が True。`_long_macro_gate(ctx_with({f.INTERVENTION_RISK: 0.1}), eps=0.0, intervention_max=0.5, enabled=False)` が True、`INTERVENTION_RISK: 0.9` なら False、`INTERVENTION_RISK` 無しなら False（介入リスク上限は残る）。
- `_evaluate` レベル（パラメータ読み取りを通す）: `SimpleNamespace` で ctx を組む。
  - `config=StrategyConfig(strategy_id="post_event_failed_breakout", instruments=["USDJPY"], timeframes=TimeframeMap(regime="1h", setup="15m", entry="5m"), parameters=StrategyParameters(defaults={"resistance_lookback": 3, "macro_confirmation_enabled": False}))`（`TimeframeMap` は `src/trading/strategy/base.py:59`、extra="allow"）
  - `market=SimpleNamespace(instrument=lambda symbol: usdjpy_spec(), bars=lambda symbol, timeframe, count: entry_bars if timeframe == "5m" else setup_bars)`、`indicators=SimpleNamespace(atr=lambda symbol, timeframe, period: 0.05)`、`features=InMemoryFeatureStore()`（空＝マクロ条件不成立）、`clock=FixedClock()`（`make_signal` が `clock.now()` を読む）、`portfolio=SimpleNamespace(position=lambda strategy_id, symbol: None)`（PR #122 マージ後の `_session_permits_setup` / `_setup_signal` が `ctx.portfolio.position` を読んでも動くように。session_profile 未設定なので現状は `_session_permits_entry` が True を返し portfolio は読まれない）
  - short 側: setup bars（15m、`make_bar(..., timeframe="15m")`）4 本で高値 150.00 → `rolling_high(setup_bars[:-1], 3)` = 150.00。entry bars（5m）は `detect_failed_breakout(entry_bars, 150.00, side="UP")`（`src/trading/indicators/market_structure.py:52-69`: 直前バーの high > level かつ close < level、最終バーの close < level）を満たす 3 本以上。
  - `macro_confirmation_enabled=False` → `strategy._evaluate("USDJPY", ctx)` が SHORT の signal を返す。`True`（既定、同じ bars・空 features）→ None。
  - long 側: `INTERVENTION_RISK: 0.9`（上限 0.5 超）＋ DOWN 型 failed breakout の bars（entry bars の high は resistance 未満にして short 分岐へ入らないようにする）で、`enabled=False` でも None。`INTERVENTION_RISK: 0.1` なら LONG の signal。
  - 戦略インスタンスはケースごとに新しく作る（`_new_setup` のメモは `self.__dict__` に残る）。

### research CLI（`tests/unit/test_research_runner.py` に追記。`CONFIG_DIR` は `tests/unit/test_config.py:16` と同じ組み方）

- `parse_param_override`: `"k=false"` → `("k", False)`、`"k=TRUE"` → True、`"k=20"` → `20`（int）、`"k=0.5"` → `0.5`（float）、`"k=usdjpy_core"` → str、`"novalue"` → `argparse.ArgumentTypeError`。
- `with_param_overrides(load_config("backtest", CONFIG_DIR), "post_event_failed_breakout", {"macro_confirmation_enabled": False})`: 新 config の `params_for("USDJPY").param("macro_confirmation_enabled", True)` が False、元 config は True のまま（破壊されていない）、`instruments` 層と他 strategy の設定は同一。未知キー `{"brand_new": 1}` も defaults に載る。
- manifest の `param_overrides` は `main()` が DB を要するため単体では直接検証しない。代わりに下の ablation_compare テストで `write_report` → `load_run` の往復（manifest に載った `param_overrides` が report に出る）を確認する。

### trades 集計（`tests/replay/test_vertical_slice.py` に追記。engine の lifecycle テストが集まっている場所）

- `run_slice(STRESS_SCENARIOS["normal"])`（既定 plan は 300 で LONG、1200 で SHORT のフリップ）: `result.trades` が 1 件、`direction == "LONG"`、`entry_at` が最初の OPEN fill の `at`、`exit_at` が CLOSE fill の `at`、`net_pnl == signed_pnl(LONG, entry_price, exit_price, quantity)`、`reason == "CLOSE"`。swap snapshot 無しで carry 0 なので `sum(trades.net_pnl) == Decimal(metrics["realized_pnl"])`。`metrics["trades"] == "1"`、`metrics["expectancy"] == str(trades[0].net_pnl)`。
- 保護決済（`test_protection_fill_closes_position_and_is_tracked` と同じ plan）: `reason` が `PROTECTION_CLOSE:STOP_LOSS`。
- round-trip 0 件のとき（fills 無しになる plan、例: `plan={}`）`metrics["trades"] == "0"`、`metrics["expectancy"] == "NaN"`。

### ablation_compare（新規 `tests/unit/test_ablation_compare.py`）

- `judge` の 4 分岐それぞれ 1 ケース（`ArmSummary` を直接組む。`pytest.mark.parametrize`）。行 1 と行 2 の両方を満たさない小標本が行 3 に落ちること、0 件の腕（mean NaN）が行 3 に落ちることも含める。
- `difference_interval`: 同じ seed で同じ結果、異なる seed で異なる結果、`with` が一様に大きければ `low > 0`、片腕 1 件なら NaN。
- `load_run` / `report`: `write_report` で `tmp_path` に run を 2 つ書く（`BacktestResult` を直接組む。`TradeRecord` 数件、manifest に `run_id` / `git_commit` / `dataset_hash` / `period_from` / `period_to` / `param_overrides` を含める）→ `load_run` の `pnls` が `trades.csv` と一致し、`report` の文字列に両腕の `param_overrides` と `verdict:` 行が含まれる。`trades.csv` が無い dir は `SystemExit`。

## 完了条件（実行コマンド）

```bash
W=/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+h5-macro-confirmation-ablation
cd "$W" && .venv/bin/ruff check .
cd "$W" && .venv/bin/pytest tests/unit tests/replay tests/failure -q      # tests/unit/test_invariants.py を含めて green
cd "$W" && .venv/bin/python -m trading.backtest.research --help | grep -- --param
cd "$W" && .venv/bin/python -m trading.backtest.ablation_compare --help
```

## やらないこと

- 戦略の entry / exit 条件そのもの、setup 検出、signal 生成コード（`detect_failed_breakout` の呼び出し、`_new_setup`、`make_signal`、reason_codes、conviction）の変更
- `failed_spike_reversal` / `monetary_policy_convergence` の変更（H4 は別作業）
- 多重検定補正、走査グリッド
- 実際のリプレイ実行（VPS で行う）
- スキーマ変更（`migrations/`）。DB は触らない
- `ENGINE_VERSION` / `strategy_version` の変更
- `docs/SYSTEM_SPEC.md` の変更（v1.3 で凍結）
- 周辺リファクタ・無関係な整形・追加の抽象化

## 変更対象ファイル一覧（網羅）

| ファイル | 変更 |
|---|---|
| `src/trading/strategy/intraday/post_event_failed_breakout.py` | `macro_confirmation_enabled` の読み取り、gate 関数の `enabled` 引数、docstring 追記 |
| `config/base.yaml` | `post_event_failed_breakout.parameters` に `macro_confirmation_enabled: true` |
| `src/trading/backtest/research.py` | `--param`、`parse_param_override`、`with_param_overrides`、manifest / stdout の `param_overrides`、docstring |
| `src/trading/backtest/engine.py` | `TradeRecord`、`BacktestResult.trades`、`_RunState.entry_at` / `trades`、`_settle_close` の `reason` と記録、metrics `trades` / `expectancy` |
| `src/trading/backtest/report.py` | `trades.csv` 出力、docstring |
| `src/trading/backtest/ablation_compare.py` | 新規 |
| `tests/unit/test_strategy_feature_gates.py` | gate / `_evaluate` のテスト追記 |
| `tests/unit/test_research_runner.py` | `parse_param_override` / `with_param_overrides` のテスト追記 |
| `tests/replay/test_vertical_slice.py` | trades / metrics のテスト追記 |
| `tests/unit/test_ablation_compare.py` | 新規 |

マイグレーション: 無し。

## 規約（AGENTS.md / グローバル規約から転記）

- 金額・数量・価格・損益は `Decimal`。統計計算（CI・hit 率）のみ float 可
- frozen モデル + `model_copy`。引数や共有オブジェクトを破壊しない
- 検証はシステム境界（CLI 引数・ファイル読込・設定）だけ。内部関数に防御的分岐・フォールバックを足さない
- WHAT を説明するコメントは書かない。「なぜ」だけ docstring / コメントに。AI レビューの引用や「〜のために追加」のような文脈依存コメントを残さない
- 通貨ペア・pip・時間足をハードコードしない（config / `InstrumentSpec` 経由）
- Strategy から Broker / OMS / DB へ到達しない（`StrategyContext` に執行系を足さない）。Strategy 内で `datetime.now()` を呼ばない
- テストデータに実在の人物・団体名を使わない
- `tests/unit/test_invariants.py` を通すためにテスト側を緩めない
- ruff（`pyproject.toml`）準拠。型注釈を付ける。日本語コメント優先（既存英語 docstring は維持）
- **コミットしない**

## PR 本文に書く実行手順（VPS、`C:\Users\Administrator\fx-trading-platform`）

`--from` / `--to` は `research.broker_label`（`research.py:123-140`）の仕様で **`+00:00` または `Z` 付き ISO 形式のみ受け付ける**（他のオフセットは拒否）。期間は 2024-08-01 〜 2026-09-01（ブローカー時刻）。

```
git pull
.venv\Scripts\python.exe -m trading.backtest.research --env backtest --symbol USDJPY --strategy post_event_failed_breakout --from 2024-08-01T00:00:00+00:00 --to 2026-09-01T00:00:00+00:00 --seed 42 --out reports\h5_with
.venv\Scripts\python.exe -m trading.backtest.research --env backtest --symbol USDJPY --strategy post_event_failed_breakout --from 2024-08-01T00:00:00+00:00 --to 2026-09-01T00:00:00+00:00 --seed 42 --out reports\h5_without --param macro_confirmation_enabled=false
.venv\Scripts\python.exe -m trading.backtest.ablation_compare --with reports\h5_with\<run_id> --without reports\h5_without\<run_id>
```

74 日欠損（2026-01-23〜04-08）の扱い（`research.py:188-218` の docstring で確認）: `ensure_period_covered` は期間の**両端**の欠損だけを open-market 時間で判定し、**期間の途中の欠損は検出せず、保存済み tick 列をそのまま市場記録として扱う**。engine は欠損をまたいで次の tick で bar・indicator・feature を更新し、欠損開始時に建っていたポジションは欠損明けの最初の tick で保護判定を受ける（両腕とも同じ扱いなので比較は対称だが、欠損をまたぐ round-trip は `trades.csv` の `entry_at` / `exit_at` で識別して別途目視すること）。
