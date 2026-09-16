# issue #157 候補 1: レンジ端の反転（`range_edge_reversal`）の実装と事前登録

- リポジトリ: `yusuke3627/fx-trading-platform`
- worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+issue-157-range-edge-reversal`
- ブランチ: `feat/issue-157-range-edge-reversal`（base は `origin/main` = `d986c34`）
- 実装担当: Codex CLI（この計画だけを読んで実装する。issue や会話履歴は読みに行かない前提）
- コミット・PR は Claude 側が行う。**Codex はコミットしない**
- 作業前に読むもの: `AGENTS.md`、`.claude/rules/workflow.md`、`.claude/rules/change-management.md`、`.claude/rules/testing-project.md`

---

## 1. 目的と範囲

issue #157 は Meme/壇上の記事を元に 3 候補を挙げ、候補 1「レンジ端の反転」を最優先の検証対象としている。issue は「測定前に確定する項目」（セッションの扱い、移動平均の種類と閾値、「一度割る→戻る」の対象足、quote と RR、保有期限の優先順位、判定規則）を自分で列挙しており、本 PR はそれを**事前登録**として確定し、検証可能な研究戦略 `range_edge_reversal` を実装する。

- **本 PR では測定しない**（VPS での run は別途。結果・判定は run 後に別 PR で研究ノートへ追記する）
- 候補 2（初回押し目）・候補 3（通貨強弱）は範囲外
- §2 の数値・規則はユーザー承認済みの事前登録。**実装の都合で変えたくなっても変えない。** 矛盾や実装不能に気づいたら、変えずに「未確認項目」として報告する（事前登録の意味が失われるため）

成果物:

1. `src/trading/indicators/session.py` に `session_end` を追加
2. `src/trading/strategy/intraday/range_edge_reversal.py`（新規）
3. `src/trading/strategy/registry.py` に登録
4. `config/base.yaml` の `strategies` に `range_edge_reversal` を追加
5. テスト: `tests/unit/test_range_edge_reversal.py`（新規）、`tests/unit/test_session.py` と `tests/unit/test_config.py` に追加
6. 研究ノート `docs/research/2026-09-17-h6-range-edge-reversal-preregistration.md`（新規。**Claude が書く**。Codex は触らない。実装レビュー時にコードとの整合だけ確認する）

---

## 2. 事前登録（確定済み。このまま実装する）

戦略 ID `range_edge_reversal`、`strategy_version = "0.1.0"`、horizon `INTRADAY`、status `RESEARCH_ONLY`、instruments `[USDJPY]`、timeframes `regime: 1h` / `entry: 5m`。

### 2-1. セッションとレンジ

- セッションは既存の `src/trading/indicators/session.py` の窓をそのまま使う（Tokyo 09–18 Asia/Tokyo、London 08–17 Europe/London、New York 08–17 America/New_York。IANA tz なので DST は自動）
- レンジは「いずれかのセッションが開始した時点」で確定し、次のセッション開始まで固定する。London と NY が重なる時間帯に NY が開始したら NY 開始で作り直し、それ以前のレンジは失効する
- レンジ = 開始時点で確定済みの直近 `range_lookback_bars`（24）本の 1h 足の高値・安値。24 本の最古の足が開始時点の `range_stale_hours`（72）時間（暦）より前なら欠測とみなし、そのセッションは対象外にする
- 1h 足がレンジ外で確定した時点で、そのレンジでの新規建玉を終える（`range_invalidated`）

### 2-2. レジーム（横ばい）判定

- 1h の EMA(`ema_period` = 20) の現在値と `range_slope_lookback`（6）本前の値の差の絶対値が ATR(`atr_period` = 14, 1h) × `range_slope_max_atr`（0.5）以下
- レンジ幅 H = 高値 − 安値 が ATR(14, 1h) × `range_width_min_atr`（1.5）以上
- どちらかを満たさなければそのセッションは対象外

### 2-3. トリガー

- 買い: 5m 確定足の安値がレンジ下端を割る（breach）。その足を含めて `reentry_max_bars`（6 本 = 30 分）以内に 5m 終値がレンジ内へ戻ったら成立。反転極値 = breach 足から成立足までの最安値
- 成立時の約定予定価格（買いは ask）が 下端 + `entry_band_fraction`（0.2）× H 以下であること。満たさなければ見送る
- 売りは上下対称（bid が 上端 − 0.2 × H 以上）
- 同じレンジ・同じ方向は 1 回だけ（`setup_id` にレンジの識別子を入れ、方向は `_new_setup` の slot で分ける）

### 2-4. 損切り・利確・RR

- 買いの損切り = 反転極値 − ATR(14, 5m) × `stop_buffer_atr`（0.25）。売りは 反転極値 + 同じ幅
- 利確 = レンジ中央（高値と安値の中点、セッション中は固定）。`take_profit_distance_pips` で signal に載せる（ADR-038）。`take_profit_enabled: false` のときは載せない（RR の判定はそのまま行う）
- RR: (中央 − 約定予定価格) ≥ `min_reward_to_risk`（1.5）× (約定予定価格 − 損切り) を満たさなければ見送る。売りは対称
- スプレッド gate は scalp と同じ `SpreadGate.from_params(params)` を使う（`max_spread_to_atr` 0.5、USDJPY の `absolute_max_spread_pips` 1.5）。ATR は entry 足（5m）の ATR(14)

### 2-5. 出口と保有期限

- 出口の優先順位は (1) broker 側の SL/TP（`ProtectionSpec`）、(2) 時間切れ決済 60 分（ADR-036、`horizon_exit_enabled: true` / `expected_horizon_seconds: 3600`）、(3) セッション閉鎖時の exit-only（既存の base の仕組み）
- ロールオーバー回避: 開いているセッションのうち最も遅く終わるものの終了まで `session_end_buffer_seconds`（3600）未満なら新規建玉を出さない。NY 終了 17:00 NY が broker の日付変更なので、これで rollover を跨がない
- 研究リプレイに再起動・注文拒否は無いので、live での期限内決済の保証は本測定の範囲外（研究ノートに明記。コードでは扱わない）

### 2-6. 通貨強弱

- 候補 1 では使わない（feature を読まない）

### 2-7. データ・指標・採否

研究ノートの事前登録節に書く（Claude が書く）。本 PR では測定しない。実装には関係しないので Codex はここを読み飛ばしてよい。

---

## 3. 設計（調査済み。このとおりに実装する）

§2 の文言が実装上どう解釈されるかは、末尾の「確定した実装上の解釈」に Claude の判断として固定してある（§2 の数値・規則は変えていない）。以下の設計はその解釈を前提にしており、**このとおりに実装する**。

### 3-1. `src/trading/indicators/session.py`: `session_end` を足す

既存の `sessions_at` / `session_start` / `SESSION_WINDOWS_LOCAL` は変えない。`session_start` の対になる関数を足す。

```python
def session_end(session: Session, ts: datetime) -> datetime:
    """End of the session window `session_start` picks for `ts` (UTC)."""
    timezone = _SESSION_TIMEZONES[session]
    _, _, end_hour = SESSION_WINDOWS_LOCAL[session]
    start_local = session_start(session, ts).astimezone(timezone)
    end = datetime.combine(start_local.date(), time(hour=end_hour), tzinfo=timezone)
    return end.astimezone(UTC)
