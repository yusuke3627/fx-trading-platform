# ADR-039: broker の範囲外の rollover 曜日を銘柄設定で補う

**Status:** Accepted (2026-09-17)

## Context

[SYSTEM_SPEC §5.11](../SYSTEM_SPEC.md#s5-11) は、[ADR-016](ADR-016-swap-rollover-pit-broker-cost.md)
から引き継いだ規則として、broker の返却値を曜日倍率の truth source とし、水曜を一律に 3 倍とするハードコードを禁じている。
曜日別倍率がない場合は `swap_rollover3days` の曜日を 3 倍、週末を 0 倍、残りを 1 倍とする。

2026-09-16 の OANDA-Japan MT5 Demo（build 6140）での実測では、USDJPY / EURUSD / GBPUSD / GBPJPY の
4 銘柄とも `swap_mode=1`（POINTS）、`swap_rollover3days=7` で、曜日別倍率は公開されず NULL だった。
`ENUM_DAY_OF_WEEK` は 0（日曜）から 6（土曜）のため、7 はどの曜日にも一致しない。
従来の計算では 3 日分の rollover が一度も計上されず、水曜の 2 日分を取りこぼす。

SYSTEM_SPEC は v2.0 で凍結されているため、本 ADR で範囲外の broker 値に対する例外規則を定める。

## Decision

1. **曜日別倍率を最優先する。** 対象曜日の `swap_sunday` から `swap_saturday` が得られる場合は、
   `swap_rollover3days` や設定の曜日にかかわらず、その倍率を使う。

2. **broker の曜日が範囲外の場合に限り銘柄設定で補う。** 曜日別倍率がない週末は従来どおり 0 倍とする。
   平日は `swap_rollover3days` が 0〜6 ならその値を使い、範囲外なら `InstrumentPolicy.swap_triple_weekday` を使う。
   設定は省略可能で、指定する場合は `ENUM_DAY_OF_WEEK` の 1〜5（月〜金）に制限する。
   週末は rollover が発生せず常に 0 倍のため、週末を指定すると 3 倍が一度も成立せず、過少計上を見逃すおそれがある。
   決まった曜日を 3 倍、他の平日を 1 倍とする。スワップ無効時は曜日の解決をせず 0 を返す。

3. **曜日が決まらなければ例外にする。** 平日の倍率を決める際、broker の曜日が範囲外で設定もなければ
   `UnknownTripleSwapWeekdayError` を送出する。メッセージには銘柄と broker が返した値を含め、黙って 1 倍にはしない。

4. **4 銘柄の基本曜日を設定する。** `config/base.yaml` の USDJPY / EURUSD / GBPUSD / GBPJPY に
   `swap_triple_weekday: 3`（水曜）を指定し、research から BacktestEngine の carry 計算へ渡す。
   曜日はコードへ固定しない。collector は引き続き broker の生値を保存する。

## Consequences

- broker の曜日が 7 でも、設定に従って水曜に 3 日分の carry を計上できる。
  有効な broker の曜日や曜日別倍率が得られる場合は、それらが設定に優先する。
- carry の計算結果が変わるため、エンジン版を更新する（ENGINE_VERSION 0.8.0）。
  既存 research レポートとは manifest の `engine_version` が異なるため、`ablation_compare` で相互に比較できない。
- 未設定の銘柄で不明な曜日に遭遇すると backtest が例外で停止するため、carry の過少計上を見逃さず設定を確認できる。
- `known_at <= boundary` の snapshot 選択、rollover 境界、POINTS の金額計算は変更しない。
  DB スキーマ、既存 ADR、SYSTEM_SPEC の本文も変更しない。
- 祝日・銀行休業日に伴う変則やスワップカレンダーの取り込みは対象外とする。
  曜日別倍率がない場合、その変則による差額は未対応のまま残る。
