# ADR-029: Portfolio Arbitrator が同時 signal の選択を所有する

**Status:** Accepted (2026-09-02)

## Context

複数 Strategy が同じ cycle で候補を生成すると、共通する通貨 factor への偏りや
triangle exposure が、個々の signal と Risk の pair 単位の評価だけでは制御できない。
一方で、pair gate、口座通貨換算、既存 portfolio、通貨 exposure、position 上限は
既に Risk が所有しており、別サービスで同じ limit 計算を複製すると判定が分岐する。

Strategy は raw candidate まで、Portfolio Manager は size までを担当する。sized
candidate のうちどれを Risk が grade するかを、入力順序に依存せず決める責務が必要である。

## Decision

Portfolio Arbitrator を size と grade の間に置き、同時 signal の選択を所有させる。
Risk は置き換えず、受理済み候補を含む book から従来の risk context を作って順に
`RiskEngine.evaluate` を呼ぶ。これにより、Risk の既存 limit を使った greedy 再評価を行う。

期限切れと取引不可の候補を先に除外し、残りを次の priority の降順で評価する。

`expected_edge_r × confidence − existing_exposure_penalty_r × 既存 book と同方向の leg 数`

同順位は `strategy_id`、`symbol`、`signal_id` の順で決め、入力順序に依存させない。
`expected_edge_r` は `StrategySignal` に持たせ、Strategy が推定を持つまでは既定 1R とする。
`expires_at` は signal の生成時刻と expected horizon から導出する。

重複 factor の初期 policy は strongest signal wins とする。factor は
`(currency, direction)` の leg で表し、先に受理された候補と同じ leg を持つ候補を退ける。
risk budget split は OOS、portfolio backtest、Monte Carlo で改善が確認された後に限り採用する。

triangle は pair 名ではなく `InstrumentSpec` の base / quote 通貨から導出する。既存 book、
当 cycle の受理済み候補、評価中の候補が持つ distinct symbol 数が
`max_pairs_per_triangle` を超える候補を退ける。`existing_exposure_penalty_r` と
`max_pairs_per_triangle` は config に固定し、backtest で校正する。現在値は仮置きであり、
LLM や runtime は変更しない。

Exit は risk reducing order なので裁定を経ない。shadow では、USDJPY 以外も証拠収集の
対象に残すため、Arbitrator へ渡す `trading_enabled` を as-if 有効にする。実際の instrument
policy は従来どおり Risk が報告する。

greedy 再評価の book へ加える基準は Risk の承認ではなく Arbitrator の受理とする。
shadow は global の `risk.trading_enabled=false` により全候補が Risk reject になるため、
Risk 承認を基準にすると再評価を観測できないからである。

## Consequences

- 同時 signal の選択は決定論的になり、入力順序で winner が変わらない。
- live では rank 1 が Risk に退けられた cycle でも、rank 2 は rank 1 を含む book で評価される。
  実際より厳しい fail-close 側の判定になる。
- 単一銘柄の backtest engine には Arbitrator を配線しない。
- rolling correlation に基づく dynamic redundancy は returns series の provider が無いため
  実装せず、既存 book との構造的 overlap penalty で代替する。
- OMS priority queue は別の変更で扱う。
- 裁定結果は `arbitration_decisions` に受理・却下とも記録する。`recent()` は従来どおり
  risk decision 起点なので、Risk に届かなかった却下候補は返さない。