```

- 同じローカル日の終了 hour を IANA tz で組み、UTC へ正規化する。DST は `session_start` と同じ仕組みで自動的に追従する
- import は `trading.indicators.session` から直接行う。`indicators/__init__.py` への再公開や `IndicatorService` のメソッド追加は不要。現在の 3 窓はいずれも同日内で終了する
- 「開いているセッションのうち最も遅く終わるものの終了時刻」は strategy 側の private 関数で `max(session_end(s, now) for s in sessions_at(now))` として求める（session.py には足さない）

### 3-2. 新規 `src/trading/strategy/intraday/range_edge_reversal.py`

`src/trading/strategy/intraday/post_event_failed_breakout.py` を構造の手本にする。

```python
class RangeEdgeReversalStrategy(Strategy):
    strategy_id = "range_edge_reversal"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY
```

モジュール docstring は既存戦略と同じく英語でよい。研究仮説（レンジ端で失敗した抜けは中央へ戻る）、利確と時間切れで決済を H4/H5 の protection fill 偏重から変える狙い、timeframe と閾値は設定から来ることを短く書く。

#### パラメータ（戦略固有の既定値と、共通 base のフラグを区別する）

この戦略が `params.param(name, default)` で読む値の既定は config と同じにする。共通 base が読む `horizon_exit_enabled` は下表の例外とする。

| 名前 | 既定 | 用途 |
|---|---|---|
| `range_lookback_bars` | 24 | レンジを作る 1h 足の本数 |
| `range_stale_hours` | 72 | 最古の足がこれより古ければ欠測 |
| `range_slope_lookback` | 6 | EMA の傾きを測る本数 |
| `range_slope_max_atr` | 0.5 | 傾きの上限（ATR 1h 倍） |
| `range_width_min_atr` | 1.5 | レンジ幅の下限（ATR 1h 倍） |
| `ema_period` | 20 | 1h EMA |
| `atr_period` | 14 | 1h / 5m の ATR 期間（共通） |
| `reentry_max_bars` | 6 | breach 足を含めて戻りを待つ 5m 足の本数 |
| `entry_band_fraction` | 0.2 | 端からの許容帯（H 比） |
| `stop_buffer_atr` | 0.25 | 反転極値の外側に足す ATR(5m) 倍 |
| `min_reward_to_risk` | 1.5 | RR 下限 |
| `take_profit_enabled` | True | 利確距離を signal に載せるか |
| `expected_horizon_seconds` | 3600 | signal と `_horizon_exit(..., default_horizon_seconds=3600)` の期限 |
| `horizon_exit_enabled` | False（base の既定） | base が読む。§2 の有効化はこの戦略の config / テストで明示的に True を渡す。base の既定値は変えない |
| `session_end_buffer_seconds` | 3600 | セッション終了前の新規停止 |

`spread_gate` / `absolute_max_spread_pips` / `session_profile` は既存の仕組み（`SpreadGate.from_params`、`StrategyConfig.session_profile_for`）が読む。
`params_for` の戻り値は `ResolvedStrategyParameters` であり、任意のキーを属性や添字で読まず `params.param(name, default)` を使う。`horizon_exit_enabled` だけは共通 base の既定 False と、この戦略の config 値 True を区別する。

#### `warmup` / `bar_window`（classmethod。既存戦略の流儀）

- `regime_tf = config.timeframes.role("regime", "1h")`、`entry_tf = config.timeframes.role("entry", "5m")`
- 1h 側の読取本数 `regime_count = max(range_lookback_bars, ema_period + range_slope_lookback, atr_period + 1)`（既定で 26）
- 5m 側の読取本数 `entry_count = reentry_max_bars + 1`（既定で 7。7 本目の戻りを「6 本以内ではない」と判定するために 1 本余分に読む）。ATR(5m) は IndicatorService が `max(DEFAULT_BAR_COUNT, atr_period + 1)` 本読む
- `warmup`: `span = max(regime_count * TIMEFRAME_SECONDS[regime_tf], (atr_period + 1 + entry_count) * TIMEFRAME_SECONDS[entry_tf])` を `market_span_to_calendar(span)` へ
- `bar_window`: `max(DEFAULT_BAR_COUNT, regime_count, atr_period + 1, entry_count)`
- `tick_window_seconds` は上書きしない（`ctx.market.ticks` を使わず `ctx.market.latest_tick` だけ読む。`ReplayMarketData` / `InMemoryMarketData` / `StoredMarketData` の全てが `latest_tick` を持つ）
- 複数 instrument の params は `[config.params_for(symbol) for symbol in config.instruments or [""]]` の max を取る（既存と同じ）
- 上記案の既定値は `warmup == timedelta(days=3, hours=12, minutes=24)`（26 時間 × 7/5 + 2 日）、`bar_window == 200`、`tick_window_seconds == 0.0`。`warmup` 中はバーが蓄積されるだけで strategy は評価されず、レンジ memo も作られない。レンジは「セッション開始後に最初に評価した市場イベント」でその時点に見える足から作る（解釈 1）ので、開始時点の足を遡る読み取りは要らない
- `tests/unit/test_research_runner.py::test_every_registered_strategy_computes_a_positive_warmup` は timeframes / parameters が空の `StrategyConfig` でも呼ぶ。role と各パラメータのフォールバックを維持する。`ReplayMarketData.bars` は宣言容量を超える count で例外になるため、実際に読む最大本数を `bar_window` に含める

#### レンジの状態（strategy 内 memo）

`StrategyContext` にフィールドを足さない。base の `_new_setup` / `_horizon_exits` と同じ流儀で `self.__dict__.setdefault("_ranges", {})` を symbol キーの dict として持つ。値はモジュール private の `NamedTuple`（新しい公開型は作らない）:

```python
class _Range(NamedTuple):
    session: Session        # レンジを作ったセッション
    start: datetime         # そのセッションの開始（UTC、session_start の戻り値）
    built_at: datetime      # 構築した時点の ctx.clock.now()
    high: float
    low: float
    eligible: bool          # regime_count 本が揃い、欠測でなく、レジーム条件を満たす
    invalidated: bool       # 1h 足がレンジ外で確定した
```

`(session, start)` がレンジの識別子で、`setup_id` にもこれを渡す。中央 `mid = (high + low) / 2` は都度計算でよい。

#### `on_event`（post_event と同じ順）

```python
async def on_event(
    self, event: EventEnvelope, context: StrategyContext,
) -> list[StrategySignal]:
    if not event.event_type.startswith("market."):
        return []
    signals = []
    for symbol in context.config.instruments:
        signal = self._horizon_exit(context, symbol, default_horizon_seconds=3600)
        if signal is not None:
            signals.append(signal)
            continue
        if not self._session_permits_evaluation(context, symbol):
            continue
        signal = self._evaluate(symbol, context)
        if signal is not None:
            signals.append(signal)
    return signals
