# ADR-025: freshness は系列の観測間隔に追随し、confidence は読み取り時刻で再計算する

**Status:** Accepted (2026-09-02)

## Context

ADR-018 §2 は `confidence = coverage × freshness` と定めたが、従来の
freshness は全 factor に固定の 48 時間 / 336 時間を適用していた。このため
月次・会合・四半期系列は、次の公表を待っているだけでも freshness が 0 に
なり、confidence が日次系列の有無へ偏っていた。

また、live は評価サイクルごとに confidence を再計算する一方、replay は行の
到着時と UTC 日付変更時にだけ通貨 state を更新していた。同じ入力と評価時刻
でも、同一日内では confidence と `known_at` が一致しなかった。

## Decision

### 1. freshness を系列の cadence で測る

正規化に使った可視観測について、相異なる `known_at` を昇順に並べた隣接差の
中央値を cadence とする。同一 `known_at` の重複行は間隔 0 を作らないよう
除外し、相異なる時刻が 2 個未満なら cadence は不明とする。

cadence が測れる場合は次の線形減衰を使う。

```text
full = max(cadence_hours, 48)
zero = full × 3

age <= full        : freshness = 1
full < age < zero  : freshness = (zero - age) / (zero - full)
zero <= age        : freshness = 0
```

48 時間の floor は日次系列を週末・祝日の空白で減点しないために置く。3 間隔で
0 にすることで、通常の次回公表までは最新の事実として扱い、公表の遅延が続く
と段階的に減点する。cadence が不明な場合だけ、従来の full 48 時間 / zero
336 時間へフォールバックする。

### 2. confidence を読み取り時刻で再計算する

`CurrencyState` は factor ごとの `fitted_through` と cadence を
`freshness_basis` として保存する。`CurrencyStateStore` は `retime(now)` で
評価時刻を受け取り、`get()` と `pair()` の読み取り時に同じ純関数で
confidence と `known_at` を再計算する。

live は毎評価サイクルの `StoredFeatureSource.refresh(now)`、replay は毎回の
`ReplayFeatureTimeline.advance(now)` から `retime(now)` を呼ぶ。replay の full
refresh 間では可視行が変わらず、保存済み basis と読み取り時刻が live の再計算
と同じになるため、同一入力・同一時刻の confidence は一致する。

## Consequences

- 月次・会合・四半期系列は、自身の通常の公表間隔内で freshness を失わない。
- 日次系列は 48 時間まで満点を保ち、144 時間で 0 になる。
- `directional_score` の計算は変更しない。
- lookback 窓のスライドによる期限切れは従来どおり日付粒度であり、本決定の
  対象外とする。US2Y だけは既存の expiry instant を引き続き使う。
