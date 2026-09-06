# ADR-028: session profile の entry gate と StrategyStatus の関係

**Status:** Accepted (2026-09-02)

## Context

ADR-023 で strategy × pair × session の名前参照（`SessionEntryPolicy` PREFERRED /
ALLOWED / SHADOW_ONLY / DISABLED）を定義したが、runtime の entry gate への配線は
session 判定の IANA timezone 化（ADR-024）の後へ送っていた。配線するには、policy と
strategy の lifecycle（`StrategyStatus`）の関係、session が重なる時間帯とどの session
も開いていない時間帯の扱い、profile の一覧を strategy がどう受け取るかを決める必要が
ある。

## Decision

gate は Strategy 層に閉じる。Strategy は `ctx.clock.now()` を `indicators/session.py`
の純関数に渡していま開いている session を得て、instrument が参照する profile の
policy で新規 signal の生成（entry）を止める。`StrategyContext` にフィールドは足さず、
runner・Portfolio・Risk は session を知らない。

policy と status の関係は次のとおり。「live」は `LIVE_ELIGIBLE_STATUSES`
（MICRO_LIVE / LIMITED_LIVE / PRODUCTION）、「非 live」は RESEARCH_ONLY /
BACKTEST_ELIGIBLE / SHADOW。

| policy | 非 live | live |
| --- | --- | --- |
| PREFERRED / ALLOWED | entry する | entry する |
| SHADOW_ONLY | entry する（research / shadow の証拠収集） | entry しない |
| DISABLED | entry しない | entry しない |
| 開いている session が無い、または profile に無い session だけが開いている | entry しない | entry しない |

- session が重なる時間帯は、開いている session のうち最も緩い policy を採る
  （DISABLED < SHADOW_ONLY < ALLOWED < PREFERRED）。London open の時間帯を Tokyo の
  policy で潰さないため。
- profile を参照する instrument は、どの session も開いていない時間帯（NY close から
  Tokyo open まで）に entry しない。profile は entry を行う session の列挙であり、
  列挙外は許可していないと読む（fail-close）。
- profile を参照しない instrument には gate が存在せず、挙動は従来どおり。
- gate は `_evaluate` の前で効く。setup の記憶（`_new_setup`）より前に止めることで、
  gate が閉じている間に現れた setup は session が開いた後に signal になれる。
- profile の一覧は `StrategyConfig.session_profiles` として全 strategy へ同じものを渡し、
  参照先の存在は `StrategyConfig` の validator で設定境界に確定する。

## Consequences

- `config/base.yaml` の参照どおり、scalp（全 session SHADOW_ONLY）は非 live でだけ
  評価され、live status では signal を出さない。swing（`usdjpy_core`）は 3 session
  すべてで entry できる。intraday は profile を参照しておらず変わらない。
- profile を参照する strategy は、backtest / shadow でも NY close から Tokyo open までの
  時間帯に entry しなくなる。その時間帯の research 評価が要る場合は、その config で
  `session_profile` の参照を外す。
- live status の strategy が SHADOW_ONLY の session で「shadow としては評価する」ことは
  できない。signal 単位で shadow 経路へ振り分けるには runner 側の配線が要り、本 ADR の
  範囲外とする。
- PREFERRED と ALLOWED の使い分け（sizing 等）は決めていない。gate としては同じ。

ADR-031 により一部改訂（gate 閉鎖中も保有の決済専用 signal は通す）。