```

`EventEnvelope` / `StrategySignal` は既存戦略と同じ import 元を使う。戻り値は signal のリストであり、不成立時は `[]`。`_evaluate` は `StrategySignal | None` を返す。

この順序では、profile が閉鎖中かつ保有なしのとき `_evaluate` は呼ばれない。したがって、セッション外の memo 削除を `on_event` 経由で必ず観測できるとは限らない。古い memo は次の許可された評価で異なる `(session, start)` に置き換わる。セッション閉鎖そのものを契機にした決済は追加しない（解釈 2）。時間切れ決済は `_horizon_exit` が gate より前に走るので、終了前の新規停止（下記 (b)）に影響されない。

#### `_evaluate(symbol, ctx)` の手順

時刻は `ctx.clock.now()` だけ（`datetime.now()` を呼ばない）。`spec = ctx.market.instrument(symbol)` が None なら None を返す。価格・価格差（レンジの高安・中央・帯・反転極値・損切り・risk / reward）は Bar / Tick の `Decimal` のまま計算し、float の ATR は `Decimal(str(atr))` で掛ける（float で比べると RR = 1.5 ちょうど・幅 = 1.5 × ATR ちょうどの境界を誤差で落とす）。EMA の傾きだけは指標値どうしの比較なので float でよい。`SpreadGate.allows` には Decimal の `spec.pip_size` を渡す。

**(a) レンジの維持**

1. `open_sessions = sessions_at(now)`。空なら `memo.pop(symbol, None)` して None を返す（セッション外にレンジは無い）
2. `session, start = max(((s, session_start(s, now)) for s in open_sessions), key=lambda item: (item[1], item[0].value))`。開いているセッションのうち最も遅く開始したものが現在のレンジの主。London/NY の重なりで NY が始まると `(NEW_YORK, NY 開始)` に変わる
3. memo に無いか `(session, start)` が memo と違えば、**このイベントで構築**する（「開始時点」= そのセッション開始後に strategy が最初に評価した市場イベント）:
   - `regime_bars = list(ctx.market.bars(symbol, regime_tf, regime_count))`。`len < regime_count` なら `eligible=False`（high/low は 0.0 でよい。eligible が False のレンジは何も出さない）
   - `range_bars = regime_bars[-range_lookback_bars:]`。`start - range_bars[0].known_at > timedelta(hours=range_stale_hours)` なら `eligible=False`（欠測。`known_at` は確定を観測した実 UTC なので `ctx.clock.now()` と直接比べられる。`start` は broker ラベルではなく IANA tz 由来の UTC）
   - `high = max(float(b.high) for b in range_bars)`、`low = min(float(b.low) for b in range_bars)`
   - `atr_regime = ctx.indicators.atr(symbol, regime_tf, atr_period)`。None か ≤ 0 なら `eligible=False`
   - EMA: `closes = [float(b.close) for b in regime_bars[-(ema_period + range_slope_lookback):]]`、`series = ema_series(closes, ema_period)`（`trading.indicators.ema.ema_series`。最初の `ema_period` 本の単純平均を初期値にする既存実装）。`slope = abs(series[-1] - series[-1 - range_slope_lookback])`。既定では series の長さはちょうど 7 で、「6 本前の値」は初期値（直近 26 本のうち古い 20 本の単純平均）に等しい。この定義を研究ノートにも書く（Claude が書く）
   - `eligible = slope <= range_slope_max_atr * atr_regime and (high - low) >= range_width_min_atr * atr_regime`
   - `memo[symbol] = _Range(session, start, now, high, low, eligible, invalidated=False)`
4. memo のレンジが `eligible` でなければ None
5. 失効判定（構築後の毎イベント）: `regime_bars = ctx.market.bars(symbol, regime_tf, regime_count)` を読み、`b.known_at > rng.built_at` かつ `not (rng.low <= float(b.close) <= rng.high)` の足が 1 本でもあれば `memo[symbol] = rng._replace(invalidated=True)`。`invalidated` なら None（次のセッション開始まで新規なし）

**(b) セッション終了前の新規停止**

`latest_end = max(session_end(s, now) for s in open_sessions)`。`latest_end - now < timedelta(seconds=session_end_buffer_seconds)` なら None。

**(c) 5m 側の材料**

- `atr_entry = ctx.indicators.atr(symbol, entry_tf, atr_period)`。None か ≤ 0 なら None
- `tick = ctx.market.latest_tick(symbol)`。None なら None。`SpreadGate.from_params(params).allows(spread=tick.spread, atr=atr_entry, pip_size=spec.pip_size)` が False なら None
- `entry_bars = list(ctx.market.bars(symbol, entry_tf, entry_count))`。`len < entry_count` なら None。`last = entry_bars[-1]`

**(d) 買い（下端）**

`self._session_permits_setup(ctx, symbol, PositionDirection.LONG)` が True のときだけ評価する（閉鎖中に signal になり得ない向きは分岐に入らない。入ると None を返して後続の売りを評価できなくなる。post_event と同じ）。

以下の「不成立」「見送り」は買い候補だけを落とし、売り候補の評価へ進む意味とする。買い条件の途中で `_evaluate` 全体から None を返すと、通常の売りケースも評価できない。買いの全条件を満たして `_setup_signal` を呼んだ場合は、その戻り値が重複抑止による None でもそのまま返す（既存戦略と同じ）。

1. `low <= float(last.close) <= high` でなければ不成立（終値がレンジ内へ戻っている）
2. `last` の直前から遡って `float(b.close) < low` が連続する本数を `run` とする（`entry_bars[-2]`, `[-3]`, … と見て、条件が切れたら止める。最大 `entry_count - 1` = 6）
   - `run == 0` なら `float(last.low) < low` が必要（breach と戻りが同じ足）。満たさなければ不成立
   - `run + 1 > reentry_max_bars` なら不成立（breach 足から数えて 7 本目以降の戻り）。`entry_count = reentry_max_bars + 1` 本読んでいるので `run == reentry_max_bars` を検出できる
   - breach 足 = `entry_bars[-(run + 1)]`。breach 足の `known_at` が `rng.built_at` 以下なら不成立（レンジ構築前に確定した足の割れは、このレンジの breach として数えない。解釈 5。足は時刻順なので breach 足だけ見れば成立足までの全足が構築後になる）。反転極値 `extreme = min(float(b.low) for b in entry_bars[-(run + 1):])`
3. `ask = tick.ask`（Decimal）。`ask > low + entry_band_fraction * (high - low)` なら見送り
4. `stop = extreme - stop_buffer_atr * atr_entry`、`risk = ask - stop`。`risk <= 0` なら見送り（戻り足の後に価格が反転極値を割り込んだ。signal の `stop_distance_pips` は正でなければ Portfolio が捨てる）
5. `mid = (high + low) / 2`、`reward = mid - ask`。`reward < min_reward_to_risk * risk` なら見送り
   - signal 化する直前に stop / TP の距離を 0.1 pip 単位へ丸める。正の risk / reward でも丸め後が 0 になる場合がある。stop が 0 以下なら見送り、TP 有効時は TP も 0 以下なら見送る。`StrategySignal.take_profit_distance_pips` は `gt=0` であり、0 を渡すと評価が例外で止まる。§2 の RR 判定は丸め前の距離で行い、既定値・閾値は変えない
6. signal:
   ```python
   return self._setup_signal(
       ctx,
       symbol=symbol,
       direction=PositionDirection.LONG,
       setup_id=(rng.session, rng.start),
       conviction=0.5,
       stop_distance_pips=Decimal(str(round(risk / pip, 1))),
       take_profit_distance_pips=(
           Decimal(str(round(reward / pip, 1))) if take_profit_enabled else None
       ),
       expected_horizon_seconds=horizon_seconds,
       reason_codes=["RANGE_REGIME_FLAT", "RANGE_LOWER_EDGE_REENTRY"],
   )
   ```
   conviction は固定 0.5（`PortfolioManager.intents_from_signal` の sizing はこの値を参照しない）。`expected_edge_r` は `_setup_signal` の既定 `Decimal(1)` のまま

**(e) 売り（上端）**: 上下対称。`self._session_permits_setup(ctx, symbol, PositionDirection.SHORT)` のときだけ。`run` は `float(b.close) > high` の連続本数、`run == 0` なら `float(last.high) > high`、breach 足の `known_at > rng.built_at` も同じ、`extreme = max(high)`、`bid = tick.bid`、帯は `bid < high - entry_band_fraction * (high - low)` なら見送り、`stop = extreme + stop_buffer_atr * atr_entry`、`risk = stop - bid`、`reward = bid - mid`、reason_codes `["RANGE_REGIME_FLAT", "RANGE_UPPER_EDGE_REENTRY"]`。買いを先に評価し、成立すればそれを返す。

`_setup_signal` → `_new_setup` は `setup_id=(session, start)` を slot `(symbol, direction, exit_only)` ごとに 1 つ記憶する。帯・RR で見送った試行は setup 用 memo に触れない。記録されるのは約定ではなく signal の生成であり、Risk・執行で拒否されてもその方向の slot は消費済みになる（解釈 3。既存戦略と同じで、再試行は足さない）。entry と exit-only は別 slot、時間切れは別 memo である。

**共通**: 指標（ATR / EMA）は float、価格と価格差は Decimal、pips は `Decimal(str(round(x / pip, 1)))`。金額・数量は扱わない。通貨ペア・pip・時間足をハードコードしない。feature（`ctx.features`）は読まない。

### 3-3. `src/trading/strategy/registry.py`

`RangeEdgeReversalStrategy` を import して `STRATEGIES` のタプルに足す（順序は既存の後ろでよい）。research runner の `--strategy` choices はここから出る。

### 3-4. `config/base.yaml`

`strategies` の `post_event_failed_breakout` の後（`monetary_policy_convergence` の前）に追加する。他の env overlay（`shadow.yaml` / `micro_live.yaml` / `production.yaml` / `backtest.yaml`）は触らない。research runner は `enabled` を見ない（`config.strategies` に存在すればよい）ので `backtest.yaml` への追記は不要。

```yaml
  range_edge_reversal:
    enabled: false
    status: RESEARCH_ONLY
    instruments: [USDJPY]
    timeframes:
      regime: 1h
      entry: 5m
    parameters:
      defaults:
        range_lookback_bars: 24
        range_stale_hours: 72
        range_slope_lookback: 6
        range_slope_max_atr: 0.5
        range_width_min_atr: 1.5
        ema_period: 20
        atr_period: 14
        reentry_max_bars: 6
        entry_band_fraction: 0.2
        stop_buffer_atr: 0.25
        min_reward_to_risk: 1.5
        take_profit_enabled: true
        horizon_exit_enabled: true
        expected_horizon_seconds: 3600
        session_end_buffer_seconds: 3600
        spread_gate:
          max_spread_to_atr: "0.5"
      instruments:
        USDJPY:
          absolute_max_spread_pips: "1.5"
          session_profile: usdjpy_core
