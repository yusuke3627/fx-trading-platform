# ADR-034: Session VWAP のバー選択に実 UTC を使う

**Status:** Accepted (2026-09-09)

## Decision

Session VWAP は `Bar.start` の broker 壁時計ラベルを、設定された `market.broker_server_ahead_of_ny_hours` と New York の timezone から実 UTC へ戻してセッション開始と比較する。
Live runner と backtest は同じ変換関数と設定値を IndicatorService へ渡す。
変換関数は research CLI から市場データ層へ移し、replay の既存時刻復元でも使う。

ADR-005 の保存時刻と可視化時刻の分離は維持する。
`Bar.start` の保存形式は変更せず、`known_at` は可視化制限にのみ使う。
遅着や backfill の受信時刻をセッション時刻へ流用しない。

## 理由

broker の 10:00 というラベルが実 UTC の 07:00 を表す場合、その直前のラベル 09:59 は London の開始前になる。
ラベルを UTC として比較すると開始前の価格が VWAP に混ざるため、比較する両辺を実時刻に揃える。
IANA timezone による London の開始判定を維持することで、米国と英国の夏時間切替日の違いにも従う。

## 影響と制約

ADR-024 の既知の制約と issue #100 を解消する。
同 ADR の broker anchor の用途に Session VWAP を追加する。
窓の絞り込みが変わるため session 指定の VWAP 値は変わりうるが、session 未指定の計算と保存済み Bar は変わらない。
DB migration は不要。

ADR-014 の NY close anchor を前提とする。
DST の重複または欠落するラベルは、従来の replay と同様に推測せずエラーにする。
broker anchor 自体の実測は issue #136 に残る。
