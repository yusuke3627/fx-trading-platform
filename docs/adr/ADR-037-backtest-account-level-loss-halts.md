# ADR-037: backtest 環境では口座水準の損失停止を適用しない

**Status:** Accepted (2026-09-14)

## Context

SYSTEM_SPEC v2.0 [§4.3](../SYSTEM_SPEC.md#s4-3) は、損失制限を JST 暦日（daily）・rolling 24h・high-water-mark drawdown（HWM）の 3 窓で評価すると定める。
初期上限の 0.75% / 1.00% / 3.00% は「初心者向け Micro Live 上限」として `config/base.yaml` に置き、backtest 最適化の対象にしない。
同ファイルのコメントも同じ趣旨だが、`config/backtest.yaml` で上書きしていなかったため、研究リプレイにもこの上限が掛かっていた。

issue #153 は、H4（issue #148）の時間切れ決済 ablation について、2024-08〜2026-08 の 25 か月を測ったところ、有効側は 2025-05 で取引が止まり、残り 15 か月の約定がゼロだったと報告している。
同報告では、棄却理由は `HWM_DRAWDOWN_WITHIN_LIMIT` が 6,376 件で支配的であり、最終資産 970,793 は最高値から 3.007% 下で固定されていた。
これらは issue #153 の報告値であり、本 ADR で元 run を独立に再集計したものではない。

HWM 判定は評価のたびに現在の equity と最高値から計算され、停止状態を保持する実装ではない（`src/trading/risk/engine.py`、`src/trading/risk/limits.py`）。
建玉が残っていれば含み損益の回復で再承認され得るが、建玉のない状態で新規建玉が拒否され続けると資産が動かないため、期間終端まで拒否が続く。

[H5 の研究ノート](../research/2026-09-10-h5-macro-confirmation-ablation.md) にある確認あり／なしの 2 本も、最大ドローダウンが 30,115.26 / 30,250.84 に揃っている。
issue #153 は H5 も同じ閾値で止まったと報告していた。
その後 issue #155 で H5 の run 成果物を月別に数え直した結果、H5 の空白期間は銘柄あたりの建玉上限（`MAX_OPEN_POSITIONS_PER_SYMBOL`）によるもので、`HWM_DRAWDOWN_WITHIN_LIMIT` は確認ありが全期間で 0 件、確認なしが最終月 2026-08 の 99 件だけだった。
最大ドローダウンが 3% を超えていても、空白期間には停止が掛かっていない。
H5 の空白は本 ADR が外す停止によるものではない。
ただし確認なしの最終月には棄却が 99 件あるため、旧閾値が H5 の標本に一切影響していないとまでは言えない。

これらの上限は運用開始のためのものであり、戦略のエッジ測定を打ち切るために置いたものではない。研究リプレイへの適用はその意図とずれている。
SYSTEM_SPEC は v2.0 で凍結されており、[§1](../SYSTEM_SPEC.md#s1) は発行後の仕様変更を本文改訂ではなく ADR 追加で行うと定める。

## Decision

1. **研究リプレイ（backtest 環境）では口座水準の損失停止を適用しない。**
   `config/backtest.yaml` の `risk` に `daily_loss_halt_pct` / `rolling_24h_loss_halt_pct` / `high_water_mark_drawdown_halt_pct` を `100.00` で置き、実質無効化する。
   戦略のエッジそのものを測り、実運用のリスク上限下でどうなるかはエッジが確認できてから別に測る。
   損失率の計算と比較は残るため、正の基準資産に対して equity がゼロ以下になると損失率が 100% 以上となり、`< 100` を満たさず拒否する。
   `100.00` は無効化のための便宜値であり、リスク許容度や完全な無効化フラグではない。

2. **適用範囲は口座水準の損失停止（daily / rolling 24h / HWM drawdown）の 3 つだけとする。**
   銘柄あたり建玉上限（`max_open_positions_per_symbol`）・portfolio 建玉上限、銘柄の数量上限、1 取引あたりのリスク比率、portfolio stop-risk・通貨 exposure の上限は維持する。
   最小ロット超過（`MINIMUM_BROKER_SIZE_EXCEEDS_RISK`）、イベントモード、spread gate、session gate も研究で掛けたままにする。
   戦略の性質と執行可能性を保つため、これらは今回の緩和対象に含めない。

3. **`RiskEngine` / `limits.py` のロジックは変えない。**
   設定値だけで対応する。
   `base.yaml` の 3 値と shadow / demo / micro_live / production の各 overlay も変えず、これらの環境では 0.75% / 1.00% / 3.00% を維持する。

4. **終端の歯止めは既存の sizing と数量制約に委ねる。**
   sizing は equity 比なので、資産が減ると許容数量が縮む（`src/trading/risk/engine.py`）。
   PortfolioManager の刻み丸めで数量が 0 以下なら intent を作らず（`src/trading/portfolio/manager.py`）、intent が無ければ backtest engine は RiskEngine を呼ばないため、この経路では最小ロットの拒否コードも記録されない。
   RiskEngine に届いた注文でも、許容数量が broker 最小数量（USDJPY は 1,000 通貨）未満なら `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` で拒否する。
   どちらの経路でも、損失停止を外したまま無制限に建て続けることはない。
   ただし判定は注文ごとで、同じ equity でも stop 距離やイベントモードが変われば発注可能になる。単一の資産額で永久停止する保証ではない。

5. **[§4.3](../SYSTEM_SPEC.md#s4-3) の「初心者向け Micro Live 上限を backtest 最適化の対象にしない」は維持する。**
   本 ADR は上限を研究用の別の値へ最適化するのではなく、研究の測定対象から外す。
   live 系 overlay の値は変わらない。

## Consequences

- 旧閾値による打ち切りを含むのは H4 の 2 腕で、有効側は 2025-05 以降の 15 か月、無効側は 2026-05 以降が該当する。H5 の空白は銘柄あたりの建玉上限によるもので、旧閾値による打ち切りではない。ただし閾値が変わった以上、本 ADR より前の研究 run は H5 を含めて本 ADR 以降の run と直接比較しない。比較する場合は両腕を修正後の同じ設定で流し直す。
- 研究リプレイの成績は「口座水準の損失停止を掛けず、既存の sizing・執行制約を維持した条件のエッジ」を示す。実運用の上限下での成績、停止の発生頻度、停止後の機会損失は、エッジが確認できた戦略について別に測る。
- `100.00` は各窓の基準資産に対する閾値であり、初期資金からの累積損失率に一律に掛かる値ではない。ゼロ以下の資産や他の gate を迂回する値でもない。本決定を追加の無効化フラグや RiskEngine の分岐変更に広げない。
- 研究ノート（`docs/research/`）の追記は別 issue で行い、本 ADR では変更しない。
