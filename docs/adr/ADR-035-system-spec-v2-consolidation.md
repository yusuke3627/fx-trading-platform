# ADR-035: 2本目の live pair より前に SYSTEM_SPEC v2.0 を統合する

**Status:** Accepted (2026-09-09、ユーザー承認)

## Context

issue #65 は M5 完了後に、凍結中の v1.3 と採用済み ADR の規範を v2.0 へ統合することを求める。
M5 の Portfolio Arbitrator と OMS queue は PR #113、#112、#131 により main に実装され、送信経路は ADR-032 で明文化されている。
現行の AGENTS.md は本文改訂を禁止しているため、統合の範囲と切替条件を先に合意する。

## Decision

2026-09-09 のユーザー承認に基づき、仕様書を v2.0 へ統合する。
統合は既存の採用済み決定を一箇所へまとめる作業とし、新しい売買権限や未実装機能を追加しない。
対象の基準は main の `fb42d83` にある v1.3 と ADR-001〜ADR-033 とする。
統合作業の開始時に main を更新し、その後に採用された ADR があれば対象表へ追加する。

`docs/research/2026-08-25-fx-multicurrency-system-design-v2.1.md` は実装設計の参考資料として扱う。
ADR と実装による裏付けのない将来案を、規範として本文へ取り込まない。
特に dynamic correlation、risk budget split、複数銘柄 backtest、非USDJPYの live 昇格は、既存 ADR が残課題とする範囲を維持する。

### 統合対象

| v2.0 の対象 | 根拠となる ADR |
| --- | --- |
| 口座モード、保護注文、netting 配分、JST risk day | 001、002、003、004 |
| broker ラベルと可視化時刻、長時間足、replay 時刻復元、時計逆行 | 005、006、014、033 |
| 通貨型と口座通貨換算、exit の換算例外、portfolio 制限 | 007、008、009、010、011、013 |
| platform 対応と取引許可の分離 | 012 |
| GBP/EUR 調達と昇格 gate、swap の PIT snapshot | 015、016 |
| 通貨別イベントリスクと intelligence、factor 入力、proxy、鮮度 | 017、018、019、020、021、022、025、026 |
| 銘柄別設定、session 時刻、shadow 多銘柄評価、entry gate | 023、024、027、028 |
| 同時 signal の裁定、送信 queue、決済専用 signal、worker 契約 | 029、030、031、032 |

### 発行条件

1. 統合する決定ごとに v2.0 の節との対応を記載し、既存 ADR のどの決定が移管されたかを追跡可能にする。
2. 完全に移管された ADR のみ `Superseded by SYSTEM_SPEC v2.0 §<節>` と記録する。
   未解決の制約や履歴の理由は削除しない。
3. AGENTS.md の「v1.3 で凍結」を v2.0 へ改訂し、v2.0 発行後の変更も ADR を追加する規則に揃える。
4. Scalping の `RESEARCH_ONLY`、Strategy/LLM の執行系到達禁止、UNKNOWN 再送禁止、fresh select と ticket による exit、look-ahead 禁止、最小ロット超過時の見送りを維持する。
5. 既存実装と不変条件テストに整合することを確認し、仕様に合わせるためにテストを緩めない。
6. EURUSD を2本目の live pair として有効にする前に v2.0 を発行する。
   発行自体は取引許可としない。#59、#57、#60、#66 の調達、実測、OOS、shadow、demo の各 gate は別途満たす。

## Consequences

統合改訂が main に取り込まれるまでは、main の v1.3 と採用済み ADR が規範である。
取り込み時に v2.0 を発行し、[移管先一覧](../SYSTEM_SPEC.md#s11) を現行規範への入口とする。
承認は上記範囲での文書統合を許可するもので、マージ、デプロイ、live 発注の許可を含まない。
統合で規範とコードの新しい不一致が見つかった場合は、本 ADR を根拠にコードの挙動を変更せず、別の判断事項として記録する。
