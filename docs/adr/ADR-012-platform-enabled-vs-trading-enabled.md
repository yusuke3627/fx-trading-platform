# ADR-012: platform 対応と live 発注許可を instrument 単位で分離する

**Status:** Accepted (2026-08-26)

## Decision

トップレベル config `instruments:` に per-instrument の2フラグを持つ。

- `platform_enabled`: 収集・feature・shadow 評価・裁定シミュレーションの対象か
- `trading_enabled`: 実際の発注を許すか。`risk.trading_enabled`（global master
  switch）との AND で効く

設定に載っていない symbol は両方 false（fail-close）。risk engine は
`INSTRUMENT_TRADING_ENABLED` として新規 entry / increase だけを gate し、
既存 position の close / reduce は止めない。backtest は live 昇格前の pair の
検証こそが目的のため、この gate を適用しない（配線が常に許可を渡す）。

初期値: 4 ペアとも `platform_enabled: true`、`trading_enabled` は USDJPY のみ
true（rollout は USDJPY → EURUSD → GBPUSD → GBPJPY、ADR 化された Gate 通過で
順次有効化）。

## Rationale

「4 ペア対応の実装」と「4 ペアで発注する判断」は別の意思決定で、時期も根拠も
異なる（設計書 v2.1 §30）。1 つの bool に畳むと、実装の進行が発注許可を
引きずるか、発注を止めるために collection / shadow まで止まるかの二択になる。
shadow で signal / hypothetical sizing / reject 理由を蓄積できることが
昇格 Gate（#66）の判断材料そのものなので、評価と発注は独立に切り替えられる
必要がある。

## Consequences

- 新 pair の live 昇格は `instruments.<symbol>.trading_enabled` の 1 行変更 +
  該当 Gate の ADR で表現される（コード変更不要）
- symbol の設定漏れは発注不可として現れる（`max_units_per_symbol` の
  fail-close と同じ方針）
- `platform_enabled` の消費者（収集・runner の対象選定）はマルチシンボル
  runner 化（#63）で配線する。現時点で参照するのは trading 側のみ
