# 介入アンカーの短期イベントスタディ CLI（`trading.backtest.intervention_event_study`）

ブランチ: `feat/intervention-event-study`（origin/main = e21590b 起点）

この計画ファイルは **単独で実装できる自己完結の仕様** である。会話履歴・issue は参照しない。
不明点があれば、この計画に書かれた決定を優先し、書かれていないことは「やらないこと」節に従って
足さない。

## 1. 背景（なぜ作るか）

`docs/research/2026-09-02-monetary-policy-convergence-verdict.md` の結論:
E′（`policy_event_study.py`、会合アンカー）・B′（`rate_differential_study.py`、金利差）を通じて
唯一符号がまとまったのは **介入後数日の非対称な円高**（5d で順行 −2.82% vs 逆行 +0.91%、
10d で消滅）。これは swing ではなく数日ホライズンの素材なので、介入そのものをアンカーにした
短期イベントスタディ（15 分〜10 営業日）で、非対称性の再現性と減衰を測る。

PR #102 で Dukascopy 歴史 tick が 2022-01〜2024-07 と 2026-01-23〜04-08 の穴に投入される
前提（VPS 側で実行）。本 PR は CLI とテストのみ。実データの実行はマージ後に VPS で行う。

## 2. 要件（全文）

- 新規 CLI: `python -m trading.backtest.intervention_event_study --env backtest`。
  symbol は USDJPY 固定（`--symbol` フラグは持たない。E′ の `SYMBOL` 定数を import）
- 対象: events テーブルに保存された介入エピソード（`config/intervention_episodes.yaml` 由来、
  `event_type = "INTERVENTION_REPORTED"`、payload の `direction == "JPY_BUY"`）。
  現在 11 件: 2022-09-22, 10-21, 10-24 / 2024-04-29, 05-01（NY 深夜 = JST 5/2 早朝）, 07-11,
  07-12 / 2026-04-30, 05-04, 05-06, 07-30。tick でカバーされないエピソードは
  「no quotes」と明示して除外する
- **アンカー 2 種類**:
  1. **ショックアンカー**（市場観測可能・PIT 安全）: 探索窓内で、5 分足の下落幅（円高方向、
     `log(close/open)` が最小）が最大の足の close。検出時刻と下落幅を出力に明示。以降の計算は
     その足の close 以降しか使わない
  2. **報道上界アンカー**（保守）: 事象の `known_at`（実 UTC）をラベル軸に変換し、
     その後の最初の 5 分足 close
- ホライズン: 15m / 1h / 4h / 1d / 2d / 3d / 5d / 10d。1d 未満は 5 分足で数え、1d 以上は
  営業日ステップ（E′ と同じ日足ベース）
- 各ホライズンで log リターン・最大順行・最大逆行。加えて **減衰プロファイル**（各オフセット
  での平均累積リターンの経路。順行のピークと反転位置が読めるように）
- **エピソード個票を全件出力**（n≈11 なので個票が主）。クラスタ（5 営業日以内に連続する介入）
  は最初の日をクラスタアンカーとし、後続日は「overlap」と明示して非重複集計から除く。
  個票では全日出す
- 集計: 全日版とクラスタ非重複版で mean / median / hit / 最大順行・逆行 / bootstrap CI90。
  無条件ベースラインは E′ と同じ思想（測定範囲内の同ホライズン無条件分布）
- データ欠損: E′ の `gaps` による無効化（5 日以上の穴を跨ぐ窓は測らない）を流用
- tick は broker ラベル軸で保存されている（ADR-014 / ADR-005）。`known_at`（実 UTC）は
  ラベル軸へ変換する。E′/B′ と同じ方式・同じアンカー
  （`config.market.broker_server_ahead_of_ny_hours`）
- 出力: PowerShell から貼りやすいプレーンテキスト表（E′/B′ と同型）。先頭にショックアンカー
  検出結果一覧
