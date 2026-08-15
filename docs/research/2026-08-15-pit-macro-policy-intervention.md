# PIT マクロデータ・政策期待・介入・バックテスト設計 — 2026-08-15

**これは SYSTEM_SPEC ではなく Research Note。** 将来の設計へ固定しない。
実装に着手する際、設計へ固定する決定は `docs/adr/` へ ADR として起こす。

出典: deep-research 最終リサーチ（2026-08-15 受領）。マクロ PIT データ調達・政策期待
Proxy・介入 Dataset・Walk Forward 手法の調査結果を蒸留したもの。本文中の金額・
料金・公表予定などの具体値は 2026-08-15 時点の取得値。検証用の一次資料は末尾の
「一次資料」節を参照。

## 結論サマリー（データ調達の優先順位）

| 調査項目 | 結論 | 実装判断 |
|---|---|---|
| Economic Release PIT | Actual/Revised は無料で高品質に構築可能。Consensus だけが有料の壁 | BLS/BEA/Census + ALFRED を先に実装 |
| 無料 Consensus | 長期・PIT・API・ライセンス明確を満たす無料の正本は存在しない | 今後の Consensus を自前蓄積、過去分は後日有料 PoC |
| Trading Economics | PIT Calendar として有力だが最初から契約不要（Standard $199/月） | Collector interface だけ作り、後日 1 週間 trial で検証 |
| Fed 期待 Proxy | 米2年債変化は無料 Proxy として使えるが FedWatch の代用品ではない | `US2Y_CHANGE_*` をまず実装 |
| BOJ 期待 Proxy | 公式文書からの hawkish/dovish shift が無料版の主軸 | `BOJ_POLICY_SHIFT_SCORE` を機械ルールで実装 |
| FedWatch | EOD $25/月で 2015 年以降が取れる。費用対効果が最も高い有料候補 | Swing の Edge 検証が始まったら最初に買う |
| 介入 Dataset | MOF 公式値は無料。ただし「その瞬間の市場認識」は別 Dataset | Official と Rumor/Reported を分離 |
| 過去 Spread | OANDA の 2022/24 実 spread を無料で完全復元できる保証はない | 今日から Tick を永久保存（collector 稼働で対応済み） |
| OANDA Swap | 東京サーバーは 2020-05-02 以降の履歴を公式提供 | Backtest に実 swap calendar を利用 |
| Walk Forward | 政策レジームでデータを恣意的に分割してはいけない | chronological WF + regime 別の事後評価 |

## 設計決定（実装時に ADR 化する候補）

1. **Consensus 欠損は `UNKNOWN`。0 を入れない。**
   `if consensus is None: surprise = 0.0` は「予想通りだった」という偽情報を作る。
   Macro Gate は POSITIVE / NEGATIVE / UNKNOWN の三値論理にし、UNKNOWN 時の挙動は
   config で選ぶ（`require_surprise_confirmation: true` → NO_TRADE、または
   `allow_missing_surprise: true` → `position_size_multiplier: 0.5`）。
   **欠損とニュートラルを同一視しないことをドメイン不変条件に加える。**
2. **Actual と Consensus の正本を分離する。**
   Actual authority = BLS / BEA / Census（+ ALFRED で vintage 照合）。
   Consensus authority = Trading Economics 等の Market Survey Provider。
   Trading Economics を Actual の正本にしない。
3. **Revision は UPDATE しない。** `EconomicReleaseEvent`（first print）とは別に
   `EconomicRevisionEvent` を追加する。ReplayClock が revision の `known_at` より
   前なら first print のみが見える。GDP は Advance/Second/Third の各 vintage を
   別イベントとして保存する（BEA 実績: Advance→Third の平均絶対改定 ≈ 0.6pt）。
4. **介入 Dataset は認識レイヤーを分離する。**
   MARKET_SUSPECTED → REPORTED → GOVERNMENT_CONFIRMED → OFFICIAL_MONTHLY_AMOUNT →
   OFFICIAL_DAILY_AMOUNT の順で「市場が知った時刻」ごとに別イベント。
   `reported_estimate_jpy` と `official_amount_jpy` を同じカラムにしない。
