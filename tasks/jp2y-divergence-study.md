# B′: 政策乖離の連続測定基盤 — JP 2年金利コレクター + 金利差イベントスタディ CLI

ブランチ: `feat/jp2y-divergence-study`（origin/main = 1de4ae5 起点）

## 背景

swing 戦略 monetary_policy_convergence の前提検証。E′（`policy_event_study.py`、PR #93）
で会合ベースの階段スコアには USDJPY への予測力が確認できなかった。原因仮説は
「会合テキストを次回会合まで固定するスコアは市場の織り込み直しを見落とす」。
測り方を市場の織り込み＝日米2年金利差 D_t = US2Y − JP2Y に替えて再検証する。
日次観測になるため標本数問題（会合は年16回）も解消される。

## MOF CSV 実地調査結果（2026-09-01 実測）

| 項目 | 事実 |
| --- | --- |
| 当月分 URL | `https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv`（実測: 令和8年8月分のみ、R8.8.3〜R8.8.31、約2.2KB） |
| 全期間 URL | `https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv`（S49.9.24〜、実測時点で R8.7.31 まで。約1.1MB、13,272行） |
| 年別ファイル | `data/jgbcm_2025.csv` は 404。当月分＋全期間の2ファイル構成 |
| エンコーディング | Shift_JIS（1行目タイトル「国債金利情報」、ヘッダ行に日本語） |
| 日付形式 | 和暦短縮形（`S49.9.24` / `H1.1.9` / `R1.5.7` / `R8.8.31`）。ゼロ埋めなし。元号は S/H/R の1文字（介入 CSV の「令和8年6月29日」形式とは別物） |
| 列構成 | 1行目タイトル、2行目ヘッダ `基準日,1年,2年,3年,...,40年`（16列）。**2年物は0始まりで index 2**。実装ではヘッダから「2年」を探して位置決めする |
| 欠測 | `-`（2年列に361行。初期年代の長期年限にも多数） |
| 値 | 負値あり（例 `-0.156`）。% 単位 |
| 末尾 | 空行＋「※最新のcsvデータがダウンロードできない場合…」の注記行（当月分） |
| **公表時刻** | **基準日の翌営業日 09:30 頃**（公式 FAQ https://www.mof.go.jp/faq/jgbs/04hf.htm 「翌営業日午前9時30分頃を予定しています。」）。実測でも jgbcm.csv の Last-Modified = 2026-09-01T09:30:43+09:00（内容は 8/31 まで）で一致 |
| 全期間ファイルの更新 | 月次（実測: 7月末までのデータで Last-Modified 2026-08-04T08:30+09:00）。当月分は前月分が翌月頭まで残る（9/1 時点で8月分を配信中） |

**重要な訂正**: タスク指示の「財務省は当日夕方公表」は誤り。公式 FAQ・Last-Modified
実測の両方が「翌営業日 09:30 頃」を示す。known_at 設計はこちらに従う。

## PIT semantics の決定と根拠

- **known_at = その行の「次の基準日」の 15:00 JST（UTC 変換して保存）**。
  - 公表は基準日の翌営業日 09:30 頃。翌営業日の暦は祝日・年末年始で +1日では
    足りず（GW は最大6暦日）、自前の祝日カレンダーは持たない。CSV の基準日列
    そのものが JGB 営業日カレンダーなので、**マージ済み系列の「次の基準日」を
    公表日の保守的な代理**に使う（基準日=取引日と MOF 営業日は実務上同一集合）。
  - 09:30「頃」への余裕として 15:00 JST を採る。USDJPY 日足の close は
    17:00 ET（翌暦日 06〜07:00 JST）なので、翌営業日の 09:30〜24:00 のどの時刻
    を選んでも日足整列は同一。15:00 は公表遅延への margin が目的。
  - 先行例: 介入 daily（`intervention/mof.py` の `daily_known_at`）が同じ
    「公表タイミングの保守的上限を backfill の known_at にする」パターン
    （四半期末+62日 19:00 JST）。
- **最新行（後続基準日が未出現の行）はそのランでは emit しない**。known_at が
  決定論的になり、再実行・両ホスト（Mac/VPS の独立 DB）で同一 vintage になる。
  DB 反映は公表から最大1営業日強遅れるが、live 消費者は現状なく、B′ は履歴
  スタディなので問題ない（ADR に制約として記録）。
- **冪等性**: known_at が決定論的 → 再収集は vintage キー
  `(series, observation_period, known_at)` の ON CONFLICT DO NOTHING と
  repository の同値スキップ（PR #18）で 0 行挿入になる。collector 側に DB 参照は
  不要。
- **改定の扱い**: 過去行の値が後日変わっても、同じ known_at を計算するため
  ON CONFLICT で落ち、**初出値が保持される**。改定の真の公表時刻は CSV から
  知り得ないので、初出値保持が PIT として最も保守的（複利利回りの遡及訂正は
  実務上ほぼ無い）。ADR に明記。