- テスト: ショックアンカー検出（合成 5 分足、探索窓境界）、クラスタ判定、5 分足ステップと
  営業日ステップの窓計算、穴跨ぎ無効化、減衰プロファイル、ラベル軸変換、個票/集計の組み立て。
  ネットワーク・実 DB 不要
- E′（`policy_event_study.py`）・B′（`rate_differential_study.py`）は変更しない（import のみ）。
  migrations なし。Strategy/LLM 層に触れない

## 3. 参照する既存実装（file:line、origin/main e21590b 時点）

### E′ `src/trading/backtest/policy_event_study.py`（import して流用する部品）

| シンボル | 行 | 使い方 |
| --- | --- | --- |
| `SYMBOL` / `EPOCH` / `BROKER_CLOCK_MARGIN` / `BOOTSTRAP_SEED` / `HOLE_MINIMUM` | 62–79 | 定数。tick 読み出し範囲は E′ と同じ `stream_between(symbol, EPOCH, now + BROKER_CLOCK_MARGIN)` |
| `Stats` dataclass | 169–178 | 集計行の値（count/mean/median/hit_rate/adverse/favorable/low/high） |
| `irregular_steps(bars)` | 206–219 | 日足系列の不規則ステップ（報告用。E′ の report 冒頭と同じ欠損一覧を出す） |
| `gaps(bars)` | 222–238 | 5 日以上の穴。5 分足系列に掛けても「≥5 日の穴」だけを返すので intraday 窓にもそのまま使える |
| `window_outcome(bars, entry, horizon)` | 257–278 | log リターン・逆行（max high）・順行（min low）。`entry + horizon` が系列外／穴跨ぎで `None`。**5 分足系列にも日足系列にもそのまま使う**（horizon は「本数」） |
| `bootstrap_interval(values, seed)` | 302–316 | CI90 |
| `unconditional(bars, horizon, seed)` | 352–376 | 無条件ベースライン（非重複窓） |
| `_row(label, stats)` | 399–408 | 集計行フォーマッタ（B′ も import している private。E′ を触らないためこのまま使う） |
| `fold_daily` | 88–117 | **使わない**（1 timeframe しか畳めない）。進捗表示の書き方（月ごとに stderr へ 1 行）だけ真似る |
| `entry_bar` | 181–203 | **使わない**。「known_at 後の最初の close」規則の参考。本 CLI は known_at をラベル軸へ変換して比較する（§4.3） |
| `summarize` / `thin` / `measured_span` / `Observation` | 319–349 | **使わない**。`Observation.returns` が `dict[int, float]` で、5 分足 3 本と 3 営業日のキーが衝突するため、本 CLI は独自 dataclass と小さな `stats()` を持つ（§4.7） |
| `main` | 478–576 | DSN 取得・`connect` ・repository 生成・進捗出力の型 |

B′ `src/trading/backtest/rate_differential_study.py:37-54` が E′ から上記部品を import している実例。
`rate_differential_study.py:301` の `anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)` も同じ。

### ラベル軸変換

| シンボル | 場所 | 用途 |
| --- | --- | --- |
| `known_to_broker_label(known, server_ahead_of_ny)` | `src/trading/data/market/dukascopy.py:52-60` | 実 UTC → broker ラベル。`known_at` の変換に使う（全域で定義、例外なし） |
| `broker_label_to_known(label, server_ahead_of_ny)` | `src/trading/backtest/research.py:76-100` | broker ラベル → 実 UTC。ショック検出足の時刻を UTC / JST で表示するためだけに使う（DST 遷移時刻のラベルでは ValueError を投げるが、tick のある時刻には現れない） |
| `broker_server_ahead_of_ny_hours` | `src/trading/config.py:51`（既定 7.0） | `anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)` |

ラベル軸の意味（ADR-005 / ADR-014、`src/trading/data/market/bars.py:81-96` の docstring）:
tick.time は「サーバーの壁時計を UTC とラベル付けした値」。サーバーは NY 時刻 + 7h 固定なので、
**ラベルの 00:00 = NY 17:00 = FX の取引日境界**。したがって broker ラベルの暦日 = FX 取引日。

