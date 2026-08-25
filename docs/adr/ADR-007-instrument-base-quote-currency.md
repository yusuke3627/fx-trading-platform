# ADR-007: InstrumentSpec は base/quote 通貨を identity として持つ

**Status:** Accepted (2026-08-26)

## Decision

`InstrumentSpec` に `base_currency` / `quote_currency`（`Currency` enum、必須）を
追加する。値の出所は次の2つに限る。

- live / demo: MT5 `symbol_info()` の `currency_base` / `currency_profit`
  （FX では profit 通貨 = quote 通貨）
- backtest / synthetic: データセット定義が明示する値

symbol 文字列（"USDJPY" 等）の parse による導出は禁止する。

`Currency` は取引対象の4通貨（USD / JPY / GBP / EUR）に限定し、それ以外の通貨を
broker が返した場合は spec 構築時点で ValueError で落とす（fail-loud）。

## Rationale

マルチカレンシー化（設計書 v2.1 §7）で、以下すべてが base/quote を必要とする。

- JPY 口座の pip value / 損失換算（quote 通貨 → JPY の conversion path 決定）
- currency exposure の leg 分解（BASE += U / QUOTE += −U×P）
- pair state projection（`base_score − quote_score`）
- event risk の currency scope（`affected ∩ {base, quote}`）

symbol parse に依存すると broker の symbol alias（"USDJPY.oj" のような接尾辞付き
シンボル）で静かに壊れる。broker が構造化された値として通貨を返す以上、それを
truth source にする。

## Consequences

- `InstrumentSpec` を構築する全箇所（MT5 mapper / backtest データセット定義 /
  テストファクトリ）が通貨の明示を要求される。
- 対応外通貨の instrument は設定ミスとして起動時に検出される。第5通貨の追加は
  `Currency` enum の拡張 = 明示的なコード変更になる。
- Risk / Portfolio 層は今後この2フィールドを前提にでき、symbol 文字列の解釈を
  持たない（ADR-008 の Money 型とセットで通貨次元バグを型で塞ぐ）。