```

`test_all_strategies_start_research_only_in_base` / `test_no_hardcoded_instruments_in_strategy_config` / `test_every_platform_instrument_has_a_spread_ceiling` などの既存 config テストはこの形で通る。

最後のテストが調べるのは `config.risk.absolute_max_spread_pips` であり、この戦略の ceiling ではない。戦略側の `absolute_max_spread_pips` は §4-3 で別途確かめる。`tests/unit/test_live_wiring.py::test_the_shipped_configuration_names_only_strategies_that_exist` は全 config ID の登録を要求するため、base.yaml と registry の追加は同時に行う。`test_invariants.py::test_one_strategy_file_one_canonical_id` は既存 3 クラスを明示列挙しており、registry 全体を 3 件に制限するテストではない。変更しない。

---

## 4. テスト

### 4-1. `tests/unit/test_session.py` に追加

- `session_end` が冬時間・夏時間で正しい UTC を返す（London: `2026-01-15 12:00 UTC` → `17:00 UTC`、`2026-07-15 12:00 UTC` → `16:00 UTC`。NY: `2026-01-15 18:00 UTC` → `22:00 UTC`、`2026-07-15 18:00 UTC` → `21:00 UTC`。Tokyo: `2026-01-15 05:00 UTC` → `09:00 UTC`）
- DST 切替日を跨ぐ 1 本: London は 2026-03-29 に BST へ移る。`2026-03-27 12:00 UTC`（GMT）→ 終了 `17:00 UTC`、`2026-03-30 12:00 UTC`（BST）→ 終了 `16:00 UTC`
- `session_start` と同じ窓を指すこと（`session_end(s, ts) > session_start(s, ts)`、差が 9 時間）。窓の前（`session_start` が前日の窓を返す時刻）でも前日の終了を返すこと（`test_session_start_uses_previous_local_day_before_window` と対）
- 固定 offset の server 時刻で渡しても UTC と同じ結果（既存 `test_session_results_are_independent_of_fixed_broker_offset` に `session_end` を足すか、同じ形の 1 本）
- 開始ちょうど・終了ちょうども確かめる。終了ちょうどは `sessions_at` から外れるが、`session_end` 自体はその日の終了を返す。既存と同様、週末を除外する機能は追加しない

### 4-2. `tests/unit/test_range_edge_reversal.py`（新規）

`tests/support.py` の `FixedClock` / `make_bar` / `make_tick` / `make_event` / `usdjpy_spec` / `held` を使う。`tests/support.py` の `evaluation_context` は post_event 専用なので使わず、このファイルに小さな組み立て関数を置く（`SimpleNamespace` の ctx。`test_horizon_exit.py` の `ctx_for` と同じ流儀）:

```python
USDJPY_CORE = SessionProfile(
    sessions={"tokyo": "ALLOWED", "london": "ALLOWED", "new_york": "PREFERRED"}
)


