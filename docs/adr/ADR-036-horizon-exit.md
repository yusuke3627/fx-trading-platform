# ADR-036: 保有期限による時間切れ決済

**Status:** Accepted (2026-09-11)

## Context

`expected_horizon_seconds` は SYSTEM_SPEC v2.0 [§7.2](../SYSTEM_SPEC.md#s7-2) では、signal の `expires_at` を「signal 生成時刻 + horizon」で導くためだけに使われる。
すなわち signal の属性であり、建玉の属性ではない。

issue #148（H4）は、時間切れ決済が scalp 戦略の成績を改善するかを、`horizon_exit_enabled` の `true` / `false` の 2 腕で測ることを求める。
測定には実際の決済規則が必要である。

SYSTEM_SPEC は v2.0 で凍結されており、[§1](../SYSTEM_SPEC.md#s1) は発行後の仕様変更を本文改訂ではなく ADR 追加で行うと定める。
`tasks/issue-148.md` は作業計画であって規範の追補ではない。

## Decision

1. **`expected_horizon_seconds` に第 2 の意味を与える。**
   instrument の `horizon_exit_enabled` が `true` のとき、この値はその strategy が建てた建玉の最大保有時間でもある。
   既定は `false` であり、明示的に有効化しない限り挙動は変わらない。

2. **保有時間の時計は strategy に注入された `Clock`（`ctx.clock.now()`）とする。**
   起点は、その向きの建玉として strategy が最初に観測した `VirtualPosition` snapshot の `as_of` である。
   broker の約定時刻でも wall clock でもない。
   backtest / replay の決定性を保ち、「Strategy 内で `datetime.now()` を直接呼ばない」不変条件を守るためである。

3. **起点時刻は strategy ローカルのプロセス内 memo（`_horizon_exits`、symbol をキーとする）で持つ。**
   `VirtualPosition` にも DB にも列を足さない。
   domain の snapshot は append-only であり、`as_of` は `INCREASE` 等の更新のたびに進むため、建玉の開始時刻として使えない。
   また、測定のための実験に domain と DB の変更を持ち込まないためである。

4. **発火時は決済専用 signal を出す。**
   形は ADR-031 / [§6.3](../SYSTEM_SPEC.md#s6-3) と同じである。
   `desired_direction` は保有の逆方向とし、`exit_only=True`、`stop_distance_pips=0`、`conviction=1.0` とする。
   reason code に `HORIZON_EXPIRED` を付け、setup 由来の決済と決定記録上で区別する。
   通常の Portfolio 経路を通るため、fresh な仮想ポジションに対する `CLOSE` になり、裸の反対売買にはならない。

5. **1 建玉につき 1 回だけ発火する。**
   発火時に memo の発火済みフラグを立て、`CLOSE` の執行 latency 中に再評価されても 2 回目を出さない。
   再武装は、strategy が flat を観測して memo を捨てたとき、または逆方向の建玉を観測して memo を新しい snapshot の `as_of` で取り直したときに起きる。

6. **時間切れ決済の判定は session gate より前に行う。**
   gate 閉鎖中でも発火する。
   ADR-010 / ADR-031 と同じ「entry は fail-close、exit は止めない」非対称を踏襲する。

7. **`failed_spike_reversal` の `strategy_version` を `0.1.0` から `0.2.0` へ上げる。**
   時間切れ決済の有無で記録済み signal を同じ戦略として比較できないためである。

## Consequences

- **約定失敗や拒否時**：`CLOSE` が Risk か執行で拒否されても、memo は既に発火済みであるため strategy は再送しない。
  建玉は setup 由来の決済か protective stop まで残る。
  発火後 flat を観測するまで `_evaluate` を抑止し続ける状態機械は、`CLOSE` が拒否されたときに戦略がその symbol で固まるため採らなかった。

- **再起動時**：memo はプロセス内にあり、`VirtualPositionLedger` も起動時に空で作られ（`src/trading/live/shadow.py`、`src/trading/backtest/engine.py`）、DB から復元されない。
  再起動後は新しい fill が届くまで strategy に建玉が見えないため、再起動をまたいだ建玉はこの規則では決済されない。
  これは **live 昇格前に解決すべき残課題**である。

- **flat を観測しない同方向の建て直し**：発火後の `CLOSE` latency 中に同方向の setup が出て `CLOSE` → `OPEN` が次の評価までに完了すると、同方向の `INCREASE` と区別できず、新しい建玉が前の開始時刻と発火済みフラグを引き継ぐ。
  latency 窓（normal で 150ms）内に新しい setup が必要であるため稀であり、検出も抑止もしない。

- **配線と既定値**：現在配線されているのは `failed_spike_reversal`（既定 300 秒）と `post_event_failed_breakout`（既定 21600 秒）の 2 本のみである。
  `config/base.yaml` ではどちらも `horizon_exit_enabled: false` で出荷する。

- take profit は本 ADR の範囲外であり、導入しない。

- 本 ADR は [§7.2](../SYSTEM_SPEC.md#s7-2) の `expected_horizon_seconds` の意味をその範囲で改訂し、決済規則については [§6.3](../SYSTEM_SPEC.md#s6-3) の決済専用 signal の形を用いる。