### 足の畳み込み

| シンボル | 場所 | 用途 |
| --- | --- | --- |
| `BarBuilder(symbol, timeframe)` / `on_tick(tick) -> Bar \| None` | `src/trading/data/market/bars.py:81-135` | 閉じた足だけ返す。空バケット（tick のない 5 分）は足を出さない |
| 複数 timeframe を 1 パスで畳む実例 | `src/trading/backtest/research.py:503`（`[BarBuilder(symbol, tf) for tf in timeframes]`） | 本 CLI の `fold_bars` の手本 |
| `TIMEFRAME_SECONDS` | `src/trading/domain/market.py:9`（"5m"=300, "1d"=86400） | |
| `Bar`（`start` / `close_time` プロパティ / `open` `high` `low` `close` は Decimal） | `src/trading/domain/market.py:49-71` | `close_time = start + timeframe` |
| `Tick`（`time` = broker ラベル、`bid`/`ask` Decimal） | `src/trading/domain/market.py:23-46` | |

### 介入エピソード

| シンボル | 場所 | 用途 |
| --- | --- | --- |
| `EVENT_TYPE_PREFIX = "INTERVENTION_"` / `event_from_recognition` | `src/trading/data/intervention/episodes.py:28, 69-92` | events の `event_type` は `f"{EVENT_TYPE_PREFIX}{kind}"`（REPORTED → `"INTERVENTION_REPORTED"`）。payload は `{"action_date": "YYYY-MM-DD"（ISO 文字列）, "direction": "JPY_BUY", "verified": bool, ["note": str]}`。`known_at` は EventEnvelope の属性 |
| `PostgresEventRepository.known_before(t, event_type=None, since=None)` | `src/trading/storage/postgres.py:1013-1031` | `known_before(now, "INTERVENTION_REPORTED")` で全件（known_at 昇順） |
| `PostgresMarketTickRepository.stream_between(symbol, start, end)` | `src/trading/storage/postgres.py:443` | keyset ページング。`event_time, id` 順で Tick を yield |
| `EventEnvelope` | `src/trading/domain/event.py:54-81` | `payload: dict`, `known_at: datetime` |
| `config/intervention_episodes.yaml` | | 11 件の実データ（テストには使わない。合成データで書く） |

### テストの手本

`tests/unit/test_policy_event_study.py:34-73`（`T0` / `ANCHOR = timedelta(hours=7)` / `bar(index, close, high, low)` ヘルパー）、
同 164–198（穴跨ぎ・週末の扱い）。`tests/unit/test_rate_differential_study.py` も同じ流儀。

## 4. 設計決定（Codex は判断せずこのとおり実装する）

### 4.1 畳み込み: tick 1 パスで 5m と 1d を同時に畳む

```python
def fold_bars(ticks: Iterator[Tick], symbol: str, timeframes: Sequence[str],
              progress: TextIO | None) -> dict[str, list[Bar]]
```

- `BarBuilder(symbol, tf)` を timeframe ごとに 1 つ持ち、各 tick を全 builder に通す
  （`research.py:503` と同型）。E′ `fold_daily` と同じく月が変わるたびに stderr へ
  `YYYY-MM  <tick 数> ticks  <5m 本数> / <1d 本数> candles` を 1 行出す
- 読み出し範囲は E′ と同じ `stream_between(SYMBOL, EPOCH, now + BROKER_CLOCK_MARGIN)`
- 見積もり（計画時点の実測 `Bar` 1 件 ≈ 1.6 KB）: 5 分足 2022〜2026 で約 35 万本 ≈ **560 MB**、
  日足 1,200 本は無視できる。VPS はメモリ 7 GB なので全本保持する。tick 約 1.5 億件の
  1 パスは **30〜60 分**（Tick デコードが支配的。E′ の 2 年分で数分）。この見積もりは
  モジュール docstring に書く