def range_context(
    regime_bars, entry_bars, tick, *, now, params=None, position=None,
    atr_regime=0.20, atr_entry=0.04, profile=USDJPY_CORE,
    status=StrategyStatus.RESEARCH_ONLY,
):
    instrument_params = {"absolute_max_spread_pips": "1.5"}
    if profile is not None:
        instrument_params["session_profile"] = "probe"
    config = StrategyConfig(
        strategy_id="range_edge_reversal",
        status=status,
        instruments=["USDJPY"],
        timeframes=TimeframeMap(regime="1h", entry="5m"),
        parameters=StrategyParameters(
            defaults={**DEFAULTS, **(params or {})},
            instruments={"USDJPY": instrument_params},
        ),
        session_profiles={} if profile is None else {"probe": profile},
    )
    clock = FixedClock(now)

    def bars(_symbol, timeframe, count):
        source = entry_bars if timeframe == "5m" else regime_bars
        return [bar for bar in source if bar.known_at <= clock.now()][-count:]

    return SimpleNamespace(
        config=config,
        market=SimpleNamespace(
            instrument=lambda _symbol: usdjpy_spec(),
            bars=bars,
            latest_tick=lambda _symbol: tick,
        ),
        indicators=SimpleNamespace(
            atr=lambda _symbol, timeframe, _period: atr_entry if timeframe == "5m" else atr_regime
        ),
        features=InMemoryFeatureStore(),
        clock=clock,
        portfolio=SimpleNamespace(
            position=lambda strategy_id, symbol: (
                position if position is not None
                and (position.strategy_id, position.symbol) == (strategy_id, symbol)
                else None
            )
        ),
    )
