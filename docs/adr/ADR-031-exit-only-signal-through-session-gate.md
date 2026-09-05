# ADR-031: session gate 閉鎖中も決済専用 signal を通す

**Status:** Accepted (2026-09-06)

## Context

ADR-028 の session profile entry gate は、閉鎖中に Strategy の `_evaluate` 自体を
スキップする。Portfolio は保有と逆方向の signal を CLOSE と新規 OPEN に分解するため、
この gate は entry だけでなく反転時の CLOSE も遮断していた。その結果、閉鎖中に反転 setup が
成立しても既存ポジションは protective stop が発動するまで残る。現行 profile では主に
NY close から Tokyo open までの 2〜3 時間だが、Tokyo を DISABLED とする
`london_ny_major` のような profile を live strategy が参照すると露出時間が広がる。

## Decision

案 A として決済専用の signal 形を導入し、「entry は fail-close、exit は止めない」という
ADR-010 と同じ非対称を採る。

- `StrategySignal` に `exit_only: bool = False` を追加する。決済専用 signal の
  `desired_direction` は反転 setup の向き、すなわち保有の逆方向とする。既存 DB 列の
  LONG / SHORT 制約を維持でき、下流の決済価格も通常の反転 signal と同じ側になる。
- Portfolio は `exit_only` を受けたら、fresh な仮想ポジションがある場合だけ CLOSE を生成し、
  OPEN と INCREASE は生成しない。ポジションが無ければ何も生成しない。Portfolio は session を
  知らず、gate を Strategy 層に閉じる ADR-028 の境界を維持する。
- Strategy は gate 閉鎖中でも保有がある instrument だけ `_evaluate` へ進める。成立した setup は
  `_setup_signal` が、gate 開放中なら通常の entry signal、閉鎖中なら保有と逆向きの場合だけ
  決済専用 signal にする。同方向の setup は INCREASE になるため閉鎖中は捨てる。
- gate 閉鎖中は entry 用の `_new_setup` memo に触れない。決済専用 signal は entry と別の
  dedupe slot を使う。

これにより、ADR-028 の「gate は `_evaluate` の前で効き、閉鎖中は評価を丸ごと止める」という
決定を一部改訂する。policy と StrategyStatus の関係、重なる session の扱い、profile の
受け渡しに関する決定は維持する。

## Consequences

- gate 閉鎖中に保有を持つ strategy は、反転 setup が成立すると決済できる。反転後の再 entry は
  session が開いてから通常経路で行い、同じ setup_id でも entry memo を未消費なので signal に
  できる。
- 決済専用 signal は `(symbol, direction, exit_only=True)` の slot で dedupe する。
- `strategy_signals` に `exit_only` 列は追加しない。trail では reason code の
  `SESSION_CLOSED_EXIT_ONLY` と CLOSE のみの intent から判別する。列追加は別 issue とする。
- profile を参照する strategy は、保有中に限り閉鎖時間帯も `_evaluate` を実行するため、
  backtest と shadow の計算量がその時間帯に増える。保有の有無は閉鎖中の市場 event ごとに
  `PortfolioView.position` で読むので、`VirtualPositionLedger` は最新 snapshot を
  (strategy_id, symbol) の索引で持ち、履歴の走査をしない。
- shadow の仮想 book は fill が届かず通常空であり、決済専用 signal が出るのは保有を
  記録した場合に限る。
