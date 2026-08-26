# ADR-011: position 上限は per-symbol と portfolio 全体の二層で持つ

**Status:** Accepted (2026-08-26)

## Decision

global の `max_open_positions: 1` を分解する。

- `max_open_positions_per_symbol`（既定 1）: 同一 symbol の broker position 数上限
- `max_open_positions_portfolio`（既定 3）: 全 symbol 合計の上限

どちらも「broker position が増える注文」だけに掛かる（netting の net 縮小
OPEN / INCREASE は従来どおり適用外）。HEDGING mode でも同一 symbol に無制限の
複数 ticket を許さない。

live 系 overlay（production / micro_live）は多ペア live を明示的に判断する
まで `portfolio: 1` を固定し、現行の実効挙動（同時 1 position）を維持する。
既定の `portfolio: 3` は 4 ペア platform への保守的初期値であり、検証結果
（OOS / Monte Carlo / shadow）に基づいて変更できる（設計書 v2.1 §24）。

## Rationale

単一の global cap は「どの pair か」を区別できず、多ペア化すると
「USDJPY を 1 本持っているから EURUSD に入れない」（過剰制限）か、cap を
上げて「同一 pair に複数 ticket」（意図しないリスク集中）のどちらかになる。
リスクの粒度が pair 内と portfolio 全体で異なる以上、上限も二層必要になる。

per-symbol 1 を保つ限り、単一ペア運用の挙動は分解前と同一であることを
回帰テストで固定する。

## Consequences

- `PreTradeContext` は symbol 件数と portfolio 件数の両方を運ぶ
- reject code は `MAX_OPEN_POSITIONS_PER_SYMBOL` / `MAX_OPEN_POSITIONS_PORTFOLIO`
  に分かれ、決定記録からどちらの層で止まったか判別できる
- 将来 strategy 単位の上限が必要になれば、strategy_id を含む別 limit を
  追加する（この二層を流用しない）
