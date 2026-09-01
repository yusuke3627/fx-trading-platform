"""USD score perturbation sensitivity（設計書 v2.1 §12.2 / §34.5A）。

    python -m trading.backtest.usd_perturbation_study --env backtest

USD は 4 ペア中 3 ペアに現れるため、USD model error はポートフォリオ全体へ
伝播しうる。ここでは USD の集約 directional_score へ ±0.25 / 0.5 / 1.0σ の
決定的な摂動を与え、実 PIT データ上で各 USD ペアの方向（pair score の符号）
が何日反転するかを測る。

**摂動は集約 score に掛ける。** 設計書の文言（「USD scoreへ±0.25σ相当の
perturbation」）どおり、ペア射影が消費する値そのものを動かす。factor 別の
z への摂動はサービス内部への到達が必要になるうえ、射影が見る量は集約後の
score だけなので、伝播の測定としては同じ問いに答える。

**σ は測定窓の USD score 系列から取る。** 摂動の単位を「USD score が実際に
動く幅」に固定することで、±1.0σ が「観測されたモデル変動の典型 1 単位」を
意味する。窓が変われば σ も変わる — それは仕様で、報告に σ を明記する。

**flip は厳密な符号反転だけ数える。** 基線と摂動後の両方が非ゼロで符号が
逆のときのみ。基線が 0 の日（方向感なし）は flippable ではないので別枠で
数える。基線 |score| の中央値と p10 を並記するのは、flip の起きやすさが
「基線がゼロにどれだけ近いか」でほぼ決まるため。

**乱数なし。** 同じ入力に対して出力は文字列単位で一致する（§34.5A）。

§12.2 の残り 4 指標 — accepted-trade set change / portfolio USD
concentration / PnL・DD sensitivity / arbitration ranking stability — は
通貨 state を消費する strategy・portfolio・arbitrator が存在するまで
（M4/M5）構造的に測定できない。繰り延べの記録は docs/research/ 側にある。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.domain.instrument import FillingMode, InstrumentSpec
from trading.domain.money import Currency
from trading.intelligence.currency import (
    CurrencyScoreConfig,
    CurrencyState,
    project_pair,
)

# 摂動の大きさ（σ 単位、符号つき）。設計書 §12.2 の ±0.25 / 0.5 / 1.0。
PERTURBATION_STEPS: tuple[Decimal, ...] = (
    Decimal("-1.0"),
    Decimal("-0.5"),
    Decimal("-0.25"),
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal("1.0"),
)

_ONE = Decimal(1)


def _study_spec(
    symbol: str, base: Currency, quote: Currency, digits: int, pip_size: str
) -> InstrumentSpec:
    """研究用のデータセット定義（per-dataset input、broker ハードコードでは
    ない）。ペア射影は symbol と通貨しか読まない — 残りは合成データセットと
    同じ埋め草。"""
    return InstrumentSpec(
        symbol=symbol,
        base_currency=base,
        quote_currency=quote,
        digits=digits,
        pip_size=Decimal(pip_size),
        contract_size=Decimal(1000),
        volume_min=Decimal(1000),
        volume_step=Decimal(1000),
        volume_max=Decimal(1_000_000),
        stop_level_points=0,
        accepted_filling_modes=frozenset({FillingMode.IMMEDIATE_OR_CANCEL}),
    )


# USD が leg に入るペアだけが対象。GBPJPY は USD を含まないので測らない。
USD_PAIR_SPECS: tuple[InstrumentSpec, ...] = (
    _study_spec("USDJPY", Currency.USD, Currency.JPY, 3, "0.01"),
    _study_spec("GBPUSD", Currency.GBP, Currency.USD, 5, "0.0001"),
    _study_spec("EURUSD", Currency.EUR, Currency.USD, 5, "0.0001"),
)

Snapshot = Mapping[Currency, CurrencyState]


def usd_sigma(snapshots: Sequence[Snapshot]) -> Decimal:
    """測定窓での USD directional_score の母標準偏差。

    USD が観測されない日を除いた系列で取る。1 日以下なら散らばりは
    定義できないので 0（摂動はゼロ幅になり flip も 0 のまま報告される）。
    """
    scores = [
        float(snapshot[Currency.USD].directional_score)
        for snapshot in snapshots
        if Currency.USD in snapshot
    ]
    if len(scores) < 2:
        return Decimal(0)
    return Decimal(str(statistics.pstdev(scores)))


def perturbed_usd(usd: CurrencyState, shift: Decimal) -> CurrencyState:
    """USD の集約 score だけを動かした state。score の値域 [-1, 1] は
    model_copy が再検証しないので、ここで保証する。"""
    score = max(-_ONE, min(_ONE, usd.directional_score + shift))
    return usd.model_copy(update={"directional_score": score})


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


@dataclass(frozen=True)
class PairSensitivity:
    symbol: str
    days: int  # 両 leg が揃った日数
    missing_leg_days: int
    zero_sign_days: int  # 基線 score が 0 で flippable でない日数
    median_abs: float | None  # 基線 |pair score| の中央値
    p10_abs: float | None
    flips: Mapping[Decimal, int]  # k（σ 単位）→ flip した日数


@dataclass(frozen=True)
class StudyResult:
    sigma: Decimal
    usd_days: int
    pairs: tuple[PairSensitivity, ...]


def measure(
    snapshots: Sequence[Snapshot],
    config: CurrencyScoreConfig,
    specs: Sequence[InstrumentSpec] = USD_PAIR_SPECS,
    steps: Sequence[Decimal] = PERTURBATION_STEPS,
) -> StudyResult:
    sigma = usd_sigma(snapshots)
    pairs = []
    for spec in specs:
        margins: list[float] = []
        flips: dict[Decimal, int] = {step: 0 for step in steps}
        days = missing = zero_sign = 0
        for snapshot in snapshots:
            base = snapshot.get(spec.base_currency)
            quote = snapshot.get(spec.quote_currency)
            if base is None or quote is None:
                missing += 1
                continue
            days += 1
            baseline = project_pair(spec, base, quote, config).directional_score
            margins.append(abs(float(baseline)))
            if _sign(baseline) == 0:
                zero_sign += 1
                continue
            for step in steps:
                usd = perturbed_usd(snapshot[Currency.USD], step * sigma)
                shifted = (
                    (usd, quote) if spec.base_currency is Currency.USD else (base, usd)
                )
                moved = project_pair(spec, *shifted, config).directional_score
                if _sign(moved) == -_sign(baseline):
                    flips[step] += 1
        pairs.append(
            PairSensitivity(
                symbol=spec.symbol,
                days=days,
                missing_leg_days=missing,
                zero_sign_days=zero_sign,
                median_abs=statistics.median(margins) if margins else None,
                p10_abs=_p10(margins),
                flips=flips,
            )
        )
    usd_days = sum(1 for snapshot in snapshots if Currency.USD in snapshot)
    return StudyResult(sigma=sigma, usd_days=usd_days, pairs=tuple(pairs))


def _p10(values: list[float]) -> float | None:
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=10, method="inclusive")[0]


def render(result: StudyResult) -> str:
    lines = [
        "USD perturbation sensitivity (design doc v2.1 §12.2)",
        (
            f"sigma(USD directional_score) = {result.sigma:.6f}"
            f" over {result.usd_days} USD days"
        ),
        "",
    ]
    for pair in result.pairs:
        median = "-" if pair.median_abs is None else f"{pair.median_abs:.6f}"
        p10 = "-" if pair.p10_abs is None else f"{pair.p10_abs:.6f}"
        lines.append(
            f"{pair.symbol}  days={pair.days}"
            f" missing_leg={pair.missing_leg_days}"
            f" zero_sign={pair.zero_sign_days}"
            f"  |score| median={median} p10={p10}"
        )
        flippable = pair.days - pair.zero_sign_days
        for step, count in pair.flips.items():
            rate = "-" if flippable == 0 else f"{count / flippable:7.1%}"
            lines.append(f"  k={step:+.2f}σ  flips={count:>4}  ({rate})")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    from trading.config import load_config
    from trading.data.features import StoredFeatureSource
    from trading.intelligence.features import InMemoryFeatureStore
    from trading.intelligence.intervention import InterventionRiskConfig

    parser = argparse.ArgumentParser(description="USD perturbation sensitivity")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--start", help="窓の開始日 (YYYY-MM-DD、UTC)")
    parser.add_argument("--end", help="窓の終了日 (YYYY-MM-DD、UTC、この日を含む)")
    args = parser.parse_args()

    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end
        else datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        if args.start
        else end - timedelta(days=365)
    )
    if start >= end:
        raise SystemExit(f"empty window: {start:%Y-%m-%d} .. {end:%Y-%m-%d}")

    config = load_config(args.env)
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresMacroObservationRepository,
        connect,
    )

    conn = connect(dsn)
    currency_config = CurrencyScoreConfig()
    source = StoredFeatureSource(
        PostgresMacroObservationRepository(conn),
        PostgresEventRepository(conn),
        InterventionRiskConfig(
            version=config.intelligence.intervention_risk.version,
            weights=config.intelligence.intervention_risk.weights,
        ),
        InMemoryFeatureStore(),
        currency=currency_config,
    )

    # 日次グリッド（UTC 00:00 = その日に入った時点の state）。replay と同じ
    # PIT 経路（known_at <= t）で読む。
    snapshots: list[Snapshot] = []
    day = start
    while day <= end:
        snapshots.append(source.currency_snapshot(day))
        day += timedelta(days=1)
    print(
        f"window {start:%Y-%m-%d} .. {end:%Y-%m-%d}"
        f" ({len(snapshots)} daily samples)",
        file=sys.stderr,
    )

    print(render(measure(snapshots, currency_config)))


if __name__ == "__main__":
    main()