### 4.2 ショックアンカーの探索窓は「broker ラベルの action_date 暦日の 00:00 から 36 時間」

- `day_start = datetime.combine(action_date, time(0), tzinfo=UTC)`（**ラベル軸の値。変換しない**）、
  探索窓は `day_start <= bar.start < window_end`、
  `window_end = min(day_start + 36h, 次のエピソードの day_start)`（次が無ければ `day_start + 36h`）。
  連日の介入（2024-07-11 → 07-12）で前日の窓が翌日の東京〜ロンドン時間（ラベル 00:00〜12:00）
  に食い込み、翌日の介入の下落を前日のショックとして拾うのを防ぐ。エピソードは action_date
  昇順に処理する
- 根拠: MOF の介入日は FX の取引日（NY 17:00 境界）で数えられており、broker ラベルの暦日と一致する。
  2022-10-24 の介入は JST 08:40 = 実 UTC 10/23 23:40 で、UTC 暦日では 10/23 だがラベルでは
  10/24 02:40。2024-05-01 の介入は JST 5/2 05:00 = 実 UTC 5/1 20:00 = ラベル 5/1 23:00。
  ラベル暦日を窓の起点にすると両方を同じ規則で拾える。36 時間は「翌取引日の前半」まで含める
  ための余裕（金曜のエピソードでは土曜に足がないので実質その日だけ）
- 窓内の 5 分足のうち `log(float(close) / float(open))` が最小の足（同値なら最も早い足）を
  ショック足とする。窓内に足が 1 本もなければそのエピソードは **「no quotes」** として
  アンカー一覧に載せ、以降の計測から外す
- 5 分足の `start` は昇順なので、`bisect` で窓の範囲を切り出す（`starts = [b.start for b in bars]`
  を 1 回作る）
- 出力するもの: 足の `start`（ラベル）、`broker_label_to_known(start, anchor)` の UTC、
  その JST（`ZoneInfo("Asia/Tokyo")`）、open→close、下落幅（円 = `close - open` を Decimal のまま、
  および `%` = log リターン × 100）、窓内の足の本数（カバレッジの目安。1 日フルで 288 本）
- **PIT 上の注意（docstring に書く）**: ショック足の選択は窓全体を見た事後選択だが、
  以降の計測はその足の close 以降しか使わない。「ショックが起きた条件で、その後に何が
  起きるか」を測るスタディであり、ショック発生を予測する主張ではない

### 4.3 報道上界アンカー

- `label = known_to_broker_label(known_at, anchor)`。entry は **`bar.close_time > label` を満たす
  最初の 5 分足**（E′ `entry_bar` の「厳密に後で閉じた足」規則をラベル軸で適用）
- そのような足が無ければ「no bar after known_at」として計測から外す

### 4.4 日足の entry（1d 以上のホライズン）

- 5 分足 entry を `e5` として、**`bars_1d[i].close_time >= bars_5m[e5].close_time` を満たす最初の
  日足** を `e1`（entry 日足 = ショック足／報道 entry 足を含む取引日）とする。5 分足が取引日
  最後の 1 本のときは日足 close と同一時刻・同一価格なので `>=` でその日を採る
- 日足ホライズンの窓は E′ と同じく **entry 日足の close → h 営業日後の日足 close**
  （`window_outcome(bars_1d, e1, h)`）。E′ の「known_at 後の最初の close」規則と同じ意味になる
  （報道上界アンカーでは `known_at < 5m close ≤ 日足 close`）
- 該当する日足が無い（系列が届かない）場合、日足ホライズンは全て未計測（個票は `-`）

### 4.5 ホライズンの定義と窓計算

```python
HORIZONS: tuple[Horizon, ...] = (
    Horizon("15m", "5m", 3), Horizon("1h", "5m", 12), Horizon("4h", "5m", 48),
    Horizon("1d", "1d", 1), Horizon("2d", "1d", 2), Horizon("3d", "1d", 3),
    Horizon("5d", "1d", 5), Horizon("10d", "1d", 10),
)
```

