# 実時間トリガーによるショック継続スタディ CLI（`trading.backtest.shock_trigger_study`）と 2026-07-31 協調介入エントリ

ブランチ: `feat/shock-trigger-study`（origin/main = 11d5b2b 起点）

この計画ファイルは **単独で実装できる自己完結の仕様** である。会話履歴・issue は参照しない。
不明点があれば、この計画に書かれた決定を優先し、書かれていないことは「やらないこと」節に従って
足さない。マイグレーションは **無し**。

## 1. 背景（なぜ作るか）

介入イベントスタディ（`src/trading/backtest/intervention_event_study.py`、PR #105/#118）で、介入
クラスタ初回 6 事例のショック足の後に数時間の円高継続（1h −0.67% / 4h −0.93%、hit 83%、CI90 が 0 を
外す）が観測された。ただしショックアンカーは「介入日を含む 36 時間窓で最大の下落 5 分足」で、窓が
閉じるまで特定できない **事後選択** である。

この結果が

- (H2) 介入固有の継続か
- (H3) 大きな下落ショック一般の性質か
- (棄却) 事後選択の産物か

を、**その 5 分足が閉じた時点の情報だけで判定できるトリガー** で決着させる。外部レビュー（2026-09-05）
の指摘を採り、コスト控除後（net）リターン・グリッドの事前登録・全セル報告・再現情報の出力を仕様に含める。

## 2. 要件（全文）

### 作業 1: `config/intervention_episodes.yaml` に 2026-07-31 を追加

- 事実（確認済み）: 2026-07-30 に日本の単独介入（既存エントリ）、翌 7/31（米東部時間）に米財務省が
  NY 連銀経由で執行した日米協調介入。8/3 の財務相談話で公式確認。7/30〜8/26 の月次総額 15 兆 3,993
  億円（8/28 公表、過去最大）。日次内訳は 11 月 2〜9 日に公表予定
- 追加エントリ（2024-05-01 の「NY 時間の介入は翌 JST 日の報道として上界を置く」前例に倣う）。
  `recognitions:` リストの末尾（既存の 2026-07-30 エントリの直後）に追加する:

  ```yaml
  - kind: REPORTED
    action_date: 2026-07-31
    known_at: 2026-08-01T14:59:00+00:00
    direction: JPY_BUY
    verified: false
    source_uri: https://www.mof.go.jp/policy/international_policy/reference/feio/data/monthly/20260828.html
    note: 米東部時間 7/31 の日米協調介入（米財務省が NY 連銀経由で執行、8/3 財務相談話で確認）。JST 8/1 中の報道として上界を置く。公式日次額は未公表
  ```

- 既存の 2026-07-30 の note はそのまま
- エピソードローダーのテスト（`tests/unit/test_intervention_collectors.py:198-223`）はエントリ数や
  日付を固定していない（`assert entries` と重複拒否のみ）ので **更新不要**。緩めない
- YAML 由来イベントは Mac と VPS の両方で collector（`python -m trading.data.intervention.collector
  --env demo`）を再実行しないと events に反映されない（PR 本文に書く。Codex の作業ではない）

### 作業 2: 新モジュール `src/trading/backtest/shock_trigger_study.py`（CLI）

既存の `policy_event_study.py`（`gaps` / `bootstrap_interval` / `summarize` / `unconditional` /
`Stats` / `_row` / `HOLE_MINIMUM`）と `intervention_event_study.py`（`fold_bars` /
`load_episodes_from_events` / `cluster_anchors` / `SHOCK_WINDOW` / CLI の `main()` の作法）を import
して再利用する。**既存 2 モジュールは変更しない**（E′ と介入スタディの結果の再現性を保つ）。必要な
関数が private なら、そのモジュールに触らず新モジュール側で同等の小関数を書く（`_row` は import して
よい。既に `intervention_event_study.py:39` が同じことをしている）。

#### 2-1. 入力

- 保存 tick（`market_ticks`）から USDJPY の全期間を 5 分足に畳む。既存 `BarBuilder` は bid で畳む
  （`src/trading/data/market/bars.py`）。本スタディは各 5 分足に **ask の終値**（そのバケツ内最後の
  tick の ask）を併記する。`Bar` モデルは変更せず、スタディ内部の frozen dataclass
  `QuoteBar(bar: Bar, ask_close: Decimal)` で持つ。実装は BarBuilder で bid 足を作りつつ、同じ tick
  ストリームからバケツ毎の最終 ask を記録する（§4.1）
- 介入エピソードと政策イベントは events テーブルから読む（`load_episodes_from_events` と同様に
  PIT イベントを読む）。政策イベントは BOJ/FED の決定イベントで、event_type は
  `trading.data.policy.scoring.EVENT_TYPES` の値 `BOJ_POLICY_SHIFT_SCORE` / `FED_POLICY_SHIFT_SCORE`
  （`src/trading/data/policy/scoring.py:27`）。`known_at` は声明公表の実 UTC
  （`scoring.py:44-75`、`known_at=meeting.statement_published_at`）

#### 2-2. トリガー（実時間で判定可能）

- 連続する 2 本の 5 分足（`prev.close_time == bar.start`）の間でだけ対数リターン
  `r_t = ln(close_t / close_{t-1})` を定義する（bid 終値）。週末・休場・欠損をまたぐ対は無効
- 直近 N 本の **有効な** r の平均 μ と母標準偏差 σ（当該足 t を含めない）から `z_t = (r_t − μ) / σ`。
  N 本ぶん揃わなければ判定しない
- **トリガー: `z_t < −K` かつ `r_t < 0`**
- グリッド（事前登録、変更しない）: `N ∈ {48, 96, 288}`、`K ∈ {3, 4, 5}` の 9 仕様。
  **主仕様は N=96, K=4**（判定規則はこのセルにだけ適用。他 8 セルは頑健性として全セル報告）