- **pit_classification = PIT_UNVERIFIED**（ソースは最新版の履歴のみ配信）。
  ただし ADR-015 の「backfill に release 時刻の known_at を与えない」規範から
  の逸脱（公表時刻が公式に有界で、履歴が初出値のまま維持される系列に限る
  例外）を **ADR-022 として追加**して合意を取る。pit_classification は現状
  情報表示のみで実行時の消費者はいない（rg で確認済み）。
- **US2Y 側**: 既存 ALFRED 経路（PIT_VERIFIED、known_at = vintage 日 18:00 ET）
  をそのまま読む。変更なし。
- 整列の帰結: 日足バー t の close（17:00 ET）で見えるのは通常 US2Y(t−1)・
  JP2Y(t−1)。両系列とも対称に1営業日ラグし、look-ahead はない。

## 変更対象ファイル（網羅）

| ファイル | 変更 |
| --- | --- |
| `src/trading/data/macro/jgb.py` | **新規**。`JGBYieldCollector`: 2 URL を fetch → Shift_JIS decode → 和暦短縮パース → ヘッダから2年列特定 → 基準日でマージ（衝突時は当月分優先）→ 後続基準日 15:00 JST の known_at → `CollectionBatch`（`EconomicObservation` + raw archive 2件）。境界検証は fail-loud（ヘッダ不一致・未知元号・データ0行・値パース不能で ValueError） |
| `src/trading/data/macro/registry.py` | `JP_JGB_2Y_YIELD = "jp_jgb_2y_yield"` と IndicatorSpec 追加（percent / daily / PIT_UNVERIFIED / release_time 09:30 Asia/Tokyo — 「翌営業日」の注記コメント付き） |
| `src/trading/data/macro/collector.py` | `SOURCES` に `"jgb"` 追加、分岐追加、`--series` 非対応ガードに `"jgb"` 追加（単一系列ソース） |
| `scripts/collect_daily.sh` | for ループに `jgb` 追加（`test_daily_collection_script.py` が SOURCES との一致を強制） |
| `src/trading/backtest/rate_differential_study.py` | **新規**。B′ CLI（下記） |
| `tests/unit/test_jgb_collector.py` | **新規** |
| `tests/unit/test_rate_differential_study.py` | **新規** |
| `docs/adr/ADR-022-jgb-2y-yield-pit-bound.md` | **新規**。上記 PIT 決定の記録 |

**マイグレーション: なし。** 既存の `macro_observations`（vintage キー + 同値
スキップ）と `events`（raw archive、ECONOMIC_RELEASE_RAW）で足りる。

collector は毎ランで全期間ファイル（1.1MB）も取得・アーカイブする。月替わり
直後に当月ファイルだけでは前月末行の後続基準日が得られない穴を塞ぐため、
2ファイル固定が最も単純（バックフィルも「初回実行」に縮退し、専用フラグ不要）。
raw archive の肥大は intervention（全履歴 CSV を毎ラン保存）と同型で許容。

## Part 2: B′ 検証 CLI 設計

`python -m trading.backtest.rate_differential_study --env backtest`
（symbol は E′ 同様 USDJPY 固定ガード）

- **データ**: E′ と同じく保存 tick から `fold_daily` で日足を畳む（market_bars
  不使用）。金利は `PostgresMacroObservationRepository.known_before(series, now,
  EPOCH)` で全 vintage を一括で読み、bar close 時刻
  `broker_label_to_known(bar.close_time, anchor)`（anchor は E′ 同様
  `config.market.broker_server_ahead_of_ny_hours`）までに known な最新値を
  ポインタ走査で整列（known_at 尊重。float 化は統計計算の境界で行う —
  `policy/features.py` の `yield_series` と同じ扱い）。
- **説明変数**: ΔD_t = D_t − D_{t−20}（20 は E′ の horizon 上限・ZSCORE_WINDOW
  と揃えたモジュール定数。CLI フラグにはしない）。lookback 窓が `gaps` の穴を
  跨ぐ観測は除外。
- **グループ**: 符号グループ（ΔD>0 拡大 / ΔD<0 縮小 / ΔD=0）を主表に、
  五分位平均を副表に（thin 後の n が小さいことは n 列で明示）。
- **回帰**: `divergence_slope`（OLS 傾き）を流用。thin 済み非重複標本を正、
  全サンプル（重複窓）回帰は「参考」ラベルで併記。収束テーゼの予測方向は
  「D 縮小 → USDJPY 下落」なので**正の傾き**が仮説整合（E′ と逆符号である旨を
  出力に明記）。