```

`DEFAULTS` はテストファイル内に §3-4 の `parameters.defaults` と同じ値で置く（config の読み込みは `test_config.py` 側で確かめる）。`horizon_exit_enabled: True` を省略しない。`StrategyStatus` / `SessionProfile` も import し、profile の名前と一覧を対にして渡す。`status=None` はモデルに渡さない。省略記号をキーワード引数の後へ残すと SyntaxError になるため、上の例では省略していない。補助関数を変数へ lambda で代入する書き方も避ける（ruff の E731）。

この ctx の ATR 0.20 / 0.04 は戦略の計算を独立に調べる注入値であり、下記 OHLC から求めた値ではない。1h 全足の high=150 / low=149 から実際に計算すると ATR は1.0、幅1.0は幅条件 `1.5 × ATR` を満たさない。また、5m 7本だけでは実際の ATR(14) は None になる。実 `IndicatorService` の接続確認は別の十分な本数・整合する OHLC を使う。

時刻は冬時間の固定 instant を使う（`test_session_entry_gate.py` と同じ流儀）。例:

- London のみ: `NOW = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)`（London 08:00–17:00 UTC、Tokyo は 09:00 UTC に閉じている。終了まで 7 時間）
- Tokyo のみ: `datetime(2026, 1, 15, 2, 0, tzinfo=UTC)`
- London / NY 重複中: `datetime(2026, 1, 15, 16, 30, tzinfo=UTC)`（最も遅い終了は NY の22:00 UTC。残り5時間30分なので新規停止にはならない）
- 最終セッション終了60分前: `datetime(2026, 1, 15, 21, 0, tzinfo=UTC)`、30分前: `datetime(2026, 1, 15, 21, 30, tzinfo=UTC)`
- セッション外: `datetime(2026, 1, 15, 23, 0, tzinfo=UTC)`

bars のヘルパー: 1h 足 `flat_regime_bars(anchor, count=30, high="150.00", low="149.00", close="149.50")` を、`i=0..count-1` として `start = anchor - timedelta(hours=count - i)`、`timeframe="1h"` で作る（open は close と同じ）。最新は `anchor`、最古は `anchor - 29h` に確定し、末尾24本の最古は `anchor - 23h` になる。通常例の `anchor` は対象セッションの開始時刻（London の NOW なら08:00 UTC）とし、§2 の開始時点ですべて既知にする。初回評価が遅れ、その間に確定した 1h 足がある場合は、その足も含めた「評価時点に見える直近 24 本」からレンジを作る（解釈 1）。この形も 1 本確かめる（開始後に確定した足の高値が最大なら、その値が `high` になる）。

5m 足は `start = now - timedelta(minutes=5 * (count - i))`、`timeframe="5m"` を**各 make_bar 呼び出しに明示**する。`make_bar` の start 既定は2026-08-13、timeframe 既定は1mなので、そのまま使うと1月の評価より未来の足になる。テストの tick / event も now を明示する。

買いの基本ケース（レンジ high 150.00 / low 149.00 / mid 149.50、ATR 1h 0.20、ATR 5m 0.04）:

- 5m 足7本（上記の時刻指定で作る。以下はopen/high/low/close）: 先頭5本は `("149.20", "149.30", "149.10", "149.20")`、6本目breach `("149.10", "149.15", "148.90", "148.95")`（終値 < low）、7本目戻り `("148.95", "149.08", "148.92", "149.05")`
- tick `make_tick("149.05", "149.06", time=now)`（スプレッド 1 pip。gate は 0.5 × 0.04 = 0.02 以下、絶対上限 1.5 pips 以下）
- 期待: `run = 1`、breach = 6 本目、極値 148.90、stop = 148.89、risk = 0.17 → `stop_distance_pips == Decimal("17.0")`、reward = 149.50 − 149.06 = 0.44 → `take_profit_distance_pips == Decimal("44.0")`（0.44 ≥ 1.5 × 0.17 = 0.255）、`desired_direction is LONG`、`exit_only is False`、`expected_horizon_seconds == 3600`、`reason_codes == ["RANGE_REGIME_FLAT", "RANGE_LOWER_EDGE_REENTRY"]`

`on_event` は `await strategy.on_event(make_event(known_at=now), ctx)` で呼ぶ（`pytest.mark.asyncio`）。

テスト（名前は `test_` + 挙動。対称ケース・境界値は parametrize 可）。下記の None は `_evaluate` の戻り値を指し、`on_event` 経由なら不成立 `[]`、成立 `[signal]` として検証する。各独立ケースで strategy を作り直し、memo の持ち越しは連続評価を検証するケースだけにする:

1. **セッション開始でレンジが確定し、次のセッション開始まで固定される**: Tokyo のみの時刻で評価し、`strategy._ranges["USDJPY"]` の `.session` / `.start` が `(Session.TOKYO, 2026-01-15 00:00 UTC)` で、high/low を保持することを確認する。同じ Tokyo 内で別の高安の1h足を追加しても high/low は変わらない（終値は旧レンジ内にし、失効テストと混ぜない）。London 開始後（08:30 UTC）には `(Session.LONDON, 08:00 UTC)` で再構築する。セッション外の削除は `_evaluate` の直接呼出しで確かめる。実 profile・保有なしの `on_event` は session gate で評価を省略するため、memo 削除を必須の期待にしない
2. **London/NY の重なりで NY 開始時に作り直す**: `13:30 UTC`（NY 08:30、London も開いている）→ `(Session.NEW_YORK, 13:00 UTC)`
3. **1h 足が揃わない・最古が 72 時間より前なら signal を出さない**: (a) 20 / 24 / 25本では None、26本なら材料が揃う。(b) `anchor = start - 50h` の30本は、末尾24本の最古が `start - 73h` となり対象外。`anchor = start - 49h` なら最古がちょうど72時間前なので鮮度条件を通る。閾値超過・本数不足の memo は `eligible is False`、同一セッション内で不足分が後から届いても再構築しない
4. **EMA の傾きが閾値を超える／レンジ幅が足りないと signal を出さない**: (a) 終値を149.00から1本あたり+0.05で30本作り、high=151.00 / low=148.00、open=close として OHLC を整合させる。末尾26本を EMA(20) に渡した series は7値、初値149.675、末尾149.975、差0.300で、閾値0.10を超える。(b) high=149.20 / low=149.00 / open=close=149.10（幅0.20 < 0.30）で対象外。傾き上限ちょうど・幅下限ちょうどは許可、超過・不足は拒否する境界も加える。境界用の計算例は後述
5. **下端を割って 6 本以内に戻ると買い**: 基本ケースは上記の期待値どおり。加えて、レンジ内の先行足1本 + breach + 終値 < low の4本 + 戻り1本の計7本（`run == 5`、breach から6本目）でも出る。同じ足で breach と戻りが成立する `run == 0` も買い・売りの両方で確かめる
6. **7 本目に戻っても出ない**: breach 1 本 + 終値 < low が 5 本 + 戻り 1 本（`run == 6`）→ None
7. **約定予定価格が下側 20% 帯の外なら見送る**: tick `("149.24", "149.25")` は帯の上限149.20を超えるので None。帯だけの境界テストは試験用 override `min_reward_to_risk=0.5` を明示し、基本ケースの ask=149.20は許可、149.201は拒否する（いずれも spread=0.01）。config / DEFAULTS の承認済み値1.5は変えない。既定1.5では、帯境界の reward=0.3H に対して risk > 0.2H なので RR < 1.5となり、signal が出る期待を置けない
8. **RR が 1.5 未満なら見送る**: `min_reward_to_risk` を既定のまま、極値を深くする（breach 足の low を148.60に → stop148.59、risk0.47、reward0.44 < 0.705）→ None。TP無効でも同じ RR 条件で見送る。RR ちょうど1.5は許可、直下は拒否する例は後述
9. **損切りと利確の距離**: 基本ケースで `stop_distance_pips == Decimal("17.0")` と `take_profit_distance_pips == Decimal("44.0")`。`take_profit_enabled: False` なら `take_profit_distance_pips is None` で、それ以外は同じ
10. **同じレンジ・同じ方向で 2 回目は出ない**: 基本ケースで signal を得た後、同一イベントの再評価と、5分後の別の breach → 戻りの両方で None。売りの形に変えれば反対方向は出る。次セッションでは同じ方向も再び許可する。帯・RRで見送った直後に条件を満たすケースでは slot を消費していないことを確認する
11. **1h 足がレンジ外で確定したら新規を出さない**: レンジ構築後、`built_at < known_at <= now` の1h足（high=150.40 / close=150.30）を追加し、時計もその確定時刻まで進める → None、`invalidated is True`。下抜けも対象とし、以後の足がレンジ内に戻っても失効は維持する。終値が high / low ちょうど、またはヒゲだけが外側の場合は失効しない。次のセッションでは失効状態を引き継がない
12. **セッション終了まで 60 分未満なら新規を出さない**: 冬時間の NY 終了22:00 UTCを基準に、20:59:59と21:00:00は許可、21:00:01と21:30:00は None。5m足は各時刻の直前の5分境界までに確定したものとし、clockとtickを各時刻に合わせる。H1はNY開始時の横ばいレンジを維持する。16:30 UTCはLondonとNYの重複中で残り5時間30分あるため、この停止条件では落ちない。22:00 UTCちょうどはセッション外として扱う
13. **売りは上下対称**: 5m 足を上下反転（breach `("149.90", "150.10", "149.85", "150.05")`、戻り `("150.05", "150.08", "149.92", "149.95")`）、tick `("149.94", "149.95")` → `SHORT`、極値 150.10、stop 150.11、risk = 150.11 − 149.94 = 0.17 → `Decimal("17.0")`、reward = 149.94 − 149.50 = 0.44 → `Decimal("44.0")`、reason_codes `["RANGE_REGIME_FLAT", "RANGE_UPPER_EDGE_REENTRY"]`
14. **スプレッド gate**: tick `("149.03", "149.06")`（3 pips > 1.5 pips、かつ0.03 > 0.02）→ None。独立した境界として ask=149.06を固定し、ATR=0.04で spread=0.015は許可、0.016は絶対上限だけで拒否する。spread=0.015固定では ATR=0.03は比率0.5ちょうどで許可、ATR=0.029は比率だけで拒否する
15. **registry と宣言**: `STRATEGIES["range_edge_reversal"] is RangeEdgeReversalStrategy`、`horizon is StrategyHorizon.INTRADAY`。§3の現案では既定 `warmup == timedelta(days=3, hours=12, minutes=24)`、`bar_window == 200`、`tick_window_seconds == 0.0`。regime / entry の時間足変更と銘柄 override で必要量が増えること、例えば `range_lookback_bars=15000` なら `bar_window >= 15000`、空のinstrumentsでも宣言計算が可能なことを確かめる
16. **時間切れ決済が配線されている**: `held(PositionDirection.LONG).model_copy(update={"strategy_id": strategy.strategy_id, "as_of": now - timedelta(seconds=3600)})` を使う。`held` の引数は direction だけで、既定 strategy_id は `test_strategy`、as_of は2026-08-13なので両方を上書きする。3599秒では発火せず、3600秒で `exit_only`、`reason_codes == [HORIZON_EXPIRED]`、TPはNone。`_evaluate` を呼ばないこと、非marketイベントでは発火しないこと、同じ保有では二重発火しないことも確かめる。`test_horizon_exit.py::test_on_event_skips_evaluation_only_when_horizon_fires` の形を新規テストファイル内で踏襲し、既存ファイルの編集は不要

17. **入力不足と将来の足**: spec / tick が None、各 ATR が None / 0、entry足が6本以下なら新規signalを出さない。`known_at > now` の将来足が混ざっても、clock付き `InMemoryMarketData` または上の可視性フィルタにより判定材料に入らない。未来のレンジ外1h足で失効しないことも確かめる
18. **厳密な breach と方向の分岐**: low が下端と等しいだけ／high が上端と等しいだけでは breach ではない。戻り終値がレンジ端ちょうどなら内側、逆側の端も超えていれば不成立。買い候補が不成立でも通常の売りケースを評価できることを確認する。6本以内の途中足が最安値／最高値になる形で、stop が breach 足だけでなく成立までの全足の極値を使うことも確かめる。breach 足がレンジ構築前（`known_at <= built_at`）に確定していれば不成立で、構築後の breach なら成立する（解釈 5）
19. **丸め後の距離**: 正の risk が0.1 pipへの丸めで0になる場合は新規を出さない。TP有効かつ丸め後のrewardが0の場合も、モデル検証エラーではなく不成立とする。riskが0以下のケースも買い・売りで確かめる
20. **実際の market / IndicatorService の接続**: clock付き `InMemoryMarketData` に30本以上のH1/M5とtickを入れ、実 `IndicatorService` で成立条件を満たす例を1本作る。H1は通常 high=149.52 / low=149.48 / open=close=149.50とし、末尾24本に含まれる足で high=150.00とlow=149.00をそれぞれ1回記録する。M5は通常 high=149.22 / low=149.18 / open=close=149.20、末尾2本を基本ケースのbreachと戻りにする。実ATRとレンジ幅・RRの成立を先に確認し、注入ATRの期待値17.0 pipsを流用しない。特徴量を読まないことは、呼ばれると失敗する `features.get` のstubでも確認できる

境界用データ（すべてテスト専用の相場データ。§2 の数値は変更しない）:

- RR: high=151.25 / low=149.25 / mid=150.25、extreme=149.125、ATR(entry)=0.5、ask=149.50。stop=149.00、risk=0.50、reward=0.75なのでRR=1.5ちょうどで許可される。帯上限149.65にも収まる。ask=149.501なら reward=0.749 < 1.5 × 0.501=0.7515で拒否する。spreadは0.01に固定し、各OHLCの安値・終値をbreachと戻りに整合させる
- EMA傾き: 26本の終値を先頭25本149.00、最後154.25とすると、EMA(20)の初値149.00、末尾149.50、傾き0.50。注入ATR(regime)=1.0なら上限0.50ちょうど。high=155.00 / low=148.00にして幅条件も通す。最後の終値を154.251へ上げれば傾きが閾値を超える
- レンジ幅: high=149.75 / low=149.00 / open=close=149.375、注入ATR(regime)=0.5なら幅0.75が下限1.5 × 0.5と一致する。high=149.749なら不足する。どちらもEMAは横ばいにする

時間切れは、通常setupが成立しない材料や閉鎖中のprofileでも3600秒で出ること、終了前 60 分の新規停止中でも出ること、`horizon_exit_enabled=False` なら出ないことも新規ファイル内で確かめる。終了前 60 分の停止中は、保有と逆向きの setup が成立しても signal を出さない（新規も反転も止める。決済は SL/TP と時間切れに任せる。解釈 2）ことを 1 本確かめる。

`tests/unit/test_invariants.py` は触らない。

### 4-3. `tests/unit/test_config.py` に追加

`test_range_edge_reversal_loads_preregistered_parameters`: `load_config("backtest", CONFIG_DIR)` で `strategies["range_edge_reversal"]` を取り、`enabled is False`、`status is StrategyStatus.RESEARCH_ONLY`、`instruments == ["USDJPY"]`、regime / entry が1h / 5mであることを確認する。`params = strategy.params_for("USDJPY")` の各値は `params.param(key, None)` で読み、§3-4 のdefaults全項目と照合する（range_stale_hours、EMA / ATR期間、傾き・幅・stop係数、spread_gateも含む）。数値・boolの型も保ち、`absolute_max_spread_pips == "1.5"`、`session_profile_for("USDJPY") == config.session_profiles["usdjpy_core"]` を確認する。

別途、backtest / demo / shadow / micro_live / production の全overlayで、この戦略は `enabled is False`、`status is RESEARCH_ONLY`、`runs is False` のままであることをparametrizeで確認する。research runnerの直接指定では評価可能だが、liveの通常配線では走らないという違いを固定する。

---

## 5. やらないこと

- 候補 2（初回押し目）・候補 3（通貨強弱）の実装
- VPS での測定、研究ノートへの結果・判定の記載
- `config/shadow.yaml` / `micro_live.yaml` / `production.yaml` / `backtest.yaml` の変更（**live に載せない**）
- 既存戦略（`post_event_failed_breakout` / `failed_spike_reversal` / `monetary_policy_convergence`）の変更
- `StrategyContext` へのフィールド追加、`IndicatorService` へのメソッド追加、OMS / Risk / Portfolio / storage / migrations の変更
- ADR の追加（既存の ADR-036 / ADR-038 の枠内。新しい規範的決定を作らない）。設計上どうしても不変条件に触れるなら、実装を止めて報告する
- `tests/unit/test_invariants.py` の変更
- 研究ノート `docs/research/2026-09-17-h6-range-edge-reversal-preregistration.md` の作成・編集（Claude が書く）
- 周辺リファクタ・無関係な整形

---

## 6. 実装上の規約

- Strategy から Broker・OMS・DB へ到達しない。`ctx` の read-only サービスだけを使う
- Strategy 内で `datetime.now()` を直接呼ばない（`ctx.clock.now()` のみ）
- LONG/SHORT（Position）と BUY/SELL（Order）を混同しない
- 通貨ペア・pip size・時間足をハードコードしない（`InstrumentSpec` / `config.timeframes` / パラメータ経由）
- 金額・数量は扱わない。指標計算は float、価格・価格差は Decimal、pips は `Decimal(str(round(x / pip, 1)))`
- frozen モデルを壊さない（`NamedTuple` は `_replace` で新しい値を作る）
- 検証はシステム境界のみ。内部関数間に防御的分岐・フォールバックを足さない
- WHAT を説明するコメントを書かない。「issue #157 のため」のようなコミット文脈のコメントを書かない
- テストデータに実在の人物・団体名を使わない

## 7. 完了条件

worktree 内で次がすべて通ること。現在のworktreeには `.venv/bin/ruff` / `.venv/bin/pytest` が存在する。

```
.venv/bin/ruff check .
.venv/bin/pytest tests/unit tests/replay tests/failure
```

- `ruff check .` 無指摘
- 上記3ディレクトリのpytestが成功する。このコマンドは `tests/broker` を収集しないため、「brokerがskipされた」とは報告しない。`tests/integration` もPostgreSQL必須のため対象外
- `tests/unit/test_invariants.py` が変更なしで通る
- §4の新規テストに加え、既存の `test_horizon_exit.py`、`test_session_entry_gate.py`、`test_research_runner.py`、`test_live_wiring.py` の回帰が通る。実データの研究測定・採否判断は完了条件に含めない

## 8. 影響範囲の確認

```
rg -n "post_event_failed_breakout" src config tests docs   # 登録経路と同じ場所に足したか
rg -n "range_edge_reversal" src config tests docs
rg -n "session_end|session_start|sessions_at" src tests
rg -n "latest_tick|bar_window|market_span_to_calendar|STRATEGIES" src tests
rg -n "horizon_exit_enabled|take_profit_enabled|absolute_max_spread_pips|session_profile" src config tests migrations
```

`research.py` のchoicesと直接評価経路、live側の `StrategyConfig.runs` による有効化判定を区別して確認する。configの任意キーは `ResolvedStrategyParameters.param` 経由で読み、共通schemaやmigrationsを追加する必要はない。新しいキーを追加した後もコード・設定・テストを横断して参照先を確認する。

## 9. コミットしないもの

- `tmp/`（Codex のログとセッション ID。`.gitignore` に無いので `git add` で明示的に外す）
- `tasks/PARENT-NOTES.md` / `tasks/APPROVAL.md`（作らない）

将来の実装PRではこのファイル（`tasks/issue-157-range-edge-reversal.md`）をコミットに含める。今回のレビューではこのファイルだけを編集し、ステージ・コミットは行わない。

## 確定した実装上の解釈（Claude 判断。§2 の数値・規則は変えていない）

計画レビュー（Codex）が挙げた 6 点への判断。いずれも承認済みの数値・規則を変えず、文言の解釈を固定したもの。研究ノートにも同じ解釈を書く。Codex はこの節を前提に実装し、ここに無い新しい解釈が必要になったら実装を止めて報告する。

1. **レンジを確定する時点**: 「開始時点」は、そのセッションが開いてから strategy が最初に評価した市場イベントの時点とし、その時点に見える（`known_at <= now`）直近 24 本の 1h 足と、その時点の `ctx.indicators.atr(symbol, regime_tf, atr_period)` を使う。研究リプレイは tick ごとに評価するので開始から数秒以内であり、過去時点のスナップショットを取る仕組みは足さない。失効判定は `known_at > built_at` の 1h 足で行う。事前登録の期間開始（2024-08-01T00:00 broker = 2024-07-31 21:00 UTC）はどのセッションも開いていないので、期間開始が途中セッションに落ちる境界の影響は無い。
2. **セッション閉鎖時の exit-only**: §2-5 の (3) は base の既存挙動（gate 閉鎖中に反対向き setup が成立したときだけ exit-only に変換）を指し、閉鎖を契機に決済する機能ではない。この戦略はセッション外にレンジを持たないので (3) には実質到達せず、閉鎖そのものによる決済は追加しない。決済は (1) SL/TP と (2) 時間切れが担い、終了前 60 分の新規停止（保有と逆向きの setup も含めて signal を出さない）と 60 分の保有期限の組で NY 終了前に閉じる設計とする。時間切れは gate と停止条件より前に判定するので止まらない。
3. **「同じ方向は 1 回」の単位と注文拒否**: 1 回 = signal 生成 1 回（`_new_setup` の slot 消費）。Risk・執行で拒否されても再試行しない（既存戦略と同じ）。研究リプレイは scenario `normal` で確率的な執行拒否は 0 だが、Risk の拒否（建玉上限など）は起こり得る。研究ノートでは「執行の確率的拒否は無く、Risk 拒否は再試行しない」と書く。
4. **保有 60 分とロールオーバーの保証範囲**: 時間切れは保有 3600 秒以降の最初の市場イベントで決済 signal を出すもので、約定は latency（normal で 150ms）と次 tick の後になる。終了ちょうど 3600 秒前の entry は NY 終了までの約定完了を保証しない。数値と `<` は変えず、事前登録の保留条件（`unpriced_rollovers` > 0 なら判定保留）がこれを捕捉する。
5. **6 本以内と 30 分**: 規則は「観測した確定足 6 本以内」。`BarBuilder` は空足を補完しないので、欠測を挟むと暦では 30 分を超え得る（30 分は名目）。breach 足はレンジ構築後（`known_at > built_at`）に確定した足に限る。
6. **固定中央への利確の精度**: 利確は signal 時点の ask/bid から中央までの距離を 0.1 pip に丸めて `take_profit_distance_pips` に載せ（ADR-038 の経路）、Portfolio が処理時の entry_price から価格へ戻す。丸めは USDJPY の 1 point 以内。pending 中に保留された signal は処理時 quote の変動分だけ中央からずれ得る。研究上はこの差を許容し、絶対価格の固定は要求しない（Portfolio / OMS は変更しない）。

### 参考: 計画レビューで挙がった論点（上の判断で解消済み）

1. **レンジを確定する時点**。§2-1はセッション開始時点、§3-2(a)は開始後に最初に評価した時点の足・ATRを使う。`MarketDataService.bars` と `IndicatorService.atr` が返すのは現在のclockまでのデータであり、`known_at <= session_start` の過去スナップショットではない。researchのwarmup中はstrategyが呼ばれないため、開始途中からの測定でも差が出る。§3の `built_at` より後だけを見る失効判定も、開始から初回評価までのレンジ外確定足を見逃す。承認済みの開始時点を守るため、過去足の絞り込み・必要読取本数・開始時点のATR取得をどう設計するかの判断が必要。既存の `trading.indicators.atr.atr` を再利用する案はあるが、§5の範囲内での採用可否を決めてから§3を更新する。

2. **セッション閉鎖時のexit-only**。§2-5の「既存のbaseの仕組み」は、閉鎖を契機に自動決済する機能ではない。`_setup_signal` は閉鎖中に反対方向のsetupが成立した場合だけexit-onlyへ変換する。§3は開いているセッションがなければ先にNoneを返すため、その経路にも到達しない。閉鎖そのもので決済する規則をどう実装するか、また終了前の新規停止を決済候補にも適用してよいかが未確定。時間切れとは別の出口を無断で追加しない。

3. **注文拒否と「同じ方向1回」の単位**。§2-5の「研究リプレイに注文拒否は無い」は現コードでは保証されない。`BacktestEngine._process_intents` はRisk拒否を記録し、`ExecutionSimulator.submit` は執行拒否を返し得る。`normal` の確率的拒否率は0だが、stressでは正の値になる。`_new_setup` はsignal生成時に消費され、`_horizon_exit` も決済拒否後に再送しない。研究条件をどこまで限定するかと、1回をsignal生成として扱うか約定として扱うかの判断が必要。拒否後の再試行をこのレビューで追加しない。

4. **保有60分・ロールオーバー回避の保証範囲**。`_horizon_exit` は建玉を最初に観測したsnapshotの `as_of` から3600秒以降の市場イベントでsignalを出す。実際の決済にはその後のtickとlatencyが必要で、normalでも既定150msかかる。§2-5の停止条件は残り3600秒ちょうどを許すため、例えば冬時間21:00 UTCのentryはNY終了22:00 UTCまでの約定完了を保証しない。研究でも「期限で決済signalを出す」のか「終了までに決済完了する」のかを区別する判断が必要。バッファの数値や `<` を無断で変更しない。

5. **6本以内と30分以内の関係**。§2-3の「6本 = 30分」に対し、`BarBuilder` はtickのない区間の空バーを補完しない。§3のrun判定は観測した本数だけなので、欠測を挟む6本が30分を超えることがある。また、直近7本をそのまま読むためセッション開始前のbreachを含み得る。観測した6本を規則とするか、経過30分・セッション内という制約も要求するかは未確定。追加の時間制約や境界フィルタを勝手に導入しない。

6. **固定中央と距離指定の精度・遅延**。§2-4は固定中央への利確だが、§3は距離を0.1 pip単位に丸め、`PortfolioManager._entry_intent` が処理時のentry_priceから価格へ戻す。中央が0.1 pipの格子に乗らなければ丸め誤差があり、`BacktestEngine._process_signal` がpending中のsignalを保留した場合は処理時のquoteの変動分だけ中央からずれる。承認済みのADR-038経路を使ったこの差を研究上許容するか、絶対価格の固定を厳密に要求するかの判断が必要。Portfolio / OMSなどの範囲外の変更は行わない。
