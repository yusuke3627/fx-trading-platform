"""Portfolio Arbitrator: 同時 signal の選択（設計書 v2.1 §25–27、ADR-029）。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from trading.domain.arbitration import (
    ACCEPTED,
    REJECTED_EXPIRED,
    REJECTED_REDUNDANT_FACTOR_EXPOSURE,
    REJECTED_TRADING_DISABLED,
    REJECTED_TRIANGLE_CAP,
    ArbitrationCandidate,
    ArbitrationDecision,
    ArbitrationResult,
    CandidateSignal,
)
from trading.domain.exposure import OpenPositionExposure
from trading.domain.instrument import InstrumentSpec
from trading.domain.money import Currency
from trading.domain.position import PositionDirection


class ArbitratorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # 係数は backtest で校正して固定する（設計書 §26 Step 7）。LLM / runtime から変更
    # しない。現在値は校正前の仮置き（ADR-013 の数値と同じ扱い）。
    # 既存 book と同方向の通貨 leg 1 本あたり、priority（edge_r × confidence）から引く量。
    existing_exposure_penalty_r: Decimal = Decimal("0.10")
    # triangle を構成する 3 ペアのうち同時に保有してよい distinct symbol 数。
    # 2 = 三辺を同時には持たない。
    max_pairs_per_triangle: int = 2


Leg = tuple[Currency, PositionDirection]


class PortfolioArbitrator:
    def __init__(self, config: ArbitratorConfig) -> None:
        self._config = config

    def select(
        self,
        candidates: Sequence[ArbitrationCandidate],
        book: Sequence[OpenPositionExposure],
        now: datetime,
    ) -> ArbitrationResult:
        valid: list[ArbitrationCandidate] = []
        rejected: list[ArbitrationDecision] = []
        candidates_by_id: dict[UUID, ArbitrationCandidate] = {}
        for candidate in candidates:
            signal = candidate.signal
            candidates_by_id[signal.signal_id] = candidate
            if signal.expires_at <= now:
                rejected.append(
                    self._decision(
                        signal,
                        now,
                        accepted=False,
                        reason_code=REJECTED_EXPIRED,
                        rank=None,
                        priority=None,
                        detail=(
                            f"expires_at={signal.expires_at.isoformat()} "
                            f"now={now.isoformat()}"
                        ),
                    )
                )
            elif not candidate.trading_enabled:
                rejected.append(
                    self._decision(
                        signal,
                        now,
                        accepted=False,
                        reason_code=REJECTED_TRADING_DISABLED,
                        rank=None,
                        priority=None,
                        detail=f"instrument policy does not allow trading {signal.symbol}",
                    )
                )
            else:
                valid.append(candidate)

        book_directions = self._book_leg_directions(book)
        priorities = {
            candidate.signal.signal_id: self._priority(candidate, book_directions)
            for candidate in valid
        }
        ranked = sorted(
            valid,
            key=lambda candidate: (
                -priorities[candidate.signal.signal_id],
                candidate.signal.strategy_id,
                candidate.signal.symbol,
                str(candidate.signal.signal_id),
            ),
        )

        specs = {
            exposure.spec.symbol: exposure.spec
            for exposure in [*book, *(candidate.exposure for candidate in candidates)]
        }
        triangles = self._triangles(specs)
        claimed: dict[Leg, CandidateSignal] = {}
        book_now = list(book)
        accepted: list[ArbitrationDecision] = []
        for rank, candidate in enumerate(ranked, start=1):
            signal = candidate.signal
            priority = priorities[signal.signal_id]
            legs = self._legs(candidate.exposure.spec, signal.position_direction)
            taken = sorted(leg for leg in legs if leg in claimed)
            if taken:
                currency, direction = taken[0]
                winner = claimed[(currency, direction)]
                rejected.append(
                    self._decision(
                        signal,
                        now,
                        accepted=False,
                        reason_code=REJECTED_REDUNDANT_FACTOR_EXPOSURE,
                        rank=rank,
                        priority=priority,
                        detail=(
                            f"{currency} {direction} already taken by "
                            f"{winner.strategy_id}/{winner.symbol}"
                        ),
                    )
                )
                continue

            held_symbols = {exposure.spec.symbol for exposure in book_now}
            capped = next(
                (
                    (triangle, (triangle & held_symbols) | {signal.symbol})
                    for triangle in triangles
                    if signal.symbol in triangle
                    and len((triangle & held_symbols) | {signal.symbol})
                    > self._config.max_pairs_per_triangle
                ),
                None,
            )
            if capped is not None:
                triangle, held = capped
                rejected.append(
                    self._decision(
                        signal,
                        now,
                        accepted=False,
                        reason_code=REJECTED_TRIANGLE_CAP,
                        rank=rank,
                        priority=priority,
                        detail=(
                            f"triangle {'/'.join(sorted(triangle))} "
                            f"held={sorted(held)} cap={self._config.max_pairs_per_triangle}"
                        ),
                    )
                )
                continue

            accepted.append(
                self._decision(
                    signal,
                    now,
                    accepted=True,
                    reason_code=ACCEPTED,
                    rank=rank,
                    priority=priority,
                    book_before=tuple(book_now),
                )
            )
            book_now.append(candidate.exposure)
            for leg in legs:
                claimed[leg] = signal

        rejected.sort(
            key=lambda decision: (
                decision.rank is None,
                decision.rank or 0,
                candidates_by_id[decision.signal_id].signal.strategy_id,
                candidates_by_id[decision.signal_id].signal.symbol,
                str(decision.signal_id),
            )
        )
        return ArbitrationResult(accepted=tuple(accepted), rejected=tuple(rejected))

    def _priority(
        self,
        candidate: ArbitrationCandidate,
        book_directions: Mapping[Currency, PositionDirection],
    ) -> Decimal:
        signal = candidate.signal
        legs = self._legs(candidate.exposure.spec, signal.position_direction)
        overlap = sum(
            1 for currency, direction in legs if book_directions.get(currency) is direction
        )
        return (
            signal.expected_edge_r * signal.confidence
            - self._config.existing_exposure_penalty_r * overlap
        )

    @staticmethod
    def _book_leg_directions(
        book: Sequence[OpenPositionExposure],
    ) -> dict[Currency, PositionDirection]:
        net: dict[Currency, Decimal] = {}
        for exposure in book:
            spec = exposure.spec
            net[spec.base_currency] = (
                net.get(spec.base_currency, Decimal(0)) + exposure.signed_units
            )
            net[spec.quote_currency] = (
                net.get(spec.quote_currency, Decimal(0))
                - exposure.signed_units * exposure.mark_price
            )
        return {
            currency: PositionDirection.LONG if amount > 0 else PositionDirection.SHORT
            for currency, amount in net.items()
            if amount != 0
        }

    @staticmethod
    def _legs(
        spec: InstrumentSpec, direction: PositionDirection
    ) -> frozenset[Leg]:
        if direction is PositionDirection.LONG:
            return frozenset(
                {
                    (spec.base_currency, PositionDirection.LONG),
                    (spec.quote_currency, PositionDirection.SHORT),
                }
            )
        return frozenset(
            {
                (spec.base_currency, PositionDirection.SHORT),
                (spec.quote_currency, PositionDirection.LONG),
            }
        )

    @staticmethod
    def _triangles(specs: Mapping[str, InstrumentSpec]) -> list[frozenset[str]]:
        triangles: list[frozenset[str]] = []
        ordered_specs = sorted(specs.values(), key=lambda spec: spec.symbol)
        for group in combinations(ordered_specs, 3):
            currencies = [
                {spec.base_currency, spec.quote_currency} for spec in group
            ]
            if len(set().union(*currencies)) != 3:
                continue
            if any(len(left & right) != 1 for left, right in combinations(currencies, 2)):
                continue
            triangles.append(frozenset(spec.symbol for spec in group))
        return triangles

    @staticmethod
    def _decision(
        signal: CandidateSignal,
        now: datetime,
        *,
        accepted: bool,
        reason_code: str,
        rank: int | None,
        priority: Decimal | None,
        detail: str | None = None,
        book_before: tuple[OpenPositionExposure, ...] = (),
    ) -> ArbitrationDecision:
        return ArbitrationDecision(
            arbitration_id=uuid4(),
            signal_id=signal.signal_id,
            accepted=accepted,
            reason_code=reason_code,
            rank=rank,
            priority=priority,
            detail=detail,
            decided_at=now,
            book_before=book_before,
        )