5. **存在しない情報を暗黙補完する feature 名を使わない。**
   日本側 2 年金利を安定取得できるまで `rate_diff_change_5d` は作らない。
   初期版は `us2y_change_5d` と `boj_policy_shift_score` に分け、JP2Y 接続後に
   `rate_diff_change_5d = Δ5d(US2Y - JP2Y)` を導入する。同じ理由で
   `FED_EXPECTATION` ではなく `US2Y_POLICY_PROXY` と命名する（2 年債には term
   premium・成長期待・インフレ期待・リスクプレミアムが混在するため）。
6. **Research label ≠ Live feature。** 「2022 = Fed tightening」のような事後
   ラベルを Live/Replay に入力しない。Live regime は当時情報だけの機械的関数
   （`fed_last_action` / `fed_guidance_score` / `us2y_change_20d` 等）で算出する。
   `intervention_risk` は連続値関数にし、アルゴリズムを `intervention_risk_v1` の
   ようにバージョン管理して過去 Backtest を新ルールで書き換えない。

## データモデル（実装時の出発点）

```text
EconomicReleaseEvent
  indicator / reference_period
  scheduled_at / published_at / retrieved_at / known_at
  actual_first_print
  previous_pre_release / previous_revised_at_release
  consensus / consensus_snapshot_at            # 欠損可（UNKNOWN）
  source_agency / source_uri / payload_hash

EconomicRevisionEvent
  対象 release / known_at / old_value / new_value

consensus_snapshots                             # 今日から forward 蓄積
  release_id / provider / snapshot_at
  forecast / median / high / low / respondent_count
  raw_uri / payload_hash

InterventionEvent                               # 認識段階ごとに別イベント。
  event_id                                      # 1 レコードへ集約しない。
  intervention_id?                              # 日次スコープの kind のみ持つ
                                                # （同一介入の相関キー）
  kind                                          # MARKET_SUSPECTED / REPORTED /
                                                # GOVERNMENT_CONFIRMED /
                                                # OFFICIAL_DAILY_AMOUNT /
                                                # OFFICIAL_MONTHLY_AMOUNT
  known_at                                      # その段階を市場が知った時刻
  source_uri / retrieved_at / payload_hash
  kind 別 payload:
    MARKET_SUSPECTED        → suspected_start_at
    REPORTED                → published_at / updated_at / reported_estimate_jpy
                              # 初報時刻の保全のため published/updated を別管理
    OFFICIAL_DAILY_AMOUNT   → official_action_date / official_amount_jpy
    OFFICIAL_MONTHLY_AMOUNT → period_start / period_end / official_total_jpy
                              # 公表対象期間全体の集計値。期間内に複数介入が
                              # あり得るため intervention_id を持たせず、単一の
                              # 介入日に紐付けない。日次内訳は後日の四半期公表
                              # （OFFICIAL_DAILY_AMOUNT）で相関させる
  # 日次スコープのイベントは intervention_id で相関。Replay は
  # known_at <= ReplayClock のイベントだけを見る。後段の official 値で
  # 過去イベントを UPDATE しない
```

Surprise の定義: `actual_first_print - consensus` を指標ごとの過去 forecast error
で標準化（z-score）。rolling window は `known_at < 対象 release` のデータのみ。
共通 feature `US_DATA_SURPRISE` へ集約する際は、指標ごとの方向マップ
（`direction_map[indicator] ∈ {+1, -1}`）を z-score に掛けて
「+ = hawkish / USD 支持方向」へ符号を正規化する（CPI・NFP は actual >
consensus が hawkish だが、失業率や新規失業保険申請は actual > consensus が
dovish であり、生の符号のまま混ぜると意味が反転するため）。
`headline_surprise` と `revision_surprise`（前月値改定）は別 feature にする
（NFP のように前月改定が市場反応を左右する指標があるため）。

## 無料データソースの正本

- **CPI/雇用統計** → BLS、**GDP/PCE** → BEA、**Retail Sales** → Census。
  現在の API から過去値を取ると改定後の値になる（BLS は archive 内の値が改定
  済みの可能性を明記、CPI 季節調整は毎年過去 5 年分を再計算）ため、当時の
  known 値には公式 News Release アーカイブを使う。