- 重なり: トリガー発火後、4 時間（48 本）の前向き窓が閉じるまで次のトリガーを抑止する
  （初撃だけを数える）。抑止した件数も報告する
- 前向き窓（+4h まで）に非連続（欠損・週末）が入るトリガーは無効として数えるが件数は報告する

#### 2-3. 前向きリターン（gross と net の両方）

- ホライズン: 15 分・1 時間・4 時間（3 / 12 / 48 本先の終値）
- gross: `ln(bid_close_exit / bid_close_entry)`（既存スタディと同じ定義。負 = 円高）
- **net（ショート想定）**: エントリはトリガー足終値の bid で売り、決済は exit 足終値の ask で
  買い戻す。`net = ln(ask_close_exit / bid_close_entry)`（負 = 利益）。加えてエントリ時点の
  スプレッド `ask − bid`（円、Decimal）を記録する
- 最大順行 / 最大逆行（既存 `window_outcome` と同じ定義）も gross で出す

#### 2-4. 層別

- **Layer A（介入窓）**: トリガー足の start が、いずれかの介入エピソードの `action_date` 00:00
  （ブローカー時刻）から `SHOCK_WINDOW`（36 時間）以内。エピソードの kind は問わない。さらに
  `cluster_anchors` でクラスタ初回に属するものを **A-first** として小計する
- **Layer B（政策イベント窓）**: BOJ/FED 決定イベントの `known_at` から 24 時間以内
- **Layer C**: それ以外
- A と B が重なる場合は A に入れ、重なり件数を報告する（2026-07-31 は協調介入と BOJ 会合が同日）

#### 2-5. 統計

- 層 × ホライズン × {gross, net} ごとに n / mean / median / hit（負の割合）/ CI90（既存
  `bootstrap_interval`、seed 固定）。無条件ベースライン（既存 `unconditional`）を同じホライズンで併記
- 主仕様（N=96, K=4）について **事前固定した判定規則** を機械的に評価して 1 行で出力する:

  | 条件（net、1h または 4h） | 判定 |
  |---|---|
  | A の CI90 が 0 を外して負、かつ C の CI90 が 0 を含む | H2 支持（介入固有） |
  | A と C の両方で CI90 が 0 を外して負 | H3 支持（ショック一般） |
  | C の mean が正で CI90 が 0 を外し、A が負で CI90 が 0 を外す | H2 強化（通常ショックは逆張り） |
  | gross では A が負で CI90 が 0 を外すが、net では 0 を含む | 統計現象だが非取引可能 |
  | それ以外 | H2/H3 棄却 |

  判定は net で行い、gross は参考として並記する。規則の評価は純関数にして単体テストで固定する
  （§4.7 に「1h または 4h」の解決順を固定してある）

#### 2-6. 出力（再現情報つき）

- 冒頭に: git commit（既存 backtest ランナー `src/trading/backtest/run.py:56` の `git_state()` を
  import して同じキー名で出す）、tick 件数、tick の最初と最後の時刻、dataset_hash、5 分足本数、
  エピソード数、政策決定数、グリッド、主仕様、seed
- 続けて: 9 セルのサマリ表（セルごとにトリガー数・抑止数・無効数・A/B/C 件数・主ホライズン 1h の
  net mean と CI90）、主仕様の詳細（層 × ホライズン × gross/net）、判定行
- stderr に進捗（既存スタディと同じ月次進捗）

#### 2-7. CLI

`python -m trading.backtest.shock_trigger_study --env backtest --symbol USDJPY [--seed 42]`。
`--env` / DSN / symbol の扱いは `intervention_event_study.main()`（`intervention_event_study.py:609`）
と `policy_event_study.main()`（`policy_event_study.py:478`、`--symbol` の拒否）に合わせる。

### テスト（`tests/unit/test_shock_trigger_study.py`、合成データ、`tests/support.py` のファクトリを使う。実在人物名不使用）

- z スコア: N 本の有効リターンから μ/σ を計算し、閾値ちょうどでは発火しないこと、`r_t < 0` の条件
- 非連続対（週末）がリターン計算から除かれ、N 本揃わないと判定しないこと
- 重なり抑止: 発火後 48 本以内の 2 本目が抑止され、件数に数えられること
- 前向き窓に欠損があるトリガーが無効になること
- net が ask で決済されること（bid/ask を分けた合成足で gross と net の差がスプレッドに一致）
- 層別: 介入窓・政策窓・その他、A と B の重なりは A
- 判定規則の純関数: 5 分岐それぞれ 1 ケース

### 完了条件

- `ruff check .` が無変更で通る
- `pytest tests/unit` が green（`tests/unit/test_invariants.py` を含む）
- 既存 `tests/unit/test_intervention_event_study.py` と E′/B′ のテスト
  （`test_policy_event_study.py` / `test_rate_differential_study.py`）が変更なしで通る

## 3. 参照する既存実装（file:line、origin/main 11d5b2b 時点）

### E′ `src/trading/backtest/policy_event_study.py`（import して流用する部品）

| シンボル | 行 | 使い方 |
|---|---|---|
| `SYMBOL = "USDJPY"` | 62 | `--symbol` の既定値と拒否判定 |
| `EPOCH` | 67 | tick ストリームの開始 |
| `BROKER_CLOCK_MARGIN` | 76 | tick ストリームの終了余裕 |
| `BOOTSTRAP_SEED = 20260828` | 79 | `--seed` の既定値 |
| `current_version(decisions)` | 120 | 政策イベントを現行 scoring_version に絞る |
| `Stats` | 170 | 集計行の型（count/mean/median/hit_rate/adverse/favorable/low/high） |
| `gaps(bars)` | 222 | 5 日以上の穴（`unconditional` 内で使われる。本スタディの連続性判定は §4.2 の自前ルール） |
| `window_outcome(bars, entry, horizon)` | 257 | gross リターン・最大逆行・最大順行（bid 足に対して呼ぶ） |
| `bootstrap_interval(values, seed)` | 302 | CI90 |
| `unconditional(bars, horizon, seed)` | 352 | 無条件ベースライン（gross、bid 足） |
| `_row(label, stats)` | 399 | 固定幅の集計行 |
| `main()` | 478-500 | `--env` / `--symbol` の拒否 / DSN 取得の作法 |