- `Horizon` は `NamedTuple(label: str, timeframe: str, bars: int)`
- 各ホライズンの値は `window_outcome(series[timeframe], entry[timeframe], bars)`。
  返り値 `(ret, adverse, favorable)` をそのまま使う（負 = 円高 = 介入方向 = 順行）。
  `None`（系列末尾を越える／5 日以上の穴を跨ぐ）はそのホライズンを未計測にする
- **5 分足のカウントは index 方式**（E′ の日足と同じ）。週末・日次メンテナンス・tick の無い
  5 分バケットは足が無いので自動的に飛ばされ、「取引された 5 分足 N 本後」を測る。
  週末を跨ぐ intraday 窓を無効化する規則は **足さない**（現エピソードでは金曜 close の 4 時間
  以内に入るアンカーが無く、E′ の「休場日は営業日に数えない」思想と同じ）。docstring に明記
- bootstrap の seed は `BOOTSTRAP_SEED + index`（`HORIZONS` 内の位置。E′ の `+ horizon` だと
  5 分足 3 本と 3 営業日が同じ seed になるため）

### 4.6 クラスタ判定

```python
def business_days_between(earlier: date, later: date) -> int   # (earlier, later] の平日数（祝日は無視）
def cluster_anchors(dates: Sequence[date]) -> dict[date, date]  # action_date -> クラスタ最初の日
```

- action_date 昇順で走査し、直前のエピソードから **5 営業日以内**なら同じクラスタに連結する
  （連鎖。2026-04-30 → 05-04 → 05-06 は 1 クラスタ）。それ以外は新クラスタ
- 現データでの帰結: 非重複 = 2022-09-22, 2022-10-21, 2024-04-29, 2024-07-11, 2026-04-30,
  2026-07-30（n=6）。overlap = 10-24, 05-01, 07-12, 05-04, 05-06
- 個票には全件、「cluster」列に `anchor` か `overlap <クラスタ最初の日>` を出す。
  非重複集計はクラスタアンカーだけを使う（E′ の `thin` は使わない。10d でもアンカー同士の
  窓は重ならないことを docstring に記す）

### 4.7 データ構造と集計

```python
@dataclass(frozen=True)
class Episode:
    action_date: date
    known_at: datetime
    cluster: date          # クラスタ最初の日。== action_date なら非重複

@dataclass(frozen=True)
class Anchor:
    kind: str              # SHOCK = "shock" / NEWS = "news"
    episode: Episode
    entry: int             # 5 分足 index
    drop: float | None     # shock のみ log(close/open)
    window_bars: int | None  # shock のみ 探索窓内の足数

@dataclass(frozen=True)
class Outcome:
    anchor: Anchor
    daily_entry: int | None
    returns: dict[str, float]     # horizon label -> log return
    adverse: dict[str, float]
    favorable: dict[str, float]
    profile_intraday: dict[int, float]  # 5 分足オフセット(本数) -> アンカー close からの累積 log return
    profile_daily: dict[int, float]     # 日足オフセット(営業日, 0 = entry 日足 close) -> 同上
```

- オフセットのラベル文字列（`+15m` / `entry-day close` / `+3d`）は report 側で組む。
  dataclass には整数オフセットのまま持つ

- `stats(outcomes, label, seed) -> Stats`: E′ `summarize` と同じ計算（count / mean / median /
  hit = `ret < 0` の割合 / adverse・favorable の平均 / `bootstrap_interval` の CI90）を
  `Outcome` に対して行う。E′ `Stats` を返し `_row` で整形する
- 無条件ベースライン: E′ `unconditional(span, horizon.bars, seed)`。`span` は
  **クラスタアンカーの entry の最小〜最大 + horizon.bars + 1** の切り出し（E′ `measured_span` と
  同じ意味。5 分足ホライズンは 5 分足系列、日足ホライズンは日足系列で切る）。
  15m の無条件窓は約 12 万本 × bootstrap 2,000 回で 1 分程度かかる。許容する
