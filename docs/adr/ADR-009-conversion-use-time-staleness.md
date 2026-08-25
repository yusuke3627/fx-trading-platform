# ADR-009: 口座通貨換算は use-time で staleness を判定し、risk-increasing は fail-close する

**Status:** Accepted (2026-08-26)

## Decision

口座通貨（JPY）への換算は `AccountCurrencyConversionService` だけが行う。

- 入出力は `Money`。呼び出し側は生の conversion rate に触れない（ADR-008）
- staleness は `convert(..., now=...)` の**使用時**に毎回評価する。
  `is_stale` のような時間依存 bool を DTO に保存しない
- purpose で挙動を分ける:
  - `RISK_INCREASING`（新規 entry / size increase の sizing）: quote 欠損・
    stale・未来 timestamp・非正値価格で **fail-close**
    （`CONVERSION_RATE_UNAVAILABLE` / `CONVERSION_RATE_STALE`）
  - `MONITORING`（既存 position の評価）: stale なら last-good quote に
    haircut を掛けて返す。source 異常（未来 timestamp・非正値）は monitoring
    でも失敗させる
- rate は損失評価が過小にならない側を使う: 直接 quote は ask、inverse は 1/bid
- 換算 path は承認済みのみ（現時点: 同一通貨の恒等、USDJPY の直接・逆数）。
  自動 path 探索はしない
- `ConversionStress`（adverse % の決定論的 floor）を interface として持ち、
  direction 条件付き stress の推定実装は portfolio exposure 対応で差し替える
- 監査根拠（path / source known_at / leg age / purpose）は `ConversionTrace`
  に記録するが、risk 計算は `Money` のみを読む

## Rationale

GBPUSD / EURUSD の JPY sizing は USDJPY quote の鮮度に依存する（設計書 v2.1
§11）。この依存を暗黙にすると、USDJPY tick が止まったまま EURUSD の新規 entry
が古い rate で size され得る。逆に、staleness を理由に全機能を止めると、リスク
を**減らす**判断（reduce / exit）まで失う。「増やす行為は止める・減らす行為は
止めない」の非対称が安全側の設計であり、それを purpose として型に載せる。

replay では `InMemoryMarketData` が ReplayClock で可視性を絞るため、conversion
も自動的に `known_at <= replay_clock.now()` の quote しか見えない（look-ahead
禁止の不変条件が換算にも及ぶ）。

## Consequences

- sizing（#56）は `RISK_INCREASING` で呼び、失敗を reject code へ写像する
- 既存 position の監視・exit 判断は `MONITORING` で呼び、conversion 失敗では
  止まらない（ADR-010）
- haircut / buffer の既定値は demo の broker PnL 実測で校正するまで暫定値
- 新しい通貨 pair の換算が必要になったら、承認 path の追加として明示的に
  変更する（自動探索を足さない）