### 介入スタディ `src/trading/backtest/intervention_event_study.py`（import して流用する部品）

| シンボル | 行 | 使い方 |
|---|---|---|
| `SHOCK_WINDOW = timedelta(hours=36)` | 59 | Layer A の窓幅 |
| `JST` | 63 | A 層トリガー一覧の JST 表示 |
| `Horizon(label, timeframe, bars)` | 66 | ホライズン定義（timeframe は "5m" 固定で使う） |
| `Episode(action_date, known_at, cluster)` | 87 | エピソード（`cluster == action_date` がクラスタ初回） |
| `fold_bars(...)` | 113-136 | 月次進捗の書式の手本（ask を併記するため関数自体は流用できない。§4.1） |
| `cluster_anchors(dates)` | 148 | `load_episodes_from_events` が内部で呼ぶ。直接呼ぶ必要はない |
| `load_episodes_from_events(events)` | 161-175 | `INTERVENTION_REPORTED` かつ `JPY_BUY` を action_date 順の `Episode` にする |
| `shock_anchors` の窓計算 | 178-229 | `datetime.combine(action_date, time(0), tzinfo=UTC)` を窓の起点にする書き方 |
| `main()` | 609-651 | DSN・`stream_between`・`known_before`・`anchor` の組み立て |
| `EVENT_TYPE` | 54 | `INTERVENTION_REPORTED` |

### 足の畳み込みと tick

- `src/trading/data/market/bars.py:70` `bucket_start(at, timeframe)`（公開関数）、`:81` `BarBuilder`、
  `:103` `on_tick(tick) -> Bar | None`。close は「`tick.time >= bucket.last_time` の tick」で更新される
  （`:53-67` `_fold`）。閉じた足を返した tick は次のバケツに属する（`:117-128`）
- `src/trading/domain/market.py` `Tick(symbol, bid, ask, time, received_at)` / `Bar(..., close_time)`。
  `Bar.close_time = start + 5 分`
- `src/trading/backtest/data.py:51` `TickDigest`（`update(tick)` / `count` / `hexdigest()`）: 走査しながら
  dataset_hash と tick 件数を得る

### 再現情報

- `src/trading/backtest/run.py:56-83` `git_state()` → `{"git_commit", "git_dirty", ["git_diff_sha256"]}`。
  import して使う（`from trading.backtest.run import git_state`）
- `src/trading/backtest/research.py:510-537` manifest のキー名（`dataset_hash` / `tick_count` /
  `config_sha256` / `seed`）。本スタディは JSON ではなく先頭行に同名キーで出す

### ラベル軸変換（ADR-005 / ADR-014）

- tick / bar の時刻は broker ラベル（NY close = サーバー 00:00 の壁時計を UTC タグで持つ）。
  events の `known_at` は実 UTC。両者を比べるときは `src/trading/data/market/dukascopy.py:67`
  `known_to_broker_label(known, server_ahead_of_ny)` で実 UTC → ラベルへ写す
  （`intervention_event_study.py:231-251` `news_anchor` と同じ向き）。逆向きは
  `src/trading/backtest/research.py:76` `broker_label_to_known`（表示用）
- `anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)`
  （`src/trading/config.py:55`）

### events の読み方

- `PostgresEventRepository.known_before(t, event_type=None, since=None)`
  （`src/trading/storage/postgres.py:1019`）。`known_at` 昇順
- 介入認識イベントの payload: `{"action_date": "YYYY-MM-DD", "direction": "JPY_BUY", ...}`
  （`src/trading/data/intervention/episodes.py:69-92`）

### テストの手本

- `tests/unit/test_intervention_event_study.py:45-97` 合成 5 分足 `m5(index, close, ...)` /
  `intervention_event(action_date, direction, known_at)` の作り方、`:126-160` tick から `fold_bars` を通す
  テスト
- `tests/support.py:21` `T0 = 2026-08-13 00:00 UTC`、`:24` `at(**kwargs)`、`:112` `make_tick(bid, ask,
  time=..., symbol=..., received_at=...)`

## 4. 設計決定（Codex は判断せずこのとおり実装する）

### 4.0 モジュール定数と型

```python
TIMEFRAME = "5m"
LOOKBACKS = (48, 96, 288)            # N
THRESHOLDS = (3.0, 4.0, 5.0)         # K
PRIMARY = Spec(lookback=96, threshold=4.0)
HORIZONS = (Horizon("15m", TIMEFRAME, 3), Horizon("1h", TIMEFRAME, 12), Horizon("4h", TIMEFRAME, 48))
FORWARD_BARS = HORIZONS[-1].bars     # 抑止と有効性判定に使う前向き窓の長さ（= 4h の 48 本。数値を二重に持たない）
POLICY_WINDOW = timedelta(hours=24)
LAYER_INTERVENTION = "A"
LAYER_POLICY = "B"
LAYER_OTHER = "C"
LAYERS = (LAYER_INTERVENTION, LAYER_POLICY, LAYER_OTHER)
GROSS = "gross"
NET = "net"
```

型（`NamedTuple` と明記したもの以外は `@dataclass(frozen=True)`）:

```python
class Spec(NamedTuple):            # グリッドのセル
    lookback: int
    threshold: float

class InterventionWindow(NamedTuple):   # label 軸の閉開区間 [start, end)
    start: datetime
    end: datetime
    first: bool                    # クラスタ初回エピソードの窓か

class PolicyWindow(NamedTuple):
    start: datetime                # 公表時刻の label
    end: datetime                  # start + POLICY_WINDOW

@dataclass(frozen=True)
class QuoteBar:
    bar: Bar                       # bid の OHLC（BarBuilder の出力そのまま）
    ask_close: Decimal             # バケツ内で最後（tick.time 最大、同時刻は到着順で後）の tick の ask

@dataclass(frozen=True)
class Provenance:                  # 冒頭の再現情報
    tick_count: int
    first_tick: datetime | None    # broker ラベル
    last_tick: datetime | None
    dataset_hash: str

@dataclass(frozen=True)
class Trigger:
    entry: int                     # トリガー足（= エントリ足）の index
    z: float
    ret: float                     # r_t
    layer: str                     # "A" / "B" / "C"
    first: bool                    # Layer A のうちクラスタ初回エピソードの窓に入るもの（A 以外は False）
    overlap: bool                  # A と B の両方に該当（layer は "A"）
    spread: Decimal                # ask_close − bid close（エントリ足）
    returns: dict[str, dict[str, float]]    # {GROSS: {"15m": .., "1h": .., "4h": ..}, NET: {...}}
    adverse: dict[str, float]      # horizon label -> 最大逆行（gross、window_outcome と同じ）
    favorable: dict[str, float]

@dataclass(frozen=True)
class CellResult:
    spec: Spec
    triggers: list[Trigger]        # 有効トリガーのみ（前向き 4h 窓が連続で、系列内に収まる）
    suppressed: int                # 抑止した候補数
    invalid: int                   # 発火したが前向き窓が非連続 / 系列末尾で切れて無効にした数
```

### 4.1 畳み込み: tick 1 パスで bid 5 分足と ask 終値を同時に作る

`fold_quote_bars(ticks: Iterator[Tick], symbol: str, progress: TextIO | None) -> tuple[list[QuoteBar], Provenance]`

- `BarBuilder(symbol, TIMEFRAME)` で bid 足を畳む。`TickDigest` を並走させて `tick_count` /
  `dataset_hash` を得る。最初と最後の `tick.time` を控える
- ask 終値は BarBuilder の close と同じ規則（バケツ内で `tick.time` が最大、同時刻なら後着）で選ぶ。
  BarBuilder の内部状態（`_bucket`）には触れず、次のように鏡写しで追跡する:

  ```python
  open_start: datetime | None = None
  last_time: datetime | None = None
  ask: Decimal | None = None
  for count, tick in enumerate(ticks, start=1):
      digest.update(tick)
      bar = builder.on_tick(tick)
      if bar is not None:
          bars.append(QuoteBar(bar=bar, ask_close=ask))   # 閉じた足の ask。この tick は次のバケツに属する
          open_start = None
      start = bucket_start(tick.time, TIMEFRAME)
      if open_start is None:
          open_start, last_time, ask = start, tick.time, tick.ask
      elif start == open_start and tick.time >= last_time:
          last_time, ask = tick.time, tick.ask
      # start != open_start の tick は閉じたバケツへの遅着（BarBuilder も無視する）
  ```

  `bar is not None` のとき `ask` は必ず `bar.start` のバケツの値である（BarBuilder は開いているバケツを
  一つしか持たず、閉じるのは常にそのバケツ）。`ask` の `None` チェックは書かない
- 進捗: `intervention_event_study.fold_bars` と同じ月次書式
  （`f"{year}-{month:02d} {count:>12,} ticks  {len(bars):>7,} candles"`）を `progress` に出す。
  `progress is None` なら出さない
- 戻り値の bid 足列は `[quote.bar for quote in bars]` で別途作って `window_outcome` / `unconditional`
  に渡す（呼び出し側で一度だけ作る）

### 4.2 リターンと連続性

`log_returns(bars: Sequence[QuoteBar]) -> list[float | None]`

- `returns[0] = None`。`i >= 1` について `bars[i-1].bar.close_time == bars[i].bar.start` のときだけ
  `math.log(float(bars[i].bar.close) / float(bars[i-1].bar.close))`、それ以外は `None`
- 連続性はこの「隣接足の close_time == start」だけで決める。E′ の `gaps`（5 日ルール）は本スタディの
  連続性判定には使わない（週末や tick のない bucket も非連続として扱うため）

### 4.3 z スコア

`z_scores(returns: Sequence[float | None], lookback: int) -> list[float | None]`

- `collections.deque(maxlen=lookback)` に **有効な** リターンだけを積む。足 t の z は、t より前の有効
  リターンが `lookback` 本たまっている（`len(window) == lookback`）ときだけ計算し、計算後に `r_t` が
  有効なら window に追加する（当該足を含めない）
- `μ = sum(window) / lookback`、`σ = math.sqrt(sum((value − μ) ** 2 for value in window) / lookback)`
  （母標準偏差）。`statistics.pstdev` は使わない（Fraction 変換で 35 万本 × 3 N では遅い）
- `r_t` が `None`（非連続）、window が不足、または `σ == 0` の足は `None`
- 非連続対は window をリセットしない（週末をまたいでも直近 N 本の有効リターンをそのまま使う）

### 4.4 トリガー検出・抑止・有効性

```python
def detect(
    bars: Sequence[QuoteBar],
    bid_bars: Sequence[Bar],           # [quote.bar for quote in bars]。呼び出し側で一度だけ作る
    returns: Sequence[float | None],   # log_returns(bars)
    z: Sequence[float | None],         # z_scores(returns, spec.lookback)
    spec: Spec,
    interventions: Sequence[InterventionWindow],
    policies: Sequence[PolicyWindow],
) -> CellResult
```

