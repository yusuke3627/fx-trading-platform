# ADR-038: 戦略の利確距離をブローカー側の保護へ変換する

**Status:** Accepted (2026-09-15)

## Context

`StrategySignal` は損切り距離を表現できるが、利確を指定する項目を持たない。
`ProtectionSpec` 以降の OMS、MT5 マッパー、`ExecutionSimulator` は利確価格に対応しているものの、`PortfolioManager` が `take_profit_price` を常に `None` とするため、戦略から利用できない。
issue #157 のレンジ中央での全決済や 2R 利確を測定するには、戦略が利確を表現する経路が必要である。

SYSTEM_SPEC は v2.0 で凍結されており、[§1](../SYSTEM_SPEC.md#s1) に従って ADR で仕様を追加する。
[ADR-036](ADR-036-horizon-exit.md) が範囲外とした take profit を、本 ADR で扱う。

## Decision

1. **戦略は利確距離を pips で signal に指定できる。**
   `StrategySignal.take_profit_distance_pips` を `Decimal | None` として追加し、値がある場合は正の値を要求する。
   `Strategy.make_signal` と `Strategy._setup_signal` に省略可能な引数として通す。
   R 倍から距離への計算は戦略側が担い、R 倍指定をドメインに追加しない。

2. **`PortfolioManager` が利確距離をブローカー側の利確価格へ変換する。**
   LONG は `entry_price + take_profit_distance_pips * pip_size`、SHORT は `entry_price - take_profit_distance_pips * pip_size` とする。
   `entry_price` と `pip_size` は損切りと同じ `SizingInput` を使い、計算には `Decimal` を用いる。
   Strategy は signal を生成するだけで、Risk、OMS、Broker への依存を追加しない。

3. **利確はブローカー側の保護とし、決済専用 signal には付けない。**
   `_setup_signal` は entry 側にだけ利確距離を渡し、session 閉鎖中の `exit_only` signal には渡さない。
   `_horizon_exit` と `PortfolioManager._close_intent` の挙動は変えず、CLOSE intent は保護指定を持たない。
   利確は裸の反対売買ではなく、既存の保護約定として扱う（[§6.3](../SYSTEM_SPEC.md#s6-3)）。

4. **増し玉（INCREASE）でも損切りと同じ保護の扱いを使う。**
   netting では新しい command に指定された保護が建玉全体に適用される。
   利確を増し玉した数量だけに限定する処理や、利確だけの特別扱いは追加しない。
   シミュレータでは新しい command の保護が未指定なら、損切りと同様に既存の利確価格を維持する。

5. **既定値は `None` とする。**
   利確距離を省略した signal は利確価格を指定せず、既存戦略の振る舞いは変わらない。
   有効化は戦略ごとのパラメータで行う。本 ADR は特定の戦略への利確導入を決定するものではなく、既存戦略や `config/` は変更しない。

## Consequences

- **決済の不変条件を維持する。** `ExecutionSimulator.check_protection` は利確到達時に建玉を book から外す。
  保護約定で決済済みの ticket へのシステム決済は fresh select で NOOP となり、二重決済や裸の反対売買を発生させない。既存の Risk、OMS、シミュレータ、MT5 マッパーは変更しない。

- **計測に利確の決済理由が残る。** replay の利確は `ProtectionReason.TAKE_PROFIT` の保護約定となり、`result.trades` と `trades.csv` に `PROTECTION_CLOSE:TAKE_PROFIT` の round trip として記録される。
  固定 seed の合成 tick で実際に到達する距離を使い、LONG と SHORT の両方で検証する。

- **DB スキーマは変更しない。** 変換後の利確価格は position intent と execution command の既存列に保存できる。
  signal の利確距離自体は現在の `strategy_signals` の保存・読取対象に含まれず、DB から読み戻した signal では `None` になる。
  元の距離を含む signal の完全な保存・復元が必要になった場合は、別途マイグレーションと repository の対応が必要である。

- **戦略への採用は測定と併せて別途判断する。** トレーリングストップ、部分利確、既存戦略への有効化、および issue #157 の候補戦略の実装は本 ADR の範囲外とする。
