# ADR-024: Market session は MT5 server timezone から独立させる

**Status:** Accepted (2026-09-02)

## Decision

market session（TOKYO / LONDON / NEW_YORK）は IANA timezone のローカル市場時間で
定義し、Python `zoneinfo` の timezone database で UTC へ写像する。

- Tokyo: Asia/Tokyo の 09:00–18:00
- London: Europe/London の 08:00–17:00
- New York: America/New_York の 08:00–17:00

session 判定は broker server clock を参照しない。MT5 の NY クローズ規約では server
clock が New York より 7 時間先行し、US DST 中は UTC+3、冬時間は UTC+2 となるが、
この broker offset は market session の定義ではない。
`market.broker_server_ahead_of_ny_hours` の用途は replay 時刻復元（ADR-014）と swap
rollover 境界（ADR-016）に限る。

timezone 名は環境で変わる値ではないため、`session.py` 内の定数として保持する。

## 理由

- 設計書 v2.1 §33.8 のとおり、broker server time を market session として扱うと
  session を誤分類する。server clock と market-local timezone は分離する必要がある
- 固定 UTC 窓では設計書 v2.1 §33.9 の DST drift が起きる。London と New York の
  ローカル市場時間を timezone database で UTC へ写像すれば、季節ごとの 1 時間差を
  個別の season table なしで吸収できる

## 影響

- 夏時間の London は 07:00–16:00 UTC、New York は 12:00–21:00 UTC のまま変わらない
- 冬時間の London は 08:00–17:00 UTC、New York は 13:00–22:00 UTC となり、従来の
  固定 UTC 窓から 1 時間後ろへ移る
- `IndicatorService.vwap` の session anchor も、正しい実時刻を受け取る限り冬時間の
  session 開始へ追従する

## 既知の制約

`Bar.start` は broker clock の壁時計を UTC ラベルの aware datetime として保持しており、
現在はそのまま `IndicatorService.vwap` の session anchor に渡る。このデータ層の問題は
ADR-005 の時刻軸設計に関わるため、本 ADR の session 判定変更では扱わない。session 関数は
実時刻を表す aware datetime を入力契約とし、broker clock の正規化はデータ層で別途行う。