`returns` / `z` を引数で受けるのは、N ごとの z 計算を K で共有するためと、テストが厳密な値
（z = −K ちょうど）を直接渡せるようにするため。下の規則は変えない。

1. 候補: `z[t] is not None and z[t] < -spec.threshold and returns[t] < 0`
2. 抑止: 直前に **発火**（候補として採用。有効・無効を問わない）したトリガーの index を `last` と
   すると、`t <= last + FORWARD_BARS` の候補は抑止して `suppressed += 1`。抑止された候補は `last` を
   更新しない
3. 発火した候補は有効性を見る。`t + FORWARD_BARS >= len(bars)`、または
   `returns[t + 1 .. t + FORWARD_BARS]` のいずれかが `None`（前向き 4h 窓に非連続がある）なら
   `invalid += 1` として `triggers` に入れない。それ以外は `Trigger` を組み立てる
   （15m / 1h は 4h 窓の部分列なので自動的に連続）
4. `Trigger` の値:
   - `entry_bid = bars[t].bar.close`、`entry_ask = bars[t].ask_close`、`spread = entry_ask − entry_bid`
     （Decimal のまま）
   - 各 `Horizon` について `result = window_outcome(bid_bars, t, horizon.bars)`（有効性を見た後なので
     `None` にはならない。`None` チェックは書かない）→ `Trigger.returns[GROSS][label] = result[0]`、
     `Trigger.adverse[label] = result[1]`、`Trigger.favorable[label] = result[2]`
     （`Trigger.returns` はホライズン別の前向きリターン辞書で、引数 `returns`（足ごとの r）とは別物）
   - `Trigger.returns[NET][label] = math.log(float(bars[t + horizon.bars].ask_close) / float(entry_bid))`
   - `layer, first, overlap = classify_layer(bars[t].bar, interventions, policies)`（§4.5）

### 4.5 層別

窓は label 軸の `datetime` 閉開区間 `[start, end)`:

- `intervention_windows(episodes: Sequence[Episode]) -> list[InterventionWindow]`:
  `InterventionWindow(day_start, day_start + SHOCK_WINDOW, episode.cluster == episode.action_date)`、
  `day_start = datetime.combine(episode.action_date, time(0), tzinfo=UTC)`。次のエピソードで窓を切り詰め
  **ない**（`shock_anchors` と違い、層別は「いずれかの窓に入るか」だけを見る）
- `policy_windows(decisions: Sequence[EventEnvelope], anchor: timedelta) -> list[PolicyWindow]`:
  `label = known_to_broker_label(decision.known_at, anchor)`、`PolicyWindow(label, label + POLICY_WINDOW)`
- `classify_layer(bar: Bar, interventions, policies) -> tuple[str, bool, bool]`（純関数。
  `(layer, first, overlap)` を返す）:
  - `in_a = any(window.start <= bar.start < window.end for window in interventions)`
  - `first = any(window.first and window.start <= bar.start < window.end for window in interventions)`
    （同じ足が初回窓と重複窓の両方に入る場合、初回窓に入っていれば `first = True`）
  - `in_b = any(window.start < bar.close_time and bar.start < window.end for window in policies)`
    （公表時刻を含む足も、公表後 24h 以内に始まる足も B）
  - `layer = "A" if in_a else "B" if in_b else "C"`、`overlap = in_a and in_b`、
    `first = first if in_a else False`

### 4.6 集計

- `cell_stats(triggers, layer_filter, kind, horizon, seed) -> Stats`（名前は実装者が整えてよい）:
  対象トリガーの `returns[kind][horizon.label]` から `count / mean / median / hit_rate（負の割合）/
  low / high（bootstrap_interval）`。`adverse` / `favorable` は kind によらず gross の excursion の平均
  （net の excursion は定義しないので gross の値を流用する。legend に明記する）。対象が空なら
  `Stats(0, *([float("nan")] * 7))`（`intervention_event_study.stats` と同じ）
- seed: ホライズン index ごとに `seed + index`（`intervention_event_study._summary_lines` と同じ）。
  `seed` は CLI の `--seed`
- 層フィルタ: `"A"`（layer == A）、`"A-first"`（layer == A かつ first）、`"B"`、`"C"`
- 無条件ベースライン: 主仕様の有効トリガー全体（層を問わない）の `min(entry)` から
  `max(entry) + horizon.bars` までの bid 足区間に `unconditional(span, horizon.bars, seed)`
  （`intervention_event_study.baseline_span` と同じ思想。gross のみ）。有効トリガーがなければ
  `Stats(0, ...)`

### 4.7 判定規則（純関数）

```python
H2 = "H2 支持（介入固有）"
H3 = "H3 支持（ショック一般）"
H2_STRONG = "H2 強化（通常ショックは逆張り）"
NOT_TRADABLE = "統計現象だが非取引可能"
REJECTED = "H2/H3 棄却"
VERDICT_HORIZONS = ("1h", "4h")

def negative_excluding_zero(stats: Stats) -> bool: return stats.high < 0
def positive_excluding_zero(stats: Stats) -> bool: return stats.low > 0
def includes_zero(stats: Stats) -> bool: return stats.low <= 0 <= stats.high

def judge(a_net: Stats, c_net: Stats, a_gross: Stats) -> str | None:
    """1 ホライズン分。表の上から順に評価し、該当なしは None。"""
    if negative_excluding_zero(a_net) and includes_zero(c_net): return H2
    if negative_excluding_zero(a_net) and negative_excluding_zero(c_net): return H3
    if negative_excluding_zero(a_net) and positive_excluding_zero(c_net): return H2_STRONG
    if negative_excluding_zero(a_gross) and includes_zero(a_net): return NOT_TRADABLE
    return None

def verdict(cell: CellResult, seed: int) -> tuple[str, str | None]:
    """主仕様の判定。1h → 4h の順に judge を評価し、最初に None でない結果を返す
    （返り値は (判定, 決めたホライズン)。どちらも None なら (REJECTED, None)）。"""
```

