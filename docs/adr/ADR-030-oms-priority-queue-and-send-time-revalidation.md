# ADR-030: OMS priority queue と送信直前 revalidation

**Status:** Accepted (2026-09-02)

## Context

複数通貨ペアの signal が同時に発生すると、broker の request 上限である同一
symbol 5 requests/sec と market new entry 1/sec に達する。queue の待機中には
signal の失効、価格変動、spread 拡大、event mode の変化、換算レートの陳腐化、
portfolio exposure の変化も起こり得る。DB claim は `created_at` 順に 1 行だけ
取得するため、執行上の優先順位を表現できない。

## Decision

in-memory の `ExecutionQueue` を DB claim の後、`SUBMITTING` の前に置き、
`CLAIMED` の command だけを載せる。優先順位は emergency、close / reduce、
protection repair、new entry、telemetry の 5 段階とし、同一優先度では Portfolio
Arbitrator の rank、enqueue 連番の順に決定論的に並べる。

rate limiter は sliding window 方式で、symbol ごとの全 request と全 symbol 共通の
market entry を二段階で制限する。上限値は `broker.rate_limit` から読み、emergency
も broker 側の上限を迂回しない。

送信直前に expiry、claim lease、ticket 付き exit の fresh select、pre-trade risk
再評価の順で確認する。送らない command は `EXPIRED` または `CANCELLED` へ遷移し、
risk の decision を dispatch 結果に残す。revalidation の拒否には `REJECTED` を使わず、
`CLAIMED` から合法的に到達できる `CANCELLED` を使う。`REJECTED` は作成時の risk
拒否と broker 拒否に予約する。state machine と DB schema は変更しない。

## Consequences

worker は claim した行を queue に載せ、`Dispatch.command` を
`save_state(expected_state=CLAIMED)` で永続化してから `order_send` へ進む。この配線と
revalidation 用 `PreTradeContext` の構築は M6 で実装し、queue は Risk の内部を知らない。

netting exit の ticket を持たない command は queue で send 時の delta を再計算せず、
`OMSService.command_for_netting` の fresh `net_exposure` 読み取りに依存する。送信直前の
再 delta は follow-up 候補とする。`PROTECTION_REPAIR` と `TELEMETRY` は順位だけを定義し、
producer は本変更に含めない。