- **ALFRED**（St. Louis Fed）が vintage 照合の基盤。`realtime_start` /
  `vintage_dates` で「そのデータがいつ利用可能だったか」を取れる。ただし
  イベント駆動 backtest に必要な秒単位 `known_at` は公式リリースの公表時刻と
  組み合わせて構成する。
- **US2Y** → 米財務省 Daily Treasury Par Yield Curve Rates（無料・公式）。
  `US2Y_LEVEL / US2Y_CHANGE_1D / US2Y_CHANGE_5D / US2Y_ZSCORE_20D` を作る。
- **BOJ** → 公式文書（決定・Statement・Outlook・Summary of Opinions・Minutes）
  から `BOJ_POLICY_SHIFT_SCORE` をイベント駆動で算出。解釈スケールは
  -2 = strongly dovish 〜 +2 = strongly hawkish。LLM の主観で点数を付けず、
  初期は機械ルールの加算とし、**合計を [-2, +2] へ clip して最終スコアにする**
  （利上げ + hawkish dissent 等が重なるとルール合計は +2 を超えるため）:

  ```yaml
  boj_policy_scoring:
    rate_hike_25bp: +2.0
    rate_cut_25bp: -2.0
    hawkish_dissent: +0.5
    dovish_dissent: -0.5
    inflation_forecast_upgrade: +0.5
    inflation_forecast_downgrade: -0.5
    explicit_future_hike_language: +0.5
  ```

- **OANDA Swap** → 東京サーバー公式 Swap Point Calendar（2020-05-02 以降
  ダウンロード可）。Backtest は `symbol_info().swap_long` の現在値を過去に
  適用せず、Replay 日付ごとに実 calendar 値を使う。
- **Demo を calibration に使わない。** OANDA 公式が Demo のレート・スプレッド・
  スワップは本番と異なると明記。Demo の目的は API/OMS/Reconciliation/SL の
  正しさ検証のみ。spread 分布は Micro Live 後の `live_spread_samples` で較正。
  Demo は原則 30 日期限のため、CI/CD を永続 Demo 口座に依存させない
  （mock + local simulator + MT5 demo preflight の三層）。

## 有料データの導入順位と Ablation

導入順: **1) FedWatch EOD（$25/月、2015〜） 2) Historical Consensus
（Trading Economics trial で検証） 3) Premium news**。

契約判断は Ablation で行う:

```text
Model A: Price only
Model B: Price + US2Y
Model C: Price + actual direction（consensus なし）
Model D: Price + true consensus surprise（trial データ）
```

D を検証する段階で初めて Trading Economics の短期 trial を使い、主要指標
（CPI/Core CPI/NFP/失業率/AHE/PPI/Retail Sales/GDP/PCE/Core PCE/ISM）について
historical consensus の PIT 粒度（日次 snapshot か、発表直前値まで再現できるか）
を実データで検査してから契約する。FedWatch 導入後は絶対値でなく変化量
（`ΔP(next meeting +25bp)` の 1d/5d 変化等）を feature にし、OOS で Proxy
（US2Y_CHANGE_5D）に勝てなければ解約する。

## 介入の確定値（Official Layer の初期データ）

MOF 公式（1991 年以降 CSV 公開）。円買い・ドル売り介入:

| 実施日 | 確定額 |
|---|---:|
| 2022-09-22 | 約 2.8382 兆円 |
| 2022-10-21 | 約 5.6202 兆円 |
| 2022-10-24 | 約 0.7296 兆円 |
| 2024-04-29 | 約 5.9185 兆円 |
| 2024-05-01 | 約 3.8700 兆円 |
| 2024-07-11 | 約 3.1678 兆円 |
| 2024-07-12 | 約 2.3670 兆円 |

これらは `action_date` と `official_amount_known_at`（月次/四半期公表日）を分離
して保存する。当時の市場認識（何時何分に「介入らしい」と認識されたか）は公式
統計から復元できないため、Reuters/共同/日経等の**当時公開時刻**を REPORTED
レイヤーとして別途構築する。

