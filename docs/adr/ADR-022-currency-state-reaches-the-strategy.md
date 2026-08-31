# ADR-022: 通貨 state を strategy へ届ける配線（M3 3/3）

**Status:** Accepted (2026-08-31)

## Context

ADR-018 / 020 / 021 で `CurrencyState` とその供給元は揃ったが、strategy に
届いていなかった。`StrategyContext` が持つのは feature store と global の
`RegimeService` だけで、通貨単位の方向感も通貨別 regime も見えない。

配線には制約が 1 つある。**strategy 層はリポジトリへ到達できない**
（`StrategyContext` に執行系を足さない、`tests/unit/test_invariants.py`）。
`CurrencyStateService` はリポジトリ backed の `FactorSeriesSource` を持つ
ので、そのまま context へ入れると strategy から DB へ到達できてしまう。

## Decision

### 1. 供給側は外、strategy には入れ替わる器だけ渡す

feature store と同じ形にする。リポジトリを触る `StoredFeatureSource` は
外に置き、strategy が参照で持つのは `CurrencyStateStore` ——
`replace()` で中身ごと入れ替わる read-only の器だけ。

入れ替えであって更新ではないので、供給が途切れた通貨は前回値のまま残らず
消える。古い方向感で売買を続けるより、欠測のほうがよい。

`StrategyContext` に足すのは 2 つ。

| フィールド | 中身 |
| --- | --- |
| `currency_states` | `CurrencyStateStore`（通貨 state + ペア射影） |
| `currency_regime` | `CurrencyRegimeService`（`snapshot(now)`） |

`currency_regime` はサービスのまま渡す。判定は feature store の現在値を
読むだけで、リポジトリを持たない。`snapshot` が `now` を取るのは
`CurrencyRegimeSnapshot` が `known_at` を持つためで、strategy は自分の
clock を渡す。

### 2. feature と通貨 state は同じ源から、同じ時刻で入れ替える

別々に refresh すると、feature は新しい会合を見ているのに通貨 state は
まだ見ていない、という不整合が replay に出る。`StoredFeatureSource` が
両方を作る。`frozen()` / `change_instants()` / `dataset_fingerprint()` も
1 つの行の集合から答えるという既存の性質がそのまま効く。

`frozen()` は **同じ store インスタンスを引き継ぐ**。strategy が参照で
持っているのはそれで、凍結側が別の器を作ると refresh が届かない。

### 3. 凍結する行の範囲を factor の読み出し幅まで広げる

これまで凍結していた観測は US2Y だけだった。通貨 state は
`DEFAULT_FACTOR_INPUTS` の全系列を読むので、`MacroFactorSeries.read_windows()`
が返す幅で系列ごとに読む。狭いまま凍結すると、replay だけ factor が欠測
する。

US2Y は feature（20 営業日の z）と RATES factor（正規化の窓 = 実測 105 日）の
両方が読むので、**広い方**を採る。この結果 `dataset_fingerprint` は行が
増えて変わるが、これは正しい：fingerprint は「strategy が見た入力」の
同一性を表すもので、入力が増えたなら変わるべき。

`ReplayFeatureTimeline` が各 instant に足す expiry は US2Y の窓幅で固定
されている。月次・四半期の factor 系列は窓が年単位なので、その expiry は
replay の長さでは到達しない — 足される instant は余分な refresh 1 回に
なるだけで、判定は変わらない。

実測（30 日の replay、Mac の PIT DB）: 凍結する行 738 観測 + 45 イベント、
change instant 783、refresh 1 回あたり 1.5 ms（replay 全体で約 1.2 秒）。

### 4. 観測が 1 つも無い通貨は store へ入れない

何も見えていない通貨は `directional_score = 0` / `confidence = 0` になる
が、それは「方向感が無い」ではなく「何も見えていない」。store へ入れると
`PairState` が射影できてしまうので落とす。`pair()` は両 leg が揃っている
ときだけ返す。

判定は **freshness ではなく観測の有無**で行う。公表間隔の長い factor は
次の公表を待つ間 freshness が 0 になるが（#89）、値そのものは依然として
最新の事実であり、方向感は語れる。

## 現時点の実測（2026-08-31、Mac の PIT DB）

```
USD: dir=0.227576 conf=0.217014  policy=0.75 growth=-0.0748 inflation=-0.0283 rates=0.2634
JPY: dir=0.000000 conf=0.000000  policy=0.00
GBP / EUR: 欠測（Mac 側に GBP/EUR の観測がまだ無い。#57）
USDJPY pair: dir=0.227576 conf=0.000000
```

**ペアの confidence が 0 なのは #89 の freshness 欠陥がそのまま出たもの。**
JPY の最後の会合が 14 日以上前で freshness = 0 になり、
`min(base, quote)` がそれを拾っている。方向感（`directional_score`）は
正しく出ているので、**この値を confidence で gate する消費者を作るのは
#89 の修正後**にする。

## スコープ外

- strategy が実際に通貨 state を読んで判断を変えること（M4 / M5）
- USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）
- confidence の freshness が公表間隔に追随しない件（#89）
