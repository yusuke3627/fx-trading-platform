# Windows VPS 選定（OANDA MT5 執行ホスト）— 2026-08-23

**これは SYSTEM_SPEC ではなく Research Note。** 将来の設計へ固定しない。
設計へ固定する決定（failover 構成等）は実装時に `docs/adr/` へ ADR として起こす。

出典: deep-research「OANDA MT5向け Windows VPS 選定調査」（2026-08-19 受領）を蒸留し、
2026-08-23 の契約決定を記録したもの。料金・キャンペーン情報は 2026-08 時点の取得値。
RTT・約定時間の数値は**レポートの工学的推定であり実測値ではない**（実測は issue #14）。

## 決定（2026-08-23 契約済み）

| 項目 | 決定 | 備考 |
|---|---|---|
| 事業者・リージョン | **ConoHa for Windows Server / 東京** | Linux VPS とは別商品。Linux 側の割引キャンペーンは対象外 |
| プラン | **8GB（6コア / SSD 100GB）** | レポートの開発推奨は 4GB だが下記の理由で 8GB |
| 課金 | **時間課金（月額上限 9,746円）** | 24/365 稼働なら上限に張り付く。長期割引（まとめトク 36ヶ月 8,855円/月）は live 昇格判断後に切替 |
| OS | Windows Server 2025 Datacenter（VPS 基盤 3.0） | RDS SAL / Office SAL は不要（Administrator の管理 RDP のみ） |
| OANDA 口座 | デモから開始。**自動売買可プラン（スタンダード）必須** | 裁量プランは EA・スクリプト不可（Python 自動発注が動かない） |

### 8GB を選んだ理由

レポートの「開発は ConoHa Tokyo 4GB」推奨は SQLite / 軽量 DB 前提。本プロジェクトは
本番 VPS に **MT5 + Python collectors + PostgreSQL を同居**させる構成が確定しており
（migrations 0001–0003 適用、tick 永久保存）、レポート自身も「PostgreSQL 同居なら
8GB 以上」としている。Windows Server ベースで約 2GB 消費する点も考慮。
バックテスト・研究ワークロードは Mac 側に残し、VPS は執行・収集に限定する。

### ConoHa（東京）で足りると判断した理由

- OANDA Japan の MT5 trade server は**東京設置**（OANDA 公式 FAQ）。本番
  `OANDA-Japan MT5 Live` / `mt5-trade.oanda.jp`、デモ `OANDA-Japan MT5 Demo` /
  `mt5-game.oanda.jp`。
- OANDA 内部処理は約 5ms、公開約定統計では 20ms 時点でほぼ処理済み・50ms で 100%。
  つまり **`order_send()` 全体ではブローカー側処理が支配項**で、東京 VPS 間の
  数 ms の RTT 差（下表）は結果を左右しにくい。
- 当プラットフォームの scalp は M1 ベースであり tick-HFT ではない。数十 ms の
  価格差を狙う戦略ではないため、東京圏に置けていれば十分。
- 時間課金で縛りがなく、**デモ運用そのものが ConoHa の実測になる**。P99 に不満が
  出た場合のみ Vultr Tokyo / AWS Tokyo との A/B 比較へ進む（レポートの本番推奨）。

## レポートの期待値（工学的推定・非実測）

ネットワーク RTT の期待レンジと `order_send()` の推定分布。SLA ではない。

| VPS | 期待 RTT | order_send Median | P95 | P99 |
|---|---:|---:|---:|---:|
| AWS Tokyo | 1–4 ms | 8–16 ms | 22–32 ms | 45–65 ms |
| Vultr Tokyo | 1–5 ms | 8–18 ms | 23–35 ms | 45–70 ms |
| **ConoHa Tokyo** | 1–6 ms | 9–20 ms | 24–38 ms | 48–75 ms |
| Vultr Osaka | 6–10 ms | 13–24 ms | 30–43 ms | 55–80 ms |
| さくら大阪 | 6–12 ms | 14–26 ms | 32–46 ms | 58–85 ms |
| さくら石狩 | 15–25 ms | 25–40 ms | 45–65 ms | 70–110 ms |

モデル: `T_order_send ≈ T_local(IPC) + RTT + T_OANDA`。急変相場・指標発表時は
ブローカー側キュー・流動性が支配的になり、100ms〜数百 ms の tail はどの VPS でも
起こり得る。

## 評価指標と判定ルール（実測時に使う）

指標の優先順位: **P99 > P95 > packet loss / disconnect > jitter > median > 平均**。
「median が 3ms 速い」より「P99 が悪化しない」ほうが重要（例: median 8ms/P99 180ms の
VPS より median 11ms/P99 55ms を採る）。

乗り換え判断の重み付け（レポート案）: order_send P99 30% / P95 20% /
MT5 ping P99 15% / disconnect・loss 15% / CPU 安定性 10% / 月額 10%。

計測上の制約:

- OANDA 東京サーバーは**同一銘柄 1 秒最大 5 件**の執行上限。ベンチマークでも超えない。
- 高サンプルの `order_send()` 計測は**デモ口座**で行う。本番は通常戦略の実注文ログで足りる。
- 経過時間は `time.perf_counter_ns()` で測る（`time.time()` は NTP 補正で汚れる）。
- OS の `ping`（ICMP）ではなく MT5 自身の `TERMINAL_PING_LAST`（μs、trade server への
  直近 ping）を主指標にする。

実測手順の具体的なチェックリスト（実接続 IP の特定 → ASN 確定 → ping 常時記録 →
order_check/order_send 計測 → 7–14 日集計）は issue #14 に記載。

## 将来の冗長化方針（ADR 化候補・今は実装しない）

- 待機系を置く場合は **passive standby + leader lock**。active-active は同一シグナル
  で二重発注するため禁止。
- フェイルオーバー順序: 接続確認 → `account_info` → `positions_get` → open order 取得 →
  自前 state と照合 → leader lock 取得 → 発注許可。
- 候補は別事業者・別地域（さくら大阪等）。石狩は DR 用途のみ。

## 一次資料

- OANDA 公式 FAQ: MT5 サーバー名・ホスト名・東京設置・約定スピード統計・
  1 秒 5 件の執行上限・プラン別の自動売買可否
- MetaQuotes 公式: `TERMINAL_PING_LAST`（μs）、`order_send()` / `order_check()`、
  Python package のローカル IPC 連携
- AWS 公式: 大阪→東京の典型レイテンシ 5–8 ms（大阪リージョン紹介資料）
- ConoHa for Windows Server 料金ページ（2026-08 実確認: 4GB 4,939円 / 8GB 9,746円、
  まとめトク 36ヶ月 8GB 8,855円/月）
