# 研究リプレイのホットパス高速化（結果は一切変えない）

このファイル単体で実装できるように書いてある。ここに書かれていない変更（周辺リファクタ・
無関係な整形・追加の抽象化）はしない。**コミットと PR は別担当**なので行わない。

作業前に `AGENTS.md` と `.claude/rules/workflow.md` / `.claude/rules/change-management.md` を読むこと。

## 目的

研究リプレイ 1 本が VPS で約 11 時間かかっている。**結果（約定・損益・資産曲線・各 hash）を
1 ビットも変えずに**、その時間を削る。

VPS 実測の 1 tick あたり内訳（合成 tick を実密度 300ms 間隔で同じエンジン経路へ流し、各 2 回）:

| 段階 | µs/tick | 1億36万tick換算 |
|---|---|---|
| PostgreSQL から行を読むだけ | 4.8 | 0.13 h |
| ＋ pydantic Tick 構築 | +2.3 | 0.20 h |
| ＋ `reconstructed_tick`(model_copy) と `TickDigest`(sha256) | +12.0 | 0.53 h |
| ＋ エンジン本体（戦略 no-op） | +71.7 | 2.3 h |
| ＋ 戦略 `failed_spike_reversal` の評価 | +307 | 10.9 h |

DB は全体の 5% 未満。95% が Python の CPU 時間で、うち 77% が戦略評価。

Codex による独立プロファイル（リポジトリ内の実 tick `reports/jev-policy-20260920T132722Z/market/20241107T19Z.bi5`
ほか 2 時間 33,054 件）でも同じ結論で、**`Tick.mid` の呼び出しが 2,563 万回 = 1 tick あたり 776 回**だった。

## 実装する 3 項目

### 項目 A: `Tick.mid` の再計算をやめる（最優先）

`src/trading/domain/market.py:40` の `mid` は `(bid + ask) / 2` を **Decimal 除算で毎回計算する
property**。1 本の tick は滑る窓の中に約 776 回居座るため、同じ値を 776 回計算している。

**実測済みの事実**（このリポジトリの Python 3.11 / pydantic 2.x で確認）:

- `functools.cached_property` は frozen な pydantic v2 モデルで動作する。hash も等価比較も壊れない
- 再アクセスが **5.8 倍速い**（Decimal property 0.173 µs → cached float 0.030 µs）
- **危険**: `model_copy(update=...)` は `__dict__` ごとコピーするため、**キャッシュ済みの値をそのまま
  持ち越す**。`bid` / `ask` を差し替える copy を作ると値が壊れたまま生き残る（実測で確認）

実装方針:

1. `Tick` に float の中値を `cached_property` で持たせる。名前は実装者の判断でよいが、
   Decimal を返す既存の `mid` は**残す**（他の利用箇所が Decimal を前提にしている）
2. **キャッシュの陳腐化を構造で防ぐこと。** 次のどちらかを取る:
   - (a) `Tick.model_copy` を override してキャッシュ済み属性を落とす（3 行程度。推奨）
   - (b) Tick から Tick を作る経路をすべて「新しいインスタンスを構築する」形に変える
3. 現状、Tick に対する `model_copy` は `src/trading/backtest/research.py:92` の
   `reconstructed_tick` だけで、更新するのは `received_at` のみ（価格は変えないので現時点では
   壊れていない）。ただし将来の追加で静かに壊れる形なので、上の防御は必須
4. `reconstructed_tick` を `model_copy` ではなく直接構築に変えてよい。12 µs/tick の一部が消える

`ReplayMarketData` / `InMemoryMarketData` / `StoredMarketData` は
`src/trading/data/market/__init__.py:19` の `MarketDataService` Protocol を共有している。
**Protocol にメソッドを足すと 3 実装すべてに波及し、live 経路にも影響する。**
Tick 側にキャッシュを持てば Protocol を変えずに 3 実装すべてが速くなる。これが項目 A を
最優先にする理由。窓側に float 列を並走させる実装（Claude 側の試作）は Protocol 変更が要るので**採らない**。

### 項目 B: `_evaluate` の窓の二重構築とゲート順

`src/trading/strategy/scalp/failed_spike_reversal.py:93` の `_evaluate` が 1 tick ごとに:

- `ctx.market.ticks(symbol, window_seconds * 3)` で窓を構築（:114）
- さらに `ctx.indicators.tick_momentum(symbol, window_seconds / 2)`（:126）の内部
  （`src/trading/indicators/__init__.py:94`）で `self._market.ticks(...)` をもう一度呼び、
  `src/trading/indicators/momentum.py:22` がさらにもう一度リスト内包で絞り込む
- 安価な `spread_gate.allows`（:117）が高価な窓構築の**後**にある

実装方針:

1. 窓の取得を 1 回にまとめる。`IndicatorService.tick_momentum` に、取得済みの窓を受け取れる
   経路を足す（既存シグネチャは live 用に残す）
2. `spread_gate.allows` を窓構築より前に出す。判定に使う `last` は窓の末尾 = `ctx.market.latest_tick(symbol)`
   と同一なので、順序を入れ替えても結果は変わらない（`len(ticks) < 10` で return するケースも
   最終的な戻り値は同じ None）
3. `max(mids)` / `min(mids)` / `mids.index(...)` / `mids[:idx]` の重複走査を減らしてよいが、
   **値と分岐条件を変えないこと**