- 集計行は各アンカー種別 × ホライズンで 3 行: `all episodes` / `cluster anchors` / `unconditional`

### 4.8 減衰プロファイル

```python
INTRADAY_OFFSETS = tuple(range(3, 49, 3))   # 15m 刻みで 4h まで（5 分足 3, 6, ..., 48 本）
DAILY_OFFSETS = tuple(range(0, 11))         # 0 = entry 日足の close, 1..10 営業日

def path(bars: Sequence[Bar], entry: int, offsets: Sequence[int], base: float) -> dict[int, float]
```

- `path` は各 `k` について `log(float(bars[entry + k].close) / base)`。`entry + k` が系列外、
  または `gaps(bars[entry : entry + k + 1])` が非空なら、その `k` は省く
- **すべてアンカー 5 分足の close を基準（base）にする**: intraday は
  `path(bars_5m, e5, INTRADAY_OFFSETS, anchor_close)`、daily は
  `path(bars_1d, e1, DAILY_OFFSETS, anchor_close)`。`k = 0` の行（entry 日足 close）が
  4h → 1d の橋渡しになり、経路がアンカーから連続して読める。
  §4.5 の日足ホライズン表（日足 close 起点の E′ 方式）とは基準が違うので、出力の見出しに
  「cumulative from the anchor close」と明記する
- プロファイル表は各オフセットで `n / mean / median` を「all episodes」「cluster anchors」の
  2 組並べる。オフセットのラベルは `+15m … +4h`、`entry-day close`、`+1d … +10d`

### 4.9 出力レイアウト（プレーンテキスト、E′/B′ と同型）

上から順に:

1. 見出し: エピソード数（うち quotes あり）、5 分足本数、日足本数、符号の読み方、
   intraday/daily の数え方、クラスタ規則
2. 日足系列の欠損一覧（E′ `report` 428–440 と同じく `irregular_steps` / `gaps` で
   「missing data」「market closed」を列挙）
3. **shock anchors 一覧**（先頭に置く要件）: `action_date | cluster | bars | bar start (label) |
   UTC | JST | open->close | drop % (yen)`。no quotes のエピソードも行として出す
4. **news anchors 一覧**: `action_date | cluster | known_at (UTC) | label | entry bar start | entry close`
5. アンカー種別ごとに:
   - 個票: 1 エピソード 3 行（`ret` / `fav` / `adv`）× 8 ホライズン列（未計測は `-`）
   - 集計: ホライズンごとに見出し行（`horizon 15m (3 x 5m)` / `horizon 5d (trading days)`）
     + `_row` 3 行（all episodes / cluster anchors / unconditional）
   - 減衰プロファイル
6. 表は固定幅の f-string で組む（E′ `_row` と同じ流儀）。Markdown 表・色・タブは使わない
7. `report(outcomes_by_kind, series, episodes, anchor)` のように **`anchor: timedelta` を受け取る**
   （ショック足の UTC / JST 表示に `broker_label_to_known` を使うため）。`ZoneInfo("Asia/Tokyo")`
   は Windows でも `tzdata` が依存に入っている（`pyproject.toml:19`）ので使ってよい

### 4.10 main

E′ `main`（478–576）と同型:

1. `argparse`: `--env`（既定 `backtest`）のみ
2. `load_config(env)` → `os.environ.get(config.storage.dsn_env)` 未設定なら `SystemExit`
3. `from trading.storage.postgres import PostgresEventRepository, PostgresMarketTickRepository, connect`
   は関数内 import（db extra の無い環境でも import できるように。E′ と同じ）。
   CI の unit ジョブは `pip install -e '.[dev]'` だけで `pytest -q` を回す（`.github/workflows/ci.yml:23`）
   ので、モジュール先頭とテストファイルで psycopg に触れる import を書かない