**2026-07-30 の介入**（約 164 → 157 円台、報道 → 公的確認）は現時点で
GOVERNMENT_CONFIRMED / REPORTED まで。正式月次総額（対象 7/30〜8/26）は
**2026-08-28 19:00 JST に MOF 公表予定**。公表時は過去イベントの UPDATE では
なく `OFFICIAL_MONTHLY_AMOUNT` イベントを新規追加する。この発表は予定された
過去期間の確定情報なので FOMC 型の ±90min blackout は不要。
`HALT_NEW_ORDER 5min + regime 再計算` 程度で足りる。

## Walk Forward と Regime 評価

- **正本は時間順 Walk Forward。** regime を知った上で train/test を切る
  （例: 2022 を train、2024 を test）と選択バイアスを持ち込む。
  初期案: Train 36m / Validation 6m / Test 6m / Step 6m の rolling。
- **OOS 結果を regime 別に事後スライス**して分析する（Fed tightening / easing /
  BOJ normalization / intervention high / dual central bank）。
- 介入日は `OOS_INTERVENTION_STRESS` として通常 OOS と別評価。Acceptance 例:

  ```yaml
  acceptance:
    overall_oos_expectancy: { min: 0 }
    intervention_stress: { max_single_event_loss_r: 2.0 }
    no_bankruptcy_in_stress: true
    no_unbounded_loss: true
    broker_stop_required: true
  ```

  「通常時 Sharpe 1.8 だが介入 3 回で全利益を失う」は Reject。
- 研究用の事後 regime ラベル（Live には入れない）:

  | 期間 | Fed | BOJ | 介入リスク |
  |---|---|---|---|
  | 2016–2018 | Tightening | Ultra-easy/YCC | Low |
  | 2019 | Easing | Ultra-easy | Low |
  | 2020–early 2022 | ZLB/Easy | Ultra-easy | Low→Medium |
  | Mar–Dec 2022 | Aggressive tightening | Ultra-easy/YCC | High (Sep–Oct) |
  | 2023 | Tight/Hold | YCC flexibility | Medium |
  | Jan–Mar 2024 | Hold | NIRP/YCC → Exit | High |
  | Apr–Jul 2024 | Hold | Normalizing | Very High |
  | Sep–Dec 2024 | Easing | Normalizing | Medium |
  | 2025 | Hold→Easing | Normalizing | variable |
  | 2026（〜8/15） | Hold + hawkish dissent | 約 1.0% normalization | High（介入後） |

## Blackout 初期値（Strategy parameter として WF で評価する）

「最適値」ではなく安全側 default。6h/12h/24h 等を Backtest で比較する。

- **Intraday（Tier-1 統計）**: T-30m 新規停止 → 発表 → T+5〜15m で
  POST_EVENT 戦略が有効化。Intraday 本命の `POST_EVENT_FAILED_BREAKOUT` は
  発表瞬間を当てる必要がなく、spike 後の market structure 形成を待つ設計。
- **FOMC 単体**: Statement T-90m 停止 → T+120m 再開。Press Conference が
  終わるまで解除しない。
- **BOJ 単体**: 発表時刻が固定されないため 2 日目 09:00 JST から停止、
  Statement 検知 +90m で再開。
- **Dual Central Bank Cluster**:

  ```yaml
  dual_central_bank_cluster:
    enabled: true
    new_entry_blackout:
      start: { event: first_decision, offset_minutes: -90 }
      end: { event: second_decision, offset_minutes: 90 }
    allow_position_carry_between_events: false
  ```

- **Swing**: first decision の 24h 前から新規停止、cluster 内は size ×0.5、
  second decision +6h で再開。

## 直近の Research Prior 更新（2026-08-13 snapshot からの差分）

- **「政策差縮小一辺倒」priors は単純化しすぎ。** 7/29 FOMC は 3.50–3.75%
  据え置きだが 9 対 3 で 3 名が **25bp 利上げ**を主張（hawkish dissent）。
  BOJ は約 1.0% で normalization 継続。つまり両中銀とも hawkish 方向へ動き得る
  環境で、Swing は BOJ hawkish 単独で Short せず
  `BOJ hawkish shift − Fed hawkish shift` の**相対変化**を見る。
