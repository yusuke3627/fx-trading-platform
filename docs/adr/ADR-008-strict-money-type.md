# ADR-008: risk domain の金額は Money 型で通貨次元を持つ

**Status:** Accepted (2026-08-26)

## Decision

risk / portfolio domain の金額（risk budget、stop 損失、exposure の口座通貨
mark 等）は `Money`（`amount: Decimal` + `currency: Currency`、frozen）で表現
する。裸の `Decimal` のままでよいのは通貨次元を持たない値 — ratio / percentage /
units / price / indicator 値 — に限る。

- 異通貨の `Money.add` は `CurrencyMismatchError`。暗黙の通貨混同を型で禁止する
- 通貨をまたぐ変換は conversion service（ADR 追加予定、設計書 v2.1 §10）だけが
  行い、呼び出し側は生の conversion rate に触れない
- `_jpy` サフィックス等の命名規約は補助であって保証ではない。保証は型が持つ

## Rationale

現行 sizing は `loss_per_unit = stop_distance_pips × pip_size` を quote 通貨の
まま JPY の risk budget と比較している（`portfolio/manager.py` /
`risk/engine.py`）。USDJPY（quote=JPY）では偶然正しいが、EURUSD / GBPUSD
（quote=USD）では通貨次元が一致せず、USDJPY≈150 なら約150倍の over-size を許容
し得る（設計書 v2.1 §8）。

この種のバグは値が `Decimal` どうしである限りレビューでしか捕まらない。通貨を
型に載せれば、次元の混同はテスト以前に型エラー・実行時例外になる。

## Consequences

- 本 ADR 時点では型の導入のみ。sizing 計算の Money 化と conversion service は
  後続変更（#55 / #56）で行い、それまで現行 sizing の挙動は変わらない。
- 今後 risk domain に金額フィールドを足すときは `Money` を使う。`Decimal` の
  金額フィールドをレビューで見たら通貨次元の確認を求める。
- 対象通貨は `Currency` enum の4通貨に限定される（ADR-007 と同一の制約）。