- NaN（n < 2 で CI が NaN）との比較は False になるので、そのまま「該当なし」に落ちる。追加の分岐を
  書かない
- 出力行: `verdict (primary N=96 K=4, net): <判定> [decided at 1h]`、棄却なら
  `verdict (primary N=96 K=4, net): H2/H3 棄却`

### 4.8 出力レイアウト（プレーンテキスト、既存スタディと同型。PowerShell から貼れる固定幅）

```
shock trigger study
git_commit=<sha> git_dirty=<bool>[ git_diff_sha256=<sha>]
ticks=<tick_count> first=<first_tick iso> last=<last_tick iso> dataset_hash=<sha>
5m bars=<n> intervention episodes=<n> (<n> cluster anchors) policy decisions=<n>
grid: N in {48, 96, 288} x K in {3, 4, 5}; primary N=96 K=4; horizons 15m/1h/4h; seed=<seed>
trigger: z = (r - mean) / pstdev over the last N contiguous 5m log returns, fires when z < -K and r < 0
negative = yen appreciation = short USD/JPY wins; gross = bid close -> bid close; net = bid close -> ask close (short round trip)
adverse/favour are gross excursions (also shown on net rows); spread = ask - bid at entry, yen
layers: A = within 36h of an intervention action_date 00:00 (label), A-first = cluster-first episodes, B = within 24h of a BOJ/FED decision, C = other; A wins over B
unconditional covers the stretch the primary triggers reach over (gross, existing helper)

grid summary (net 1h mean and CI90 by layer)
   N  K  triggers  suppr  invalid     A  A-first     B     C   A&B   A mean [CI90]           B mean [CI90]           C mean [CI90]
  48  3       ...
  ...（9 行。N 昇順 × K 昇順）

primary N=96 K=4
horizon 1h (12 x 5m)
                         n     mean   median    hit adverse  favour  CI90
  A net               ...
  A gross             ...
  A-first net         ...
  A-first gross       ...
  B net               ...
  B gross             ...
  C net               ...
  C gross             ...
  unconditional       ...
（15m / 1h / 4h の 3 ブロック）
spread at entry (yen): A <mean> A-first <mean> B <mean> C <mean>   （ホライズンに依らないので 1 行だけ。
                                                                     n=0 の層は "-"）

primary N=96 K=4 layer A triggers
bar start (label) | JST | episode | cluster | z | r % | spread | net 1h % | net 4h %
...（A 層の有効トリガーを entry 順に 1 行ずつ。episode は該当した介入窓の action_date。複数窓に入る
    場合は最も遅い action_date。cluster は "anchor" / "overlap YYYY-MM-DD"）

verdict (primary N=96 K=4, net): <判定>[ decided at 1h|4h]
```

- 集計行は `_row` をそのまま使う（`_row` は `Stats` 1 つを固定幅にする）。gross と net で `Stats` を
  別に作る
- `mean` などは `_row` 内で `* 100`（%）される。grid summary の `A mean [CI90]` も % で
  `f"{mean*100:>7.2f} [{low*100:>6.2f},{high*100:>6.2f}]"`、n が 0 なら `"      - [     -,     -]"`
- A 層トリガー一覧の JST は `broker_label_to_known(bar.start, anchor).astimezone(JST)`
- `report(...) -> str` は純関数（DB に触れない）で、テストから合成データで呼べるようにする。
  引数: `cells: Sequence[CellResult]`（9 セル、グリッド順）、`bars`、`bid_bars`、`episodes`、
  `decisions`、`provenance`、`git: dict`、`anchor`、`seed`。`git_state()` は `main()` で呼び、`report`
  には辞書を渡す（テストで subprocess を走らせない）

### 4.9 main

`intervention_event_study.main()`（:609-651）と `policy_event_study.main()`（:478-500）の作法に合わせる:

1. `argparse`: `--env`（既定 `"backtest"`）、`--symbol`（既定 `SYMBOL`。違う値なら
   `raise SystemExit(f"this study is about {SYMBOL}, not {symbol}")`）、`--seed`（`int`、既定
   `BOOTSTRAP_SEED`）
2. `config = load_config(args.env)`、`dsn = os.environ.get(config.storage.dsn_env)`、未設定なら
   `SystemExit`
3. `from trading.storage.postgres import PostgresEventRepository, PostgresMarketTickRepository, connect`
   は関数内 import（既存と同じ。db extra なしでも module が import できるように）
4. `now = datetime.now(UTC)`（CLI の main 内なので可）。
   `bars, provenance = fold_quote_bars(PostgresMarketTickRepository(conn).stream_between(symbol, EPOCH,
   now + BROKER_CLOCK_MARGIN), symbol, sys.stderr)`。空なら `SystemExit(f"no stored quotes for {symbol}")`
5. `events = PostgresEventRepository(conn)`、
   `episodes = load_episodes_from_events(events.known_before(now, EVENT_TYPE))`（`EVENT_TYPE` は
   `intervention_event_study` の `INTERVENTION_REPORTED`）。空なら既存と同じメッセージで `SystemExit`
6. `decisions = current_version([event for event_type in EVENT_TYPES.values() for event in
   events.known_before(now, event_type)])`
7. `anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)`
8. 9 セルを `for lookback in LOOKBACKS: z = z_scores(...); for threshold in THRESHOLDS: detect(...)`
   （z は N ごとに 1 回だけ計算する）
9. `print(report(...))`

### 4.10 数値型

- 価格・スプレッドは `Decimal`（`Bar` / `Tick` の型のまま）。対数リターン・z・統計は `float`
  （`math.log(float(...) / float(...))`。既存スタディと同じ）