- 市場の 9 月利上げ確率は 8/14 時点で約 31%（弱い 7 月小売売上高後）。
  この種の値は絶対値でなく**変化量**を feature として保存する
  （level = hawkish 確率が残存、change = dovish repricing は別情報）。
- **9 月は正式に dual central bank cluster**: FOMC Statement 2026-09-17 03:00
  JST（Press 03:30）→ BOJ 決定 9/17–18（時刻非固定）。
  `dual_central_bank_cluster = true`。

## 次の実装順（Phase A〜D）

```text
Phase A  economic/   bls_collector / bea_collector / census_collector /
                     alfred_collector / models
                     → EconomicReleaseEvent / EconomicRevisionEvent
Phase B  policy/     treasury_yield_collector / fed_statement_collector /
                     boj_statement_collector / policy_features
                     → US2Y_CHANGE_1D/5D / FED_GUIDANCE_SHIFT / BOJ_POLICY_SHIFT_SCORE
Phase C  intervention/ mof_collector / intervention_events / intervention_risk
Phase D  Intraday Strategy = Price + US2Y proxy + BOJ shift
                     + US_DATA_SURPRISE(if available)
```

Phase A〜D は `US_DATA_SURPRISE = UNKNOWN` を正式に許容した状態で全体を動かし、
並行して consensus_snapshots の forward 蓄積を今日から始める。この構造なら
無料データだけで始めても、FedWatch・Historical Consensus・Premium News を後から
追加したときに backtest 基盤を作り直す必要がない。

## Preflight への追加項目（issue #14 へ反映）

- tick flags 分布・`last != 0` / `volume != 0` 比率の実測（LAST/BUY/SELL を
  aggressor side と解釈して Order Flow feature に使うのは実測後に判断）
- tick history depth の自動測定（today−1d/30d/1y/2y/5y を順に照会して
  `earliest_available_tick_at` を保存。2024 介入 Tick を OANDA MT5 だけで
  検証できるかを憶測でなく確定させる）
- Preflight 結果は表示して終わりにせず DB へ保存する（margin_mode / digits /
  volume_min/step/max / trade_stops_level / filling_mode / rollover 挙動 /
  swap 実付与と公式 calendar の一致 / latency p50-p99 / session 別 spread 分布）

## 一次資料（検証用）

本 Note の具体値（データ公開範囲・料金・介入額・公表予定）は 2026-08-15 時点の
取得値であり、実装・契約の判断時には以下の一次資料で再確認する。

- **BLS**（bls.gov）: 公式 News Release アーカイブ（archive 内の値は後日改定
  され得る旨の注記、CPI 季節調整の年次再計算を含む）
- **BEA**（bea.gov）: GDP の Advance/Second/Third estimate と改定幅の公式研究
- **U.S. Census Bureau**（census.gov）: Advance Monthly Retail Trade と
  公式リリース日程
- **ALFRED / FRED API**（alfred.stlouisfed.org / fred.stlouisfed.org）:
  `realtime_start` / `realtime_end` / `vintage_dates`
- **米財務省**（home.treasury.gov）: Daily Treasury Par Yield Curve Rates
- **CME Group**（cmegroup.com）: FedWatch Tool と EOD データ（$25/月〜、
  2015 年以降）の料金ページ
- **Trading Economics**（tradingeconomics.com）: Economic Calendar API
  ドキュメントと料金ページ（Standard $199/月）
- **財務省**（mof.go.jp）: 外国為替平衡操作の実施状況（月次総額・四半期
  日次内訳・1991 年以降の CSV、公表予定日時）
- **日本銀行**（boj.or.jp）: 金融政策決定会合の公表資料・2026 年会合日程・
  現行の政策金利ガイドライン
- **FRB**（federalreserve.gov）: FOMC Meeting calendars / Statement・
  政策金利の公式履歴
- **OANDA Japan**（oanda.jp）: 東京サーバー Swap Point Calendar
  （2020-05-02 以降）、MT5 Demo と本番の差異・Demo 期限に関する FAQ
- **MetaQuotes**（mql5.com）: `copy_ticks_range` / `COPY_TICKS_*` /
  `TICK_FLAG_*` の Python API 公式ドキュメント
