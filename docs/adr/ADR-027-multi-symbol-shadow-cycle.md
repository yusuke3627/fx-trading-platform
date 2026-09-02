# ADR-027: shadow cycle を複数シンボルで評価する

**Status:** Accepted (2026-09-02)

## Context

従来の shadow runner は 1 つの `InstrumentSpec` と quote だけを持ち、quote が
欠けると cycle 全体を停止していた。この構造では、独立した collector を持つ
複数ペアを同時に評価できず、1 ペアの停止が他ペアを道連れにする。

一方、Strategy は 1 回の dispatch で設定された全 instruments を走査する。同じ
cycle を symbol ごとに dispatch すると、最初の dispatch で setup が記録され、
後続 symbol の signal を取りこぼす。このため dispatch 自体は cycle ごとに 1 回を
維持し、symbol ごとの分離は quote gate と sizing / risk grading で行う必要がある。

ADR-012 では `platform_enabled` の runner 対象選定への配線を本変更まで延期して
いた。複数シンボル化にあたり、評価対象と platform 対応状況の矛盾も起動時に
検出する必要がある。

## Decision

shadow runner は `primary_instruments`、または繰り返し指定された `--symbol` の
順序で複数シンボルを評価する。1 cycle は 1 instant・1 dispatch とし、quote の
有無と鮮度、sizing、event mode、risk context は symbol ごとに解決する。quote が
使えない symbol の signal は記録せず、他の symbol の評価は継続する。

gate は account、quote の順に適用する。account は全 symbol が同じ equity で
size され、同じ損失履歴で grade されるための cycle 全体の条件であり、symbol
固有の quote より先に確定させる。account が無いか古い場合は、全 symbol を同じ
理由で停止し、dispatch と feature refresh を行わない。

market event の `EventEnvelope.retrieved_at` と `known_at` には cycle instant を
使う。複数 quote に共通する単一の quote 時刻はなく、event はその instant に
見えている市場全体を表すためである。Strategy は market event の種別で起動し、
個別価格は market service から読むため、評価結果は変わらない。

`primary_instruments` または `--symbol` で選ばれた symbol が、稼働 Strategy の
対象でない場合、あるいは `platform_enabled` でない場合は黙って除外せず起動を
拒否する。設定矛盾を起動時に表面化させる方が、評価されない symbol を抱えた
まま動き続けるより安全であり、未登録 Strategy を拒否する wiring の方針とも
一致する。

## Consequences

- 1 ペアの quote collector が停止しても、fresh quote がある他ペアは評価できる。
- Strategy の dispatch と setup memo の意味は変わらず、signal を取りこぼさない。
- `ShadowCycle` は停止理由を symbol ごとに返し、1 symbol でも停止中なら
  `--once` は非ゼロで終了する。
- portfolio arbitrator の差し込み前に全 candidate を size する二段階構造になるが、
  arbitrator 自体と新しい Protocol は本決定に含めない。
- 起動時の `InstrumentSpec` 取得は対象 symbol ごとに MT5 を initialize / shutdown
  する。単一セッションへの最適化は行わない。
