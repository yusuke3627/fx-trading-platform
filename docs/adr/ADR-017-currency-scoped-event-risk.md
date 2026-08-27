# ADR-017: currency-scoped event risk と伝播 policy（M3 1/3）

**Status:** Accepted (2026-08-27)

## Decision

scheduled event risk を通貨 scope 付きにする（設計書 v2.1 §14 / §14.1A /
34.4）。

- `EventRiskWindow` に `affected_currencies: frozenset[Currency]` と
  `propagation: EventPropagationPolicy` を追加する。独立した RiskEvent
  モデルは導入しない — 現行アーキテクチャの window がイベントの写像で
  あり、dependency graph の実装（次段階）まで二重のモデルを持たない
- `EventRiskCalendar.mode_for_instrument(spec, horizon, now)` を追加。
  DIRECT_LEGS の window は `affected ∩ {base, quote}` が空ならそのペアを
  止めない（**ECB だけで USDJPY を止めない**）。coverage の意味論
  （None = 暦が語れない）は mode_for と共有する
- `EventPropagationPolicy`:
  - `DIRECT_LEGS`: leg 交差のみ
  - `GLOBAL_CRITICAL`: 全ペアの hard gate。**FED（FOMC）の政策決定は
    これに固定**（GBPJPY ≒ GBPUSD × USDJPY の synthetic cross 伝播。
    年間の限定的な機会損失より false-negative event entry を避ける）
  - `DEPENDENCY_GRAPH`: scaffold。sensitivity 導出を実装するまで
    GLOBAL_CRITICAL と同じ全ペア到達（保守側）に倒す
- **中央銀行 cluster の pair-local 化**: cluster は bank 単位で構築する
  （FED+BOJ の近接会合を 1 window に統合しない）。「連続会合は 1 リスク
  状態」の原則は、重なり合う scope 付き window の最大 severity を
  mode_for_instrument が取ることで pair-local に保たれる — 両 leg を持つ
  ペアに切れ目は出ず、片 leg のペアが他行の会合で止まらない。
  4 中銀を 1 cluster に混ぜることを構造的に不可能にする
- `affected_currencies` が空の window は全ペアに適用（fail-close の従来
  互換。手書きの window 定義が scope を忘れても止める側に倒れる）
- meeting file は **schedule（採点パス外・window の材料）のみ** BOE /
  ECB を許可する。facts（`PolicyMeeting`）は採点（`EVENT_TYPES`）が
  BOJ/FED のみのため従来どおり — BOE/ECB の facts と採点接続は
  M3 2/3 の CurrencyState とセットで解禁し、日程の yaml 登録は
  GBP/EUR ペアの shadow 開始前に行う（bank → 通貨は `BANK_CURRENCIES`）

## 理由

- 現行の `mode_for(horizon, now)` には scope がなく、4 ペア化すると
  ECB 会合が USDJPY を止める（不要な機会損失）か、逆に scope を絞りすぎて
  FOMC が GBPJPY に届かない（false negative）かの二択になる。leg 交差 +
  GLOBAL_CRITICAL の二段構えが v2.1 の initial safety decision
- window 統合による cluster は「どのペアにも同じ 1 状態」を強制する。
  scope 付き window の重なりとして表現すれば、同じ「切れ目のない gate」
  が通貨ごとに正しい範囲で成立する

## 影響

- engine / shadow の event gate は `mode_for_instrument` に切替済み。
  単一銘柄運用（USDJPY + FED/BOJ 登録）では従来と同じ grading になる
  （FED は GLOBAL_CRITICAL、BOJ は JPY leg で必ず交差するため）
- `mode_for` は「全 window・scope 無視」の保守的 view として残る