4. `now = datetime.now(UTC)`（CLI なので可。Strategy ではない）
5. `series = fold_bars(stream_between(SYMBOL, EPOCH, now + BROKER_CLOCK_MARGIN), SYMBOL, ("5m", "1d"), sys.stderr)`。
   5 分足が空なら `SystemExit("no stored quotes for USDJPY")`
6. `episodes = load_episodes_from_events(repo.known_before(now, EVENT_TYPE))`
   （`EVENT_TYPE = f"{EVENT_TYPE_PREFIX}REPORTED"`、`direction == "JPY_BUY"` のみ。
   0 件なら `SystemExit("no INTERVENTION_REPORTED events — run trading.data.intervention.collector first")`）
7. `anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)`
8. `print(report(...))`

## 5. 変更対象ファイル（網羅）

| ファイル | 変更 |
| --- | --- |
| `src/trading/backtest/intervention_event_study.py` | **新規**。上記すべて。1 ファイル、**800 行以内**（目安 500〜650 行。docstring は日本語、B′ と同じ流儀） |
| `tests/unit/test_intervention_event_study.py` | **新規**。§6 のテスト |

**マイグレーション: なし。config 変更: なし。既存ファイルの変更: なし。**

## 6. テスト方針（`tests/unit/test_intervention_event_study.py`、合成データのみ）

ヘルパー: `T0 = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)`（月曜、ラベル軸）、`ANCHOR = timedelta(hours=7)`、
`m5(index, close, open=None, high=None, low=None)`（`start = T0 + 5min × index`、timeframe "5m"）、
`d1(index, close, high=None, low=None)`（`start = T0 + 1day × index`、timeframe "1d"）。
価格は Decimal 文字列。E′ テストの `bar()` ヘルパー（`tests/unit/test_policy_event_study.py:38-49`）と同型。

1. **fold_bars**: 数日分の合成 tick 1 本の iterator から "5m" と "1d" の両系列が同時に出る
   （本数と `start` を検証。最後の未完バケットは出ない）
2. **ショックアンカー検出**:
   - 窓内で `log(close/open)` 最小の足が選ばれる（大きな下ヒゲだけの足は選ばれない = close/open 基準）
   - 窓の境界: `day_start` より前の足（前日 23:55）の大きな下落と、`day_start + 36h` 以降の足の
     大きな下落は無視される
   - 連日エピソード: 翌日の `day_start` 以降にもっと大きな下落があっても、前日の窓はそこで
     打ち切られ、前日の窓内の足が選ばれる
   - 同値なら早い足
   - 窓内に足が無ければ `None`（→ report で「no quotes」）
   - `window_bars` が窓内の本数になる
3. **報道上界アンカー**: `known_at = 2026-05-04T14:59Z`（夏時間、anchor 7h → ラベル 17:59）で
   `close_time > 17:59` を満たす最初の足（ラベル 17:55 start の足、close 18:00）が entry になる。
   冬時間の例（2026-01 の known_at → ラベル +2h）も 1 本
4. **日足 entry**: 5 分足 entry を含む取引日の日足が選ばれる。5 分足が取引日最後の 1 本
   （close_time == 日足 close_time）でもその日になる。日足が届かなければ `None`
5. **クラスタ**: `[2022-09-22, 2022-10-21, 2022-10-24]` → 10-24 は 10-21 の overlap、09-22 は単独。
   `[2026-04-30, 05-04, 05-06]` → 1 クラスタ（連鎖）。`business_days_between` が週末を数えない
   （金→月 = 1、木→翌木 = 5、木→翌金 = 6 で別クラスタ）
6. **窓計算**: 15m は 5 分足 3 本後の close（`window_outcome` 経由で ret/adverse/favorable が
   期待値どおり）、1d は日足 1 本後。系列末尾を越えるホライズンは `returns` に無い
7. **穴跨ぎ**: 日足に 5 日以上の穴を置いた 5d 窓は未計測、週末跨ぎは計測される（E′ 規則が
   本 CLI の組み立てを通して効くことを確認）