## 5. 変更対象ファイル（網羅）

| ファイル | 種別 | 内容 |
|---|---|---|
| `config/intervention_episodes.yaml` | 変更 | 2026-07-31 の REPORTED エントリを末尾に追加（§2 作業 1） |
| `src/trading/backtest/shock_trigger_study.py` | 新規 | CLI 本体（§4） |
| `tests/unit/test_shock_trigger_study.py` | 新規 | 合成データの単体テスト（§6） |
| `tasks/shock-trigger-study.md` | 新規 | この計画（コミットに含める） |

上記以外は触らない。特に `policy_event_study.py` / `intervention_event_study.py` /
`rate_differential_study.py` / `bars.py` / `domain/market.py` / `migrations/` / `config/policy_meetings.yaml`
（別ブランチが並行編集中）は変更しない。`docs/PROJECT_STRUCTURE.md` は backtest 配下の個別スタディを
列挙していないので更新不要。

## 6. テスト方針（`tests/unit/test_shock_trigger_study.py`、合成データのみ）

モジュール docstring は日本語。ヘルパ:

- `T0 = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)`（月曜相当。`tests/support.T0` でもよい）
- `q5(index, bid_close, ask_close=None, *, high=None, low=None, start=None) -> QuoteBar`:
  `start = T0 + 5 分 × index`。`ask_close` 省略時は `bid_close + 0.010`
- `series(closes: Sequence[str], spread="0.010") -> list[QuoteBar]` 連続足
- `ANCHOR = timedelta(hours=7)`

テスト（関数名は内容が読めれば変えてよい）:

1. `test_fold_quote_bars_pairs_bid_close_with_last_ask`: `make_tick` で 1 バケツに 3 tick（ask が
   単調でない）+ 次バケツの tick 1 本。閉じた足の `bar.close` が最後の bid、`ask_close` が最後の ask。
   `Provenance.tick_count == 4`、`first_tick` / `last_tick`
2. `test_fold_quote_bars_ignores_straggler_for_ask`: 閉じたバケツより前の時刻の tick（遅着）が
   `ask_close` を変えない（2 バケツ目に遅着 tick を混ぜ、3 バケツ目で閉じる）
3. `test_log_returns_skip_non_contiguous_pairs`: 3 本連続 → 週末相当の飛び（`start` を 2 日ずらす）→
   2 本。飛びの直後の足の r が `None`、他は `ln(close/prev)`
4. `test_z_scores_need_lookback_valid_returns_and_exclude_current`: `lookback=4`。有効 r が 4 本たまる
   までは `None`、その次の足で `(r − μ) / σ` に一致（μ/σ は自前計算）。非連続を 1 つ挟んでも window が
   リセットされず、飛びの足自体は `None`
5. `test_trigger_fires_only_below_threshold_with_negative_return`: `detect` に `returns` / `z` を
   手で作ったリストで渡す（価格から作ると z がちょうど −4.0 にならない）。連続な合成足 60 本の上で、
   (a) `z[5] = -4.0`, `returns[5] = -0.001` → K=4 で発火しない、(b) `z[5] = -4.01` → 発火して
   `triggers` 1 件、(c) `z[5] = -6.0`, `returns[5] = +0.0001` → 発火しない。他の index は
   `z = 0.0`、`returns = 0.0`。`z_scores` と価格の整合は 4 で見る
6. `test_second_trigger_within_forward_window_is_suppressed`: 発火から 48 本以内にもう 1 度候補を作る
   → `triggers` 1 件、`suppressed == 1`。49 本目以降の候補は発火する
7. `test_trigger_with_gap_inside_forward_window_is_invalid`: 発火足の 20 本先に非連続 → `invalid == 1`、
   `triggers` 空。系列末尾で 48 本届かない場合も `invalid`
8. `test_net_is_settled_at_ask_and_spread_is_recorded`: spread 0.020 の合成足で、各ホライズンの
   `net − gross == ln(ask_exit / bid_exit)`（`pytest.approx`）、`spread == Decimal("0.020")`、
   `adverse` / `favorable` が `window_outcome` の値と一致
9. `test_layers_assign_intervention_policy_other_and_overlap_goes_to_a`: エピソード（`Episode`
   直接生成、初回と重複の 2 件）から `intervention_windows`、決定イベント（`EventEnvelope`、
   `known_at` 実 UTC）から `policy_windows(decisions, ANCHOR)` を作り、`classify_layer` で
   (a) 介入窓内 → A / first、(b) 重複エピソードの窓のみ → A / not first、(c) 政策窓内
   （公表時刻を含む足も含む）→ B、(d) どちらでもない → C、(e) 両方 → A かつ `overlap`。
   政策窓の label は `known_to_broker_label(known_at, ANCHOR)` で計算して足の `start` を置く
10. `test_judge_covers_every_branch`: `Stats` を直接組み立て、H2 / H3 / H2_STRONG / NOT_TRADABLE /
    None の 5 ケース（`pytest.mark.parametrize`）。`verdict` は 1h が None で 4h が H2 なら
    `(H2, "4h")`、両方 None なら `(REJECTED, None)`
11. `test_report_lists_every_cell_and_the_verdict`: 約 400 本の連続合成足（最初の 300 本は小さな
    交互リターン、その後に大きな下落 1 本、続けて 60 本以上の平坦）で、N=288 を含む複数セルが発火する
    ようにし、主仕様の 9 セル分の `CellResult` を `log_returns` / `z_scores` / `detect` で作る。
    下落足を含む介入窓を持つ `Episode` 1 件（A 層トリガー一覧の経路を通す）と、決定イベント 0 件で
    `report(...)` を呼び、例外なく文字列を返し、`grid summary` に 9 行、
    `primary N=96 K=4 layer A triggers` の下に 1 行、`verdict (primary N=96 K=4, net):` を含む
    ことを確認する（`git` は `{"git_commit": "test", "git_dirty": False}`、`provenance` は手で作る）。
    **この smoke テストは必須**（VPS で 30〜60 分の畳み込みの後に整形で落ちるのを防ぐ）

