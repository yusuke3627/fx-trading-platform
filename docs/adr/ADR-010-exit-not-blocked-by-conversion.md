# ADR-010: リスクを減らす exit / reduce は conversion 失敗で止めない

**Status:** Accepted (2026-08-26)

## Decision

conversion quote の欠損・staleness は、**新規 entry / size increase だけ**を
止める理由になる。既存 position の close / reduce / protection 維持は、

- conversion が `MONITORING` で値を返せる限りその値（stale なら haircut 付き）
  で評価し、
- conversion が完全に失敗しても、ticket 整合性など既存の安全条件を満たす限り
  実行可能であり続ける。

ただし ticket 参照・fresh position select・HEDGING 意味論などの既存 exit
安全条件（SYSTEM_SPEC v1.3）は一切緩めない。

## Rationale

換算 rate が得られない状況は、市場接続の劣化を意味することが多い。その瞬間に
最も価値があるのはリスクの削減であり、entry 用の market-data freshness 障害を
理由に exit を禁止すると、劣化時にポジションを抱えたまま動けなくなる（設計書
v2.1 §3 不変条件 10、§11.2）。entry と exit の判断は非対称であり、fail-close
の対象は「リスクを増やす行為」に限定する。

## Consequences

- risk engine / OMS の exit 経路（#56 以降）は conversion 例外を entry 経路の
  reject に変換せず、監視値なしでも close を発行できる形を維持する
- exit / risk-reducing order は entry とは別の priority で扱う（OMS priority
  queue、#64）
- 「conversion 停止中に exit できたか」は failure テスト（設計書 §33.2）で
  回帰固定する