**必須の禁止事項**: `src/trading/indicators/momentum.py:29` の
`float(window[-1].mid - window[0].mid)` という **Decimal 同士の引き算**を
`float(mid) - float(mid)` に置き換えないこと。1 tick に 1 回しか呼ばれないので速度に効かず、
置き換えると値の同一性の証明が難しくなる。項目 A の float キャッシュは
「窓全体の中値リストを作る」用途にだけ使い、momentum の差分は Decimal のまま計算する。

`src/trading/backtest/market.py:47` の遅着 tick の挿入処理、同時刻 tick の順序、
窓端の包含条件（`tick.time >= start`）は必ず維持すること。

### 項目 C: Bar の二重生成を解消

`src/trading/backtest/research.py:275` の `capture_bars` が CSV 出力用に `BarBuilder` を作り、
`src/trading/backtest/engine.py:784` がエンジン内部用に
**`strategy_config.timeframes.all()` という同じ時間足の組**でもう一組作っている。
全 tick が 2 回折り畳まれている（時間足 2 つの戦略なら 1 tick あたり 4 回）。

実装方針: エンジンが確定させた Bar を CSV 側へ渡す。エンジンの `handle` は
`for builder in w.bar_builders:` で確定 Bar を得ているので、そこから呼び出し側へ渡す経路
（コールバックなど）を作るのが低リスク。

**CSV の内容（`bars_<timeframe>.csv` の行の値と順序）と、Bar が strategy に見えるようになる
時刻を変えないこと。** 現在 CSV は Bar 確定ごとに `flush()` している（途中で落ちた run の
記録を残すため）。この性質も維持する。

### 項目 D: `params_for` の再解決（余力があれば）

`src/trading/strategy/base.py:118` の `params_for` が、呼ばれるたびに resolver を生成し
defaults と銘柄別設定をマージして `ResolvedStrategyParameters` を生成・検証している
（`src/trading/strategy/parameters.py:68`）。Codex 実測で実 tick 33,054 件に対し
**94,347 回・0.838 秒（3.0%）**。

`StrategyConfig` は frozen な pydantic モデルなので、項目 A と同じ `cached_property` の手が使える
（引数 `symbol` があるので素朴な `cached_property` は使えない。`symbol` をキーにした
内部キャッシュを持たせる）。**時刻依存の判定（セッションが開いているか等）まで固定しないこと。**
設定を差し替えたら再解決されること、別 run とキャッシュを共有しないことを確認する。

項目 A〜C が先。D は時間が余った場合に入れ、入れないなら報告に理由を書く。

## やってはいけないこと

- tick の間引き（損切り・利確・約定・時間切れ決済・最大 DD の観測が変わる。`engine.py:458` 付近）
- 足確定時だけの評価（同上）
- ATR / EMA を無期限の逐次漸化式に置換（現在は有限窓から再初期化するので値が変わる。
  `src/trading/indicators/atr.py:22`、`ema.py:7`）
- 全期間の足を事前計算して `ReplayMarketData` に流し込む（同クラスは投入済みデータを可視済みと
  みなし読み出し時に PIT フィルタをかけないため先読みが入る）
- `MarketDataService` Protocol への安易なメソッド追加（3 実装と live 経路に波及する）
- Strategy / LLM 層から DB・OMS・Broker へ到達する経路を作る、Strategy 内で `datetime.now()` を呼ぶ
- 非同期ループの統合と指標キャッシュは **main に実装済み**（`engine.py:507`、
  `indicators/__init__.py:38`）。作り直さない

## 完了条件と検証

### 1. 結果の同一性を証拠で示す（最重要）

tests が通るだけでは不十分。**変更前後で `BacktestResult` が完全一致することを示すこと。**

推奨する形: `BacktestResult` 全体（約定記録・trade 記録・資産曲線・snapshot・metrics）と
`bars_<timeframe>.csv` の内容から正準な digest を作り、固定した入力（seed 固定の合成 tick と、
`reports/jev-policy-20260920T132722Z/market/` の実 bi5 tick の両方）に対する期待値を
**origin/main 上で先に算出**してからテストに焼き込む。こうすると将来の変更にも効く回帰テストになる。

手順の例:
1. `origin/main` の worktree（`/Users/yusuke/Products/fx-trading-platform` が main）で digest を出す
2. このブランチで同じ digest が出ることを確認する
3. その digest を固定値としてテストに入れる

項目 A の陳腐化防止（`model_copy` 後に中値が正しいこと）を固定するテストも入れること。

### 2. 既存テスト

`tests/replay/`（特に `test_vertical_slice.py` / `test_decision_consistency.py` /
`test_research_replay.py` / `test_lookahead.py`）と `tests/unit/test_invariants.py`、
`tests/unit/test_replay_market_data.py`、`tests/unit/test_indicators.py` を通す。
**通すためにテスト側を緩めない。**

### 3. lint とテスト全体

`ruff check .` と `pytest` を通す。broker テストは MT5 なし環境で自動 skip、
integration は DSN 未設定なら skip される（venv は `.[dev,db]` で作成済み）。

### 4. 速度の実測

変更前後の tick/s を実測して報告する。合成 tick を 300ms 間隔で流す形でよい
（実データは窓が約 4 倍大きいので、この測定は控えめな値になる）。
戦略は `failed_spike_reversal` と `range_edge_reversal` の両方を測る
（前者は tick 窓、後者は bar 中心で、項目 A / C の効き方が違う）。

## 未確定事項

- 項目 A のキャッシュ陳腐化対策を (a) `model_copy` の override にするか (b) 構築経路の変更にするかは
  実装者の判断。ただし**どちらも入れないのは不可**
- 項目 C のエンジンから CSV へ Bar を渡す具体的な形（コールバック / 戻り値 / 別経路）は任せる
- 項目 D を今回入れるかどうか