- **窓計測**: horizon 5/10/20 営業日。`window_outcome`（log リターン + 逆行/
  順行）、`thin`（非重複化）、`bootstrap_interval`/`summarize`（CI90）、
  `unconditional` + `measured_span`（無条件ベースライン）、`irregular_steps`/
  `gaps`（欠損の報告と無効化）を **`policy_event_study` から import して共有**
  （E′ 側は一切変更しない）。
- **出力**: E′ と同じプレーンテキスト表（PowerShell から貼り付け可能）。
  行フォーマッタ `_row` も import（同 package の姉妹モジュール間の private
  参照として許容。E′ を触らないことを優先）。
- `Observation` dataclass も流用（group=符号グループ、divergence=ΔD、
  intervention=False 固定。summarize/thin は intervention を読まない）。

## E′ からの部品共有方法（まとめ)

`trading.backtest.policy_event_study` から import:
`fold_daily` / `Observation` / `Stats` / `irregular_steps` / `gaps` /
`window_outcome` / `thin` / `bootstrap_interval` / `summarize` /
`measured_span` / `unconditional` / `divergence_slope` / `_row` /
定数（`SYMBOL` `TIMEFRAME` `HORIZONS` `EPOCH` `BROKER_CLOCK_MARGIN`
`BOOTSTRAP_SEED`）。加えて `trading.backtest.research.broker_label_to_known`。
E′ 専用のもの（`classify` / `entry_bar` / `current_version` / `collapse_same_entry` /
`report`）は使わない。共有モジュールへの抽出は E′ に差分が出るため行わない
（必要になったら follow-up）。

## テスト方針（unit のみ、実在人物名なし）

`tests/unit/test_jgb_collector.py`（fixture は実 CSV の形を模した Shift_JIS
バイト列を合成）:
1. パース: タイトル行・ヘッダ行・注記行・空行を除外し、2年列をヘッダから
   特定して Decimal で読む（列順を変えた fixture でも正しい列を拾う）
2. 和暦変換: S49.9.24 / H1.1.9 / R1.5.7 / R8.8.31 の各境界、未知元号 X1.1.1 は
   ValueError
3. `-` の 2年値はスキップ、ただしその行の基準日は後続日計算には使われる
4. known_at: 次の基準日 15:00 JST（UTC）になる。金曜行→月曜行（週末跨ぎ）で
   known_at が月曜 15:00 JST になる。最終行は emit されない
5. 冪等性: 当月分のみの状態と、翌月に jgbcm_all へ同値が現れた状態の 2 回の
   collect が同一 (series, period, known_at, value) を生む（決定論）
6. マージ: 全期間と当月分の重複日は当月分の値を採る
7. fail-loud: ヘッダ不一致 / データ0行で ValueError
8. registry: `INDICATORS["jp_jgb_2y_yield"]` の spec（PIT_UNVERIFIED 等）
   — `test_daily_collection_script.py` は SOURCES 追加により自動でカバー

`tests/unit/test_rate_differential_study.py`（E′ テストの流儀に合わせ、部品は
再テストせず B′ 固有の組み立てを試験）:
1. PIT 整列: known_at が bar close より後の vintage は見えない（JP2Y の
   1営業日ラグが正しく効く）。改定 vintage は known_at 順で上書き
2. ΔD: 20 bar lookback の差。片系列欠如・lookback 穴跨ぎで観測が落ちる
3. 符号グループ分け・五分位の境界
4. 回帰の符号: 「D 縮小 → 下落」を埋め込んだ合成系列で正の傾きが出る
5. 全サンプル回帰（参考値）と thin 済み回帰が別々に報告される

実行: `ruff check .` と `pytest tests/unit/test_jgb_collector.py
tests/unit/test_rate_differential_study.py tests/unit/test_daily_collection_script.py`
→ 最後に全 `pytest`。

## マージ後の実行手順（Phase 4 で PR 本文に記載するメモ）

```bash
# 両ホスト（Mac / VPS。DB は独立、初回実行がバックフィルを兼ねる）
python -m trading.data.macro.collector --env demo --source jgb
# 以後は日次収集（Mac: collect_daily.sh の cron に自動で乗る。VPS も同コマンド）

# VPS（PowerShell）で B′ 実行
python -m trading.backtest.rate_differential_study --env backtest
```

## リスク・未決事項

- jgbcm.csv の月替わり挙動（9月分がいつ8月分を置き換えるか）は未観測。
  2ファイル併読 + 決定論的 known_at で、どちらに転んでも取りこぼし・重複は
  出ない設計にしている（前月末行は全期間ファイルの月次更新で最終的に埋まる）。
- 価格アーカイブは 2024 年半ば以降（E′ 実測）。B′ の measured_span も同区間に
  縛られる。金利差の履歴自体は 1974 年からあるが、使うのは価格と重なる区間のみ。
- thin 後の h=20 標本は ~25 観測 × グループ分割でかなり薄い。n を常に表示し、
  五分位は副表扱いにする。