テストデータに実在の人物・団体名を使わない。`datetime.now()` を使わない。

## 7. 完了条件（実行可能コマンド。worktree の `.venv` を使う）

```bash
cd /Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+shock-trigger-study
.venv/bin/ruff check .                                   # 無変更で通る
.venv/bin/pytest tests/unit/test_shock_trigger_study.py -q
.venv/bin/pytest tests/unit -q                           # test_invariants.py を含め green
.venv/bin/pytest tests/unit/test_intervention_event_study.py tests/unit/test_policy_event_study.py tests/unit/test_rate_differential_study.py tests/unit/test_intervention_collectors.py -q   # 変更なしで通る
.venv/bin/python -c "import trading.backtest.shock_trigger_study"   # db extra なしでも import できる
```

## 8. やらないこと

- `policy_event_study.py` / `intervention_event_study.py` / `rate_differential_study.py` の変更
- SPA / DSR / PBO 等の多重検定補正の実装（9 仕様の全セル報告で代替）
- `Bar` / `Tick` モデルやスキーマ（migrations）の変更
- 戦略（`src/trading/strategy/`）への配線
- 2026 年 8 月の介入日の推測追加（日次内訳は 11 月公表まで不明）
- `config/policy_meetings.yaml` への変更（別ブランチが並行編集中）
- 研究ノート（`docs/research/`）の追加・更新（実データの実行はマージ後に VPS で行う）
- 周辺リファクタ・無関係な整形・追加の抽象化・将来のための汎用化
- コミット（Claude 側が行う）

## 9. 規約（`AGENTS.md` と `.claude/rules/` から転記。Codex 向け）

- 金額・数量・価格・スプレッドは `Decimal`。統計計算（対数リターン・z・平均・CI）のみ float 可
- frozen dataclass / frozen pydantic モデル + 新しい値を返す。引数や共有オブジェクトを破壊しない
- 検証はシステム境界（DB・設定・CLI 引数）だけ。内部関数間に防御的分岐・フォールバック・
  「起こり得ないケース」の `None` チェックを足さない
- WHAT を説明するコメントは書かない。「なぜ」だけを docstring かコメントに書く。「〜のために追加」
  「レビュー指摘対応」のようなコミット文脈依存のコメント、AI レビューの引用を残さない
- 通貨ペア・pip・時間足をハードコードしない（`SYMBOL` / `TIMEFRAME` / `Horizon` 定数を経由する）。
  本スタディは `SYMBOL = "USDJPY"` を E′ から import し、`--symbol` はそれ以外を拒否する
- Strategy / LLM 層から Broker・OMS・DB へ到達しない（本モジュールは backtest 配下の研究 CLI で、
  DB を読むのは `main()` だけ）
- Strategy 内で `datetime.now()` を呼ばない（本モジュールは Strategy ではないので `main()` 内の
  `datetime.now(UTC)` は既存スタディと同じく可。`report` 以下の純関数では呼ばない）
- ruff（`pyproject.toml` の設定、`line-length = 100`）に準拠。型注釈を付ける。
  `from __future__ import annotations`。§4.8 の長い legend 行は文字列リテラルを分割して 100 桁に収める
- テストは pytest。共有ファクトリは `tests/support.py`。実在の人物・団体名を使わない。
  `tests/unit/test_invariants.py` を通すためにテスト側を緩めない
- コメント・docstring は日本語を優先（既存の英語 docstring は維持してよい）
- 小さいファイルを多く（目安 200〜400 行、上限 800 行）。本モジュールは 1 ファイルで 500 行前後を想定。
  800 行を超えそうなら報告する（分割は計画外なので勝手にしない）

## 10. 仮定と未確認事項（実装で迷ったらここを優先）

- 「エピソードの kind は問わない」は、Layer A の窓をアンカー種別（shock / news）や認識段階で区別しない
  という意味に取り、エピソードの読み込みは既存 `load_episodes_from_events`（`INTERVENTION_REPORTED` かつ
  `JPY_BUY`）をそのまま使う。現行 YAML のエントリは全て `REPORTED` / `JPY_BUY` なので結果は同じ
- 「1h または 4h」は、1h → 4h の順に判定規則を評価し、最初に該当した分岐を採る（§4.7）。
  決めたホライズンを判定行に併記する
- 抑止は bar index で数える（`t <= last + 48`）。無効（前向き窓非連続）になったトリガーも抑止の起点になる
  （初撃の意味を優先）
- 政策窓の判定は「公表時刻を含む足（`bar.close_time > label`）から 24h 以内に始まる足まで」
- 無条件ベースラインは既存 `unconditional`（gross、5 日以上の穴だけを除く）をそのまま使う。トリガー側の
  「連続 4h 窓」より緩い基準だが、既存スタディとの比較可能性を優先する（legend に明記）
- 実行時間の見積り: tick の畳み込みが支配的（VPS で 30〜60 分、介入スタディと同じ）。z の計算は N ごとに
  35 万本 × N の合計 2 回で数十秒、bootstrap は主仕様 4 層 × 3 ホライズン × 2 種 + grid 9 セル × 3 層で
  数十回（各 2,000 標本）。K=3 の C 層で n が数千でも 1〜2 分に収まる。並列化・キャッシュは足さない
- 2026-07-31 エントリの追加により、介入スタディ（`intervention_event_study`）を再実行すると 07-30 の
  探索窓が次エピソードの 00:00 で切られて 24 時間になる（`shock_anchors` の `next_start`）。コードは
  変更しないが結果は変わり得る。PR 本文で明記する（Codex の作業ではない）