8. **減衰プロファイル**: `path` が各オフセットでアンカー close 基準の累積 log return を返す。
   `k = 0`（entry 日足 close）が含まれる。系列外のオフセットは省かれる
9. **集計**: `stats` の n / hit / mean、`cluster anchors` 行が overlap を除く、
   `unconditional` の span がアンカー entry の範囲になる
10. **report**: 合成の 2〜3 エピソード（うち 1 件 no quotes、1 件 overlap）で文字列に
    `no quotes` / `overlap` / 各見出しが含まれる。厳密な桁合わせは検証しない

実在の人物・団体名は使わない（価格・日付だけの合成データ）。

## 7. 完了条件（実行可能コマンド。worktree の `.venv` を使う）

```bash
cd /Users/yusuke/Products/fx-trading-platform/.claude/worktrees/feat+intervention-event-study
.venv/bin/ruff check .                                               # 無指摘
.venv/bin/pytest tests/unit/test_intervention_event_study.py -q      # 全 green
.venv/bin/pytest tests/unit -q                                       # 既存を含め全 green
.venv/bin/python -m trading.backtest.intervention_event_study --help # 起動できる（DB 不要）
wc -l src/trading/backtest/intervention_event_study.py               # 800 未満
git status --porcelain                                               # 新規 2 ファイル以外に差分がない
```

`tmp/` 配下は無視してよい（計画・ログ置き場。コミットしない）。

## 8. やらないこと

- `policy_event_study.py` / `rate_differential_study.py` / `dukascopy.py` / `episodes.py` /
  `research.py` / `bars.py` / `config.py` / `config/*.yaml` / `migrations/` を変更しない
  （共有部品の別モジュールへの抽出も **しない**。E′ に差分が出るため）
- Strategy / LLM / OMS / Broker 層に触れない。`StrategyContext` に何も足さない
- `--symbol` / `--horizons` / 探索窓長などの CLI フラグを足さない（すべてモジュール定数）
- `JPY_SELL` エピソードの符号反転を実装しない（`JPY_BUY` 以外は読まない。docstring に明記）
- 週末跨ぎ intraday 窓の無効化、祝日カレンダー、5 分足のメモリ節約（軽量 dataclass 化）を
  実装しない
- 研究ノート（`docs/research/`）・ADR・README の追加をしない（実データの結果はマージ後に
  VPS で実行してから別途記録する）
- コミットしない（Claude 側が行う）

## 9. 規約（`.claude/rules/` から転記。AGENTS.md と併せて守る）

- **Decimal**: 価格は `Bar` の Decimal のまま持ち回り、`float()` にするのは統計計算
  （`log` / mean / bootstrap）の入口だけ（E′ `window_outcome` と同じ）。円建ての下落幅表示は
  Decimal の減算で出す
- **look-ahead 禁止**: アンカー足の close より前の価格を計測に使わない。`known_at` と足の比較は
  必ず同じ軸（ラベル軸）で行う。`known_at` を変換せずに `bar.start` と比べない
- **Clock**: `datetime.now()` は `main` の `now` だけ。計測関数は引数の値のみで決まる純粋関数にする
- **ファイルサイズ**: 1 ファイル 200〜400 行目安、上限 800 行
- **テスト**: 実在の人物・団体名を使わない。pytest。`tests/unit` に置く。ネットワーク・DB 不要
- **コメント**: WHAT ではなく WHY を書く。「〜のために追加」のようなコミット文脈依存の
  コメントを書かない。レビュー指摘の引用をコードに残さない
- **不変性**: dataclass は `frozen=True`。引数の list を書き換えない
- **境界検証だけ**: 内部関数間の防御的分岐（`if bars is None` 等）を足さない。境界は
  DSN 未設定・events 0 件・tick 0 件の 3 箇所（`SystemExit`）だけ
- ruff: `line-length = 100`、`target-version = py311`（`pyproject.toml`）
- 日本語でやり取り・コメント（既存の英語 docstring は維持してよい）
