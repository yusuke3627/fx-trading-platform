"""Does the market's rate pricing predict USD/JPY where the meeting score did not?

    python -m trading.backtest.rate_differential_study --env backtest

E′（policy_event_study）は会合ごとの階段スコアを測り、USD/JPY への予測力を
確認できなかった。原因仮説は「会合テキストを次回会合まで固定するスコアは、
市場の織り込み直し（最大7週間）を見落とす」。ここでは測り方を市場の織り込み
そのもの — 日米2年金利差 D_t = US2Y − JP2Y — に替え、その変化
ΔD = D_t − D_(t−20営業日) が先の USD/JPY を語るかを測る。観測は日次なので、
会合が年16回しかない標本数問題も解消される。

**PIT 整列。** US2Y は ALFRED vintage の known_at（vintage 日 18:00 ET）、
JP2Y は MOF 公表 bound の known_at（次の基準日 15:00 JST、ADR-026）を持ち、
日足バー t の close（17:00 ET）で見えるのはどちらも通常 t−1 の値。両系列が
対称に1営業日遅れるだけで、close より未来の known_at を持つ値は決して使わ
ない。

**窓は重ならないものが正。** 日次観測の 20 日先リターンは隣の観測と 19 日を
共有する。E′ と同じ thin で各 horizon の非重複部分集合を正とし、全サンプル
（重複窓）の回帰傾きは参考値として併記する。

**符号の読み方。** 収束テーゼは「D の縮小の後に USD/JPY が下がる」なので、
リターンの ΔD への回帰傾きは**正**が仮説整合。E′ の divergence slope
（BOJ−Fed スコア、負が仮説整合）とは逆向きなので、両スタディの傾きの符号を
直接比べないこと。

日足は E′ と同じく保存 tick から畳む（market_bars の live 系列は読まない）。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from trading.backtest.policy_event_study import (
    BOOTSTRAP_SEED,
    BROKER_CLOCK_MARGIN,
    EPOCH,
    HORIZONS,
    SYMBOL,
    Observation,
    _row,
    divergence_slope,
    fold_daily,
    gaps,
    irregular_steps,
    measured_span,
    summarize,
    thin,
    unconditional,
    window_outcome,
)
from trading.backtest.research import broker_label_to_known
from trading.data.macro.registry import JP_JGB_2Y_YIELD, US_TREASURY_2Y_YIELD
from trading.domain.economic import EconomicObservation
from trading.domain.market import Bar

# ΔD の観測窓（営業日）。E′ の最長 horizon および既存 rates feature の
# ZSCORE_WINDOW と同じ長さ。フラグにしないのは、窓長を振って良さそうな値を
# 探すことが、このスタディの目的（前提の検証）と両立しないため。
LOOKBACK = 20

WIDENING = "ΔD > 0 (widening)"
NARROWING = "ΔD < 0 (narrowing)"
FLAT = "ΔD = 0"
GROUPS = (WIDENING, NARROWING, FLAT)
QUINTILES = 5


def visible_levels(
    vintages: Sequence[EconomicObservation], instants: Sequence[datetime]
) -> list[float | None]:
    """各時点で見えている最新水準。

    「最新」は二重: 見えている vintage のうち最も新しい基準日を採り、その
    基準日については known_at が最新の vintage を採る（改定が上書きする）。
    古い基準日への遅れて届いた改定は水準を動かさない。vintages は known_at
    昇順（repository の契約）、instants は昇順であること。
    """
    levels: list[float | None] = []
    index = 0
    period = ""
    value: float | None = None
    for instant in instants:
        while index < len(vintages) and vintages[index].known_at <= instant:
            vintage = vintages[index]
            # ISO 日付文字列は辞書順 = 日付順（daily 系列のみ渡すこと）。
            if vintage.observation_period >= period:
                period = vintage.observation_period
                value = float(vintage.value)
            index += 1
        levels.append(value)
    return levels


def differential(
    us2y: Sequence[float | None], jp2y: Sequence[float | None]
) -> list[float | None]:
    return [
        None if us is None or jp is None else us - jp
        for us, jp in zip(us2y, jp2y, strict=True)
    ]


def deltas(bars: Sequence[Bar], series: Sequence[float | None]) -> list[float | None]:
    """バー i ごとの ΔD = D_i − D_(i−LOOKBACK)。

    端点のどちらかが欠けるか、lookback 窓がアーカイブの穴（gaps）を跨ぐ
    バーは None: バー数で 20 日でも時間では 20 日でない窓は変化量ではない。
    """
    values: list[float | None] = [None] * len(bars)
    for index in range(LOOKBACK, len(bars)):
        now, then = series[index], series[index - LOOKBACK]
        if now is None or then is None:
            continue
        if gaps(bars[index - LOOKBACK : index + 1]):
            continue
        values[index] = now - then
    return values


def classify_delta(delta: float) -> str:
    if delta > 0:
        return WIDENING
    if delta < 0:
        return NARROWING
    return FLAT


def build_observations(
    bars: Sequence[Bar],
    delta_by_bar: Sequence[float | None],
    close_instants: Sequence[datetime],
) -> list[Observation]:
    """ΔD が定義できた各バーを1観測にする。

    Observation は E′ と共有: group は符号グループ、divergence は ΔD、
    intervention はこのスタディでは分けないので False 固定。
    """
    observations: list[Observation] = []
    for index, delta in enumerate(delta_by_bar):
        if delta is None:
            continue
        outcomes = {
            horizon: outcome
            for horizon in HORIZONS
            if (outcome := window_outcome(bars, index, horizon)) is not None
        }
        if not outcomes:
            continue
        observations.append(
            Observation(
                at=close_instants[index],
                entry_index=index,
                group=classify_delta(delta),
                divergence=delta,
                intervention=False,
                returns={h: o[0] for h, o in outcomes.items()},
                adverse={h: o[1] for h, o in outcomes.items()},
                favorable={h: o[2] for h, o in outcomes.items()},
            )
        )
    return observations


def quintiles(kept: Sequence[Observation]) -> list[tuple[str, list[Observation]]]:
    """ΔD の五分位。Q1 が最も縮小側、Q5 が最も拡大側。

    thin 後の標本に掛けるため n は小さい: 行の n を見て読むこと。端数は
    境界の丸めで前後のバケットに寄る。
    """
    ordered = sorted(kept, key=lambda o: o.divergence)
    count = len(ordered)
    buckets: list[tuple[str, list[Observation]]] = []
    for q in range(QUINTILES):
        lower = round(q * count / QUINTILES)
        upper = round((q + 1) * count / QUINTILES)
        buckets.append((f"Q{q + 1}", ordered[lower:upper]))
    return buckets


def report(observations: Sequence[Observation], bars: Sequence[Bar]) -> str:
    lines = [
        (
            f"{len(observations)} daily ΔD observations with forward data, "
            f"{len(bars)} daily bars"
        ),
        (
            "D = US2Y - JP2Y in percent points, "
            f"ΔD = D_t - D_(t-{LOOKBACK} trading days)"
        ),
        (
            "convergence thesis: a narrowing D precedes a lower USD/JPY "
            "-> POSITIVE slope of return on ΔD (opposite sign to E's score slope)"
        ),
        (
            "return/median/adverse/favourable in %, negative = yen "
            "appreciation = short USD/JPY wins"
        ),
        "hit = share of windows that ended lower; CI = 90% bootstrap of the mean",
        "Q1..Q5 = quintiles of ΔD over the thinned sample (Q1 most narrowing)",
        (
            "unconditional covers the stretch the observations reach over, not "
            "the whole series"
        ),
        "",
    ]
    holes = gaps(bars)
    for before, after in irregular_steps(bars):
        skipped = (after.start - before.start).days - 1
        kind = (
            "missing data, windows crossing it are not measured"
            if (before, after) in holes
            else "market closed, windows spanning it are still trading days"
        )
        lines.append(
            f"{before.start.date()} -> {after.start.date()} "
            f"({skipped}d): {kind}"
        )
    lines.append("")

    for horizon in HORIZONS:
        seed = BOOTSTRAP_SEED + horizon
        lines.append(f"horizon {horizon} trading days (non-overlapping)")
        lines.append(
            f"  {'':<22} {'n':>6} {'mean':>8} {'median':>8} {'hit':>6} "
            f"{'adverse':>7} {'favour':>7}  CI90"
        )
        kept = thin(observations, horizon)
        for group in GROUPS:
            lines.append(
                _row(
                    group,
                    summarize([o for o in kept if o.group == group], horizon, seed),
                )
            )
        span = measured_span(kept, bars, horizon)
        lines.append(_row("unconditional", unconditional(span, horizon, seed)))
        for label, bucket in quintiles(kept):
            lines.append(_row(label, summarize(bucket, horizon, seed)))
        overlapping = [o for o in observations if horizon in o.returns]
        lines.append(
            f"  slope (thinned):     {divergence_slope(kept, horizon) * 100:>+8.3f} % per pp"
        )
        lines.append(
            f"  slope (all windows): {divergence_slope(overlapping, horizon) * 100:>+8.3f}"
            " % per pp  [overlapping, reference only]"
        )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Rate differential event study")
    parser.add_argument("--env", default="backtest")
    parser.add_argument("--symbol", default=SYMBOL)
    args = parser.parse_args()

    config = load_config(args.env)
    if args.symbol != SYMBOL:
        # 別ペアの日足を日米金利差でグループ分けして「円高」と読むことになる。
        raise SystemExit(f"this study is about {SYMBOL}, not {args.symbol}")
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    from trading.storage.postgres import (
        PostgresMacroObservationRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(dsn)
    now = datetime.now(UTC)
    bars = fold_daily(
        PostgresMarketTickRepository(conn).stream_between(
            args.symbol, EPOCH, now + BROKER_CLOCK_MARGIN
        ),
        args.symbol,
        sys.stderr,
    )
    if not bars:
        raise SystemExit(f"no stored quotes for {args.symbol}")

    observation_repo = PostgresMacroObservationRepository(conn)
    vintages = {
        series: observation_repo.known_before(series, now, EPOCH)
        for series in (US_TREASURY_2Y_YIELD, JP_JGB_2Y_YIELD)
    }
    for series, stored in vintages.items():
        if not stored:
            raise SystemExit(
                f"no {series} vintages stored — run the collector first "
                "(trading.data.macro.collector)"
            )

    anchor = timedelta(hours=config.market.broker_server_ahead_of_ny_hours)
    close_instants = [broker_label_to_known(bar.close_time, anchor) for bar in bars]
    series = differential(
        visible_levels(vintages[US_TREASURY_2Y_YIELD], close_instants),
        visible_levels(vintages[JP_JGB_2Y_YIELD], close_instants),
    )
    observations = build_observations(bars, deltas(bars, series), close_instants)
    if not observations:
        raise SystemExit(
            "no ΔD observations: the rate series and the price archive do not "
            "overlap far enough"
        )
    print(report(observations, bars))


if __name__ == "__main__":
    main()
