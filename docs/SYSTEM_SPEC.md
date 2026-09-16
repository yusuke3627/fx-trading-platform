# SYSTEM_SPEC: FX Trading Platform

**Status:** Architecture Frozen (v2.0)
**運用主体:** 個人、自己資金運用
**Broker / Execution:** OANDA証券 東京サーバー / MT5
**Platform対象:** USDJPY / EURUSD / GBPUSD / GBPJPY（取引許可は別管理）

<a id="s1"></a>

## 1. 規範と版の切替

本書は v1.3 と採用済み ADR-001〜033 を統合した設計の正本である。
統合の基準となる main は `fb42d83`、統合の承認は ADR-035 に記録する。
この改訂が main に取り込まれた時点を v2.0 の発行とする。
それまでは main の v1.3 と採用済み ADR が規範であり、未マージの PR は規範へ取り込まない。

v2.0 発行後の仕様変更も、本文を直接改訂せず `docs/adr/` に ADR を追加する。
移管済み ADR は判断理由と経緯を残す履歴資料とし、現行規範の参照先を本書の節へ付け替える。
発行後に採用された ADR は、その対象範囲で本書の決定を改訂する。
[§11](#s11) に移管先と、後続 ADR により置換した決定を記す。

[Multi-Currency Design v2.1](research/2026-08-25-fx-multicurrency-system-design-v2.1.md) は実装設計の参考資料である。
採用済み ADR と実装による裏付けのない将来案を、本書の規範や実装済み機能として扱わない。
相場観と研究仮説は `docs/research/` に置く。
数値パラメータは YAML でバージョン管理し、本書に記した初期値の変更理由と検証結果も追跡可能にする。

<a id="s2"></a>

## 2. アーキテクチャと不変条件

<a id="s2-1"></a>

### 2.1 責務の境界

取引アプリケーションは Windows 1台上のモジュラーモノリスとする。
scalp / intraday / swing の Strategy は同一 Runner で動かし、時間軸ごとに執行プロセスを分けない。
市場データと口座の collector は MT5 のある Windows VPS、macro / policy / intervention の日次収集は Mac 側で行う。
collector の実行ホストを分けても、Strategy から執行への経路は共通にする。

```text
Collectors → Point-in-Time Event Store → Fundamental / Regime
→ Strategy → Portfolio sizing → Portfolio Arbitrator → Risk
→ OMS → MT5 Execution
```

Strategy は「保有をどう変えたいか」という signal までを生成し、最終数量、Risk の許可、broker command を決めない。
StrategyContext は clock、market、indicators、features、regime、currency_states、currency_regime、portfolio の読み取りと config に限定する。
Broker、OMS の書き込み、DB、credential を到達可能にしない。
LLM は初期 OFF とし、使用しても構造化イベントの生成までとする。
LLM に注文、パラメータの実行時変更、取引許可を与えない。
Event Bus は初期構成では使わない。

共通 Indicator は `indicators/` に置き、Strategy 内や Live / Backtest 間で重複実装しない。
時間足は StrategyConfig に置き、Strategy は運用時間軸で分類する。
通貨、pip size、数量制約、filling mode は InstrumentSpec または設定から取得し、symbol 文字列の解釈や固定値で代替しない。
構成の詳細は [PROJECT_STRUCTURE](PROJECT_STRUCTURE.md) に従う。

<a id="s2-2"></a>

### 2.2 維持する不変条件

```text
A Strategy cannot call Broker.
An LLM cannot call Broker.
Every live position must have broker-side SL.
Every broker position must map to internal ownership.
Every command-origin fill must map to a command.
Every broker-side SL/TP fill must map to a known position.
Unknown external fills must halt new risk.
UNKNOWN commands are never blindly retried.
SUBMITTING commands are never reclaimed as READY.
Exit is never a naked opposite market order.
Hedging exit must reference the target position.
Netting order quantity is broker-target delta, not raw strategy quantity.
A system exit cannot reverse a position that broker protection already closed.
Minimum broker size may never override risk limits.
Backtest cannot access information with known_at in the future.
Strategy code is shared between replay and live.
Every trade must be reproducible from stored input, config and code version.
```

Position の lifecycle は OPEN / INCREASE / REDUCE / CLOSE とする。
Position の LONG / SHORT と Order の BUY / SELL は別の型とする。
たとえば SHORT の CLOSE は BUY だが、LONG の新規 signal を意味しない。
不変条件を通すためにテストを緩めない。

実装と検証: [StrategyContext](../src/trading/strategy/base.py)、[方向の変換](../src/trading/domain/order.py)、[不変条件テスト](../tests/unit/test_invariants.py)。

<a id="s3"></a>

## 3. 市場データと時刻

<a id="s3-1"></a>

### 3.1 保存時刻と可視化時刻

Tick の `time` と Bar の `start` / `close_time` は broker の壁時計ラベルであり、実 UTC と同じ数直線として比較しない。
Bar の `known_at` は、その確定を観測した実 UTC とする。
通常の確定では、broker 時刻がバケツ終端へ達した tick の `received_at` がその値になる。
[§3.3](#s3-3) の時計逆行時は、逆行を検出した quote の `known_time` で確定する。

BarBuilder の畳み込みと確定は broker の `tick.time`、Strategy への可視化は `known_at` で判定する。
`Bar.known_at` は必須であり、DB の読み戻しでも保存値を使う。
`known_at >= end_at` のような、別の時計を比較する制約を置かない（migration 0004）。
`replay_time(Bar)` と市場データの可視化フィルタも `known_at` を読む。

受信時刻による通常 replay では、Tick は `received_at` から可視になり、値がない場合のみ broker 時刻を使う。
遅着した価格を broker 時刻まで遡って見せない。
backfill の `received_at` は取得時刻を保ち、修復前のシステムがその価格を知っていたことにはしない。
研究用の時刻復元は [§3.4](#s3-4) の別の入力契約とする。

<a id="s3-2"></a>

### 3.2 Bar の構築と保存

`TIMEFRAME_SECONDS` にある全時間足を、broker の `tick.time` の epoch floor で区切る。
4h と 1d も同じ規則とし、バケツを区切るための別の session anchor は設けない。
時刻復元や rollover に使う NY close anchor と、Bar の区切り方を混同しない。
Live も replay も tick から同じ BarBuilder で計算し、MT5 `copy_rates` の足を受け取る構成にはしない。
端末のチャートとの実機比較は別の確認事項であり、同一の畳み込み処理を使うという規則を置き換えない。

終端以降の quote が届くまで Bar は確定せず、clock だけによる flush はしない。
週末前の最終足も、週明けの quote が終端を越えて初めて可視になる。
BarService は保存された tick から毎回構築し、最後の保存 Bar の終端から前へ進む。
cold start は7日を読み戻し、読取範囲の途中から始まるバケツは保存しない。
バケツがまだ閉じていない場合は2回の index seek で終了し、長時間足も確定時点に追随する。

`market_bars` は当時見えた Live 系列であり、`(symbol, timeframe, start_at)` が重複した保存は無視する。
後日の backfill で既存行を書き換えない。
修復後の tick を反映した研究用の足は tick から再計算し、Live の保存 Bar を修正済み系列として読まない。

<a id="s3-3"></a>

### 3.3 broker 時計の逆行

開いているバケツより前の別バケツに属する quote が、そのバケツで観測した最大 broker 時刻から30分以上戻った場合を時計の逆行として扱う。
開いている Bar をその quote の `known_time` で確定し、戻った位置で新しいバケツを開く。
30分未満の別バケツへの逆行は straggler として捨てる。
同じバケツ内の逆行は通常の畳み込みに渡すが、古い quote で close を置き換えない。
経過時間による staleness guard は設けず、開くバケツは1つとする。

この規則は遅着と時計変更を完全には区別できない。
30分以上遅着した quote は時計変更と誤認されうるが、publish 済み Bar は書き換えない。
先行 quote の後に届いた旧バケツ向けの quote は足に入らず、tick 系列にだけ残る。
複数バケツの保持への変更は、実アーカイブによる必要性の確認を前提とする（#12）。

保存 tick の Live 読取と research stream は `ORDER BY event_time, id` であり、到着順の逆行は観測できない。
Live は保存足の終端より前へ読み戻さず、仮に再構築しても繰り返された足は一意キーが衝突する（#135）。
到着順で渡す BacktestEngine.run ではこの規則が働くが、実データ経路全体の解決とは扱わない。
publish 順では `Bar.start` が単調でなくなり、開始時刻だけを使う戦略の setup_id が衝突する制約もある（#133）。

NY close anchor が正しければ、米国の DST 切替は FX の週末休場内にある。
ただし anchor 自体の実測が必要であり、この仮定だけで #29 を解決済みとしない。
実機確認は #136 に残る。

<a id="s3-4"></a>

### 3.4 研究用の時刻復元

`trading.backtest.research` は、poll と backfill のどちらの Tick も、読取境界で broker ラベルから known time を一律に復元する。
保存行の `received_at` は変更しない。
この復元は仮説探索と粗い検証のためであり、live 昇格の最終判断は受信時刻を実測した forward-collected 系列で行う。

server の壁時計は New York の壁時計に `market.broker_server_ahead_of_ny_hours`（初期7時間）を加えたものとする。
anchor を引いて America/New_York へ対応付け、DST を timezone database に従わせる。
固定の UTC+3 や UTC+2 で年間全体を扱わない。
DST で重複または欠落する時間のラベルは、休場内であるはずの時刻に quote があるという矛盾なので、fold を推測せず拒否する。

poll と backfill が混在する期間も同じ軸に置くため、poll の受信時刻も研究入力では置換する。
通常の poll では実受信時刻との差は通信遅延になる。
復元するのは可視化軸のみであり、Bar の broker 軸の OHLC 計算は変更しない。
anchor の訂正は設定と変換処理に限り、保存済み tick を変更しない。

実装と検証: [BarBuilder](../src/trading/data/market/bars.py)、[BarService](../src/trading/data/market/bar_service.py)、[research](../src/trading/backtest/research.py)、[時刻と逆行のテスト](../tests/unit/test_bar_builder.py)。

<a id="s4"></a>

## 4. 通貨と Risk

<a id="s4-1"></a>

### 4.1 InstrumentSpec と Money

InstrumentSpec は必須の `base_currency` / `quote_currency` を Currency enum で持つ。
Live / demo は MT5 `symbol_info()` の `currency_base` / `currency_profit`、backtest / synthetic はデータセット定義が明示する値を使う。
symbol 名から通貨を parse しない。
Currency は USD / JPY / GBP / EUR に限定し、対応外通貨は spec 構築時に拒否する。
第5通貨は明示的なコード変更として追加する。

Risk / Portfolio の通貨を持つ金額は、frozen な `Money(amount: Decimal, currency: Currency)` で表す。
異通貨の Money の加算は `CurrencyMismatchError` とし、暗黙に混ぜない。
ratio、percentage、units、price は Money と区別する。
金額、数量、価格の計算に float を使わず、Indicator 計算のみ float を許す。
金額名の `_jpy` のような接尾辞だけを通貨整合の保証にしない。

<a id="s4-2"></a>

### 4.2 使用時の通貨換算と決済の例外

口座通貨JPYへの換算は AccountCurrencyConversionService に集約し、呼び出し側は Money を受け取る。
生の換算 rate を Risk の呼び出し側へ漏らさない。
`convert(..., now=...)` のたびに鮮度を判定し、DTO に時間依存の `is_stale` を保存しない。

| purpose | 欠損、stale、異常な quote の扱い |
| --- | --- |
| RISK_INCREASING | 欠損、stale、未来時刻、非正値なら sizing を拒否する。`CONVERSION_RATE_UNAVAILABLE` / `CONVERSION_RATE_STALE` を記録する |
| MONITORING | stale なら last-good quote に haircut を加えて評価する。未来時刻と非正値は拒否する |

損失を過小評価しないよう、直接 quote は ask、逆数は 1/bid を使う。
換算 path は注入された InstrumentSpec の base/quote とその逆向きに限り、自動探索しない。
新しい path は承認対象として明示的に追加する。
ConversionTrace には path、source known_at、leg age、purpose を残すが、Risk の金額計算は Money を読む。
replay の換算も、その clock で可視な quote だけを使う。

close / reduce / protection 維持は、MONITORING が値を返せるならその値を使い、換算が完全に失敗しても決済を止める理由にはしない。
ticket 整合、fresh select、口座モードに応じた既存の安全条件は引き続き必須とする。
entry の換算拒否を exit に流用しない。

`ConversionStress` は決定論的な adverse floor とし、sizing の `conversion_stress_adverse_pct` は初期0で無効にする。
条件付き historical quantile による推定は未導入である。
haircut、buffer、stress の校正は broker 実測と検証結果に基づいて行う。

<a id="s4-3"></a>

### 4.3 損失と exposure の上限

損失制限は永続化した account_snapshots から、JST暦日、rolling 24h、high-water-mark drawdown の3窓で評価する。
日付境界で rolling 24h と HWM をリセットしない。
初期上限は [config/base.yaml](../config/base.yaml) の daily 0.75%、rolling24h 1.00%、HWM 3.00% とし、初心者向け Micro Live 上限を backtest 最適化の対象にしない。

broker position が増える注文には、`max_open_positions_per_symbol` と `max_open_positions_portfolio` の両方を適用する。
初期値は同一symbol 1、portfolio 3だが、micro_live / production は多ペア live を別途判断するまで portfolio 1を維持する。
netting の実質的な縮小には件数上限を適用しない。
PreTradeContext は両方の件数を運び、拒否理由を `MAX_OPEN_POSITIONS_PER_SYMBOL` / `MAX_OPEN_POSITIONS_PORTFOLIO` と区別する。
Strategy 単位の上限が必要になった場合は別の制約とする。

portfolio の open stop-risk と候補の stop-risk の合計を、equity 比の `portfolio_stop_risk_budget_pct` 以下に制限する。
初期0.10%で、per-trade 0.05%を4本分まで自動許可しない。
通貨別 net exposure は `max_currency_net_exposure_pct`（初期equity比300%）以下に制限し、leverage 上限とは区別する。
拒否理由は `PORTFOLIO_RISK_LIMIT` / `CURRENCY_EXPOSURE_LIMIT` とする。
これらの数値は Monte Carlo と demo 実測で校正する。

exposure は InstrumentSpec の通貨 leg へ分解する。
base leg の値を units × price で quote 通貨建てにし、quote から口座通貨へ換算する。
既存 book は MONITORING、候補 leg は sizing と同じ保守側で評価する。
同じ通貨方向への偏りは通貨 net に合算し、pair 名の固定 cluster で代替しない。
Arbitrator の triangle 制約は [§7.2](#s7-2) として別に適用する。

PortfolioRiskSnapshot は provider が供給する。
shadow は仮想 book（通常空）、backtest は simulator の stop を含む position から作る。
pending entry の stop-risk はこの snapshot の合計に含めない逐次近似であり、当 cycle の受理候補は Arbitrator の greedy 再評価で扱う。

最小ロットが Risk 許容量を超えたら `MINIMUM_BROKER_SIZE_EXCEEDS_RISK` で拒否し、数量確保のために risk% を引き上げない。
Kill Switch は HALT_NEW_ORDER / CLOSE_ONLY / EMERGENCY とし、EMERGENCY は無条件の全成行決済ではなく Freeze、Reconcile、実行可能な exit の評価を行う。

<a id="s4-4"></a>

### 4.4 platform 対応と発注許可

instrument ごとの `platform_enabled` は収集、feature、shadow、裁定シミュレーションの対象を表す。
`trading_enabled` は実発注の許可を表し、global の `risk.trading_enabled` と AND で適用する。
設定にない symbol は両方 false として新規 entry / increase を拒否するが、既存 position の close / reduce は止めない。
拒否は `INSTRUMENT_TRADING_ENABLED` として記録する。
backtest は live 昇格前の検証に使うため、この instrument gate を適用しない。

初期設定では4ペアが platform_enabled、instrument の trading_enabled は USDJPY のみ true とする。
base の global switch は false であり、これだけでUSDJPYの発注を開始するものではない。
初期の `max_units_per_symbol` は各ペア1,000 unitsとする。
未設定の `max_units_per_symbol` も fail-close とし、instrument のフラグだけで制限を迂回できない。
platform 対象の Runner への配線は [§6.4](#s6-4) に従う。
ペアの昇格には対応 gate の ADR と設定変更が必要である。

実装と検証: [Money](../src/trading/domain/money.py)、[換算](../src/trading/risk/conversion.py)、[RiskEngine](../src/trading/risk/engine.py)、[exposure](../src/trading/portfolio/exposure.py)、[換算テスト](../tests/unit/test_conversion.py)。

<a id="s5"></a>

## 5. データ取得と intelligence

Source Registry は `events` 上の View とし、別の正本を二重管理しない。
介入データの推定額と公式額は別カラムに保持し、検証状態の遷移はイベントとして追記する。

<a id="s5-1"></a>

### 5.1 GBP と EUR の公式統計および PIT 分類

GBP と EUR の方向感に用いる公式統計は、次の canonical 系列として継続収集する。
収集できる系列と factor に接続する系列は区別し、factor への現行の対応は §5.4 に定める。

| canonical 系列 | 公式ソースと取得経路 |
| --- | --- |
| `uk_bank_rate` | BOE IADB CSV、`IUDBEDR`、日次 |
| `uk_cpi_headline_yoy_nsa` | ONS website timeseries JSON、`D7G7` / MM23 |
| `uk_unemployment_rate_sa` | ONS website timeseries JSON、`MGSX` / LMS、LFS ローリング3か月 |
| `uk_real_gdp_growth_qoq_sa` | ONS website timeseries JSON、`IHYQ` / PN2、四半期 |
| `ea_deposit_facility_rate` | ECB Data Portal SDMX-JSON、`FM/D.U2.EUR.4F.KR.DFR.LEV` |
| `ea_hicp_headline_yoy_nsa` | Eurostat statistics API、`prc_hicp_manr` の CP00 |
| `ea_unemployment_rate_sa` | Eurostat statistics API、`une_rt_m` |
| `ea_real_gdp_growth_qoq_sca` | Eurostat statistics API、`namq_10_gdp` の B1GQ |

これらの取得に API キーは使わず、HTTP transport は収集主体を示す User-Agent を送る。
UK CPI と euro area HICP は指数の基準改定に依存しない前年比を正本とする。
指数に基づく momentum などを追加する場合は、基準をつなぐ vintage 管理も含めて別途設計決定する。

ONS は廃止済みの time-series API に依存せず、`www.ons.gov.uk/{topic}/timeseries/{cdid}/{dataset}/data` を使う。
この経路には文書化された API 契約がないため、パーサは依存するフィールドだけを検査し、raw payload を全量保存し、実応答の構造に沿った fixture で契約テストを行う。
Eurostat では `EA21, EA20, EA` を毎回要求し、観測期間ごとに値が存在する最新構成を選ぶ。
全候補に値がなければ失敗として扱い、正常な0件収集にはしない。
ECB の `U2` は変動構成を表す。

上記8系列の `IndicatorSpec.pit_classification` は `PIT_UNVERIFIED` とする。
最新値だけを返すソースから過去の真の vintage は復元できないため、有効な PIT データは `known_at = 取得時刻` の forward snapshot に限る。
過去履歴を backfill しても公表時刻を `known_at` に与えず、strict OOS と PIT 評価では収集開始前の期間を除外する。
ALFRED の vintage アーカイブで履歴を裏付ける US 系列は `PIT_VERIFIED` とする。
JGB 2年金利だけに認める例外は §5.3 に定める。

EUR の inflation coverage は、2026年8月26日の調達確認時点で HICP が2025年12月までしか取得できず、production 昇格前に解消すべき gate として残っている。
この過去の確認結果を、現在の供給停止や gate 通過の証拠に置き換えない。
Eurostat flash HICP の追加と BOE / ECB の声明採点は未採用であり、会合日程の扱いは §5.9 に従う。

根拠実装: [系列レジストリ](../src/trading/data/macro/registry.py)、[HTTP transport](../src/trading/data/macro/http.py)、[ONS](../src/trading/data/macro/ons.py)、[Eurostat](../src/trading/data/macro/eurostat.py)、[BOE](../src/trading/data/macro/boe.py)、[ECB](../src/trading/data/macro/ecb.py)。

<a id="s5-2"></a>

### 5.2 2年カーブによる RATES 入力と調達 gate

研究と backtest の RATES factor は、USD の `us_treasury_2y_yield`、GBP の `uk_ois_2y`、EUR の `ea_yield_curve_2y` を使う。
年限は3通貨とも2年にそろえる。
GBP は BOE OIS spot カーブの「4. spot curve」シート、EUR は ECB の AAA ソブリンカーブ `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y` から取得する。
GBP と EUR のカーブも forward collection とし、`known_at` は取得時刻、分類は `PIT_UNVERIFIED` とする。

これらはカーブ上の1点であり、会合ごとの政策金利織り込みと同じ feature にはしない。
USD の国債利回りも期間、信用、成長、インフレのプレミアムを含むため、GBP と EUR だけに proxy を理由とした confidence 減点は付けない。
各通貨自身の履歴による正規化は定常的なプレミアム差を吸収するが、プレミアムの変化はスコアに残る。
将来1通貨だけに真の meeting-path 系列を導入する場合は、その時点で factor 単位の confidence 減点を決める。

GBP と EUR の `rates_score` の live 昇格 gate は閉じたままとする。
公式カーブを研究入力に接続することは、この gate の通過を意味しない。
ICE MPC Dated SONIA futures と ICE / Eurex ECB Dated €STR futures の entitlement、履歴深度、latency、license、cost の調達評価は別途管理する。
採用時の評価では、2025年以降の上場で履歴が1.5年未満のため strict OOS に必要な深度を得られなかった。
GBP / EUR が M6 の live 昇格候補となった場合、または meeting-dated futures の履歴が3年を超えた場合に再評価する。

BOE の取得では履歴用 `oisddata.zip` と当月用 `latest-yield-curve-data.zip` の両方を読む。
日付の重複は当月ファイルを優先し、履歴 zip は要求年と重なる member だけ、当月 zip は OIS の member だけを開く。
名目、実質、インフレの GLC カーブを OIS と取り違えない。
Excel の読み取りには `openpyxl` を使う。
容量を抑えるため workbook 自体は raw event に保存せず、抽出した2年点の系列と配布ファイルの SHA-256 を残す。
各観測には、その日付を実際に収録していた配布ファイルの URI と hash を付ける。
この保存方式では原本を再解析することはできず、出所の検証は digest 照合までとなる。

根拠実装: [BOE yield curve collector](../src/trading/data/macro/boe_yield_curve.py)、[ECB collector](../src/trading/data/macro/ecb.py)、[factor 入力](../src/trading/data/factor_series.py)、[依存定義](../pyproject.toml)。

<a id="s5-3"></a>

### 5.3 JGB 2年金利の保守的な公表時刻上界

財務省「国債金利情報」の2年複利利回りを `jp_jgb_2y_yield` として `macro_observations` に保存する。
全期間の `data/jgbcm_all.csv` と当月の `jgbcm.csv` を毎回取得し、基準日で統合し、重複日は当月ファイルを優先する。
CSV は Shift_JIS と和暦短縮日付を解釈し、`2年` 列を読む。
値が `-` の行は観測を作らないが、その基準日は前の行の公表日を決める材料に残す。

この系列に限り、backfill にも「その基準日の次の基準日の15:00 JST」を `known_at` として付ける。
財務省の翌営業日09:30頃という公表時刻に余裕を置いた上界であり、祝日カレンダーの代わりに CSV の基準日列を使う。
次の基準日がまだない最新行と、次の基準日まで14暦日を超える行は出力しない。
後者は全期間ファイルの月次更新と当月ファイルの切り替わりの間にできた空白を、翌営業日として誤認しないための制限であり、真の後続基準日が現れた収集で出力する。

`known_at` は再実行と独立 DB のホスト間で同じ値となる。
後日同じ基準日の値が変わっても同じ vintage キーへの競合で初出値を保持し、訂正の公表時刻を推測して別の vintage を作らない。
真の vintage アーカイブはないため分類は `PIT_UNVERIFIED` のままとするが、この系列の strict OOS 評価では収集開始前の期間を除外する必要はない。
例外の前提は、公表時刻が公式に有界であり、歴史ファイルが実務上初出値のまま維持されることである。
他の `PIT_UNVERIFIED` 系列へ一般化せず、追加の適用は系列ごとに設計判断する。

保存には次の基準日の出現を待つため最大1営業日強、月末行には全期間ファイルの更新待ちで2〜3日の遅延がある。
live 昇格の設計はこの遅延を前提にする。
17:00 ET の日足 close で見える JP2Y と、vintage 日18:00 ET の ALFRED US2Y は、通常どちらも前営業日の値となる。
JGB の収集と研究利用は、JPY の RATES factor への接続を意味しない。
現行の factor 対応にはこの系列を含めない。

根拠実装: [JGB collector](../src/trading/data/macro/jgb.py)、[系列レジストリ](../src/trading/data/macro/registry.py)、[factor 対応](../src/trading/data/factor_series.py)。

<a id="s5-4"></a>

### 5.4 macro 観測から factor への写像

`FactorSeriesSource` は `(currency, factor, now)` に対して、`known_at <= now` の `(known_at, raw_value)` 観測列を返す。
供給側が PIT の一次責任を持ち、未供給の組み合わせは空列とする。
`MacroFactorSeries` と `PolicyScoreFactorSeries` は `ChainedFactorSeries` で束ね、最初に観測を返した供給元を採用する。
供給元は互いに素な `(currency, factor)` を担当し、重複設定がある場合の挙動は先勝ちとする。

現行の供給元は次のとおりとする。

| factor | USD | JPY | GBP | EUR |
| --- | --- | --- | --- | --- |
| POLICY | FOMC 声明スコア | BOJ 声明スコア | 欠測 | 欠測 |
| GROWTH | 米失業率 | 欠測 | 英失業率 | ユーロ圏失業率 |
| INFLATION | 米 CPI を前年比へ変換 | 欠測 | 英 CPI 前年比 | HICP 前年比 |
| RATES | 米国債2年利回り | 欠測 | 英 OIS 2年 | ユーロ圏 AAA 2年 |
| RISK_SENTIMENT | 欠測 | 欠測 | 欠測 | 欠測 |

macro の1 factor には1系列だけを割り当てる。
異なる単位の複数系列を同じ分布に混ぜず、複数列の合成が必要になった場合は `FactorSeriesSource` の契約変更とともに決める。
`LEVEL` は率や利回りをそのまま渡し、`YEAR_OVER_YEAR` は水準や指数を同じ観測期間ラベルの1年前と比較して前年比へ変換する。
前年の相手が欠測または0ならその点を落とし、補間しない。
`FactorInput.sign` により値が大きいほど通貨が強い向きにそろえ、失業率には `-1` を掛ける。

vintage 連鎖は観測期間ごとの最初に届いた1点へ畳む。
後の改定を直近の方向感として使わないため、`known_at` による読み出し窓に加え、観測期間が窓の開始より古い行も除外する。
出力順は `(known_at, 観測期間)` とする。
初回の forward 収集で全履歴が同じ取得時刻となっても、任意の UUID 順で古い期間を直近値にしない。

読み出し幅は正規化 window に12観測期間の余裕を加え、前年比変換ならさらに1年分を加える。
日次252、月次12、四半期4を年間観測数として年数に換算し、365.25日を掛けて日数を切り上げる。
`MacroFactorSeries` と `CurrencyStateService` には同じ `NormalizationConfig` を渡す。

根拠実装: [factor 供給元と写像](../src/trading/data/factor_series.py)、[供給元 Protocol と合成](../src/trading/intelligence/currency.py)。

<a id="s5-5"></a>

### 5.5 正規化と POLICY の共通尺度

通常の factor は可視観測だけを使い、rolling median と MAD による robust z、clip、tanh の順に変換して `[-1, 1]` の範囲へ収める。
既定値は window が60観測、最小観測数が20、`clip_sigma` が3.0である。
計算は `z = (latest − median) / MAD / 1.4826`、`score = tanh(clip(z, ±clip_sigma) / clip_sigma)` とする。
全期間で fit したパラメータを過去へ適用しない。
非有限値を観測に数えず、観測不足、MAD が0、または非有限の z は `None` とする。
欠測を中立の0へ変換しない。

POLICY は通貨横断で共通の採点表を使う声明スコアであり、rolling robust 正規化の対象から外す。
`CurrencyScoreConfig.bounded_factors` の POLICY 上限は2.0とし、最新の可視スコアを2.0で割って `[-1, 1]` に制限する。
上限は通貨ごとに変えず、観測数の下限も課さない。
政策スタンスの絶対的な乖離を、各中銀の履歴に対する相対位置で消さないためである。

POLICY の供給元は FED と BOJ の声明スコアだけとする。
`PolicyScoreFactorSeries` は `EventRepository` の過去400日から、このビルドの `SCORING_VERSION` に一致するイベントを読む。
BOE と ECB の採点は未供給として coverage を下げ、`uk_bank_rate` や `ea_deposit_facility_rate` の水準で置き換えない。
これらの政策金利系列の収集は研究と将来の採点材料のため継続する。
BOE / ECB の声明採点は M6 以降の判断とし、RISK_SENTIMENT の供給元も未採用とする。
forward risk-normalized return への walk-forward calibration と USD perturbation 感度検証（±0.25 / 0.5 / 1.0σ）は、この正規化の実装済み機能に含めない。

根拠実装: [正規化](../src/trading/intelligence/normalization.py)、[声明採点](../src/trading/data/policy/scoring.py)、[POLICY 供給](../src/trading/data/factor_series.py)。

<a id="s5-6"></a>

### 5.6 CurrencyState と PairState

方向感は通貨ごとに作り、ペアは base と quote の差として求める。
`CurrencyState.directional_score` は利用可能な factor の加重平均とし、欠測 factor の重みを分母から外す。
既定では5 factor を等重みとする。
個別の `factor_scores` は `Decimal | None` で保持する。
欠測で残りのスコアを薄めたり、coverage 不足を理由にスコアを膨らませたりせず、不確かさは confidence に表す。

`confidence` はスコアの絶対値とは独立に、coverage と freshness から求める。
正確には、観測できた各 factor の「設定重み × freshness」の合計を、期待する全 factor の重み合計で割る。
鮮度の計算は §5.7 に従う。

`PairState` の射影は次の式に従う。

```text
pair.directional_score = b − q
magnitude = |b| + |q|
cancellation = 0                         (magnitude = 0)
cancellation = (magnitude − |b − q|) / magnitude  (それ以外)
pair.confidence = min(base.confidence, quote.confidence)
                  × (1 − cancellation × pair_cancellation_penalty)
```

`pair_cancellation_penalty` の既定値は0.5とする。
片方の観測不足を平均で覆い隠さず、両通貨が同じ方向を向くときの相殺にも減点する。
`PairState.known_at` は両 leg の新しい方とする。
方向感が新しい leg の情報を含むため、古い方の時刻へ遡らせない。

`PairState` には event risk を重ねて保持しない。
方向感と scheduled event gate を分離し、event risk の唯一のペア別判断は §5.9 の `EventRiskCalendar.mode_for_instrument` とする。

根拠実装: [通貨 state とペア射影](../src/trading/intelligence/currency.py)。

<a id="s5-7"></a>

### 5.7 系列間隔に応じた freshness

freshness は正規化に用いた可視観測の到着間隔に従う。
相異なる `known_at` を昇順に並べ、その隣接差の中央値を cadence とする。
同一時刻の重複は間隔0を作らないよう除き、相異なる時刻が2個未満なら cadence は不明とする。

```text
full = max(cadence_hours, 48)
zero = full × 3
age <= full        : freshness = 1
full < age < zero  : freshness = (zero − age) / (zero − full)
zero <= age        : freshness = 0
```

cadence が不明な場合だけ、full 48時間、zero 336時間を使う。
日次系列は48時間まで満点、144時間で0となる。
月次、会合、四半期の系列には通常の公表間隔を適用し、次の公表を待っているだけで一律の14日後に confidence を0にしない。

`CurrencyState` は factor ごとの `fitted_through` と cadence を `freshness_basis` に保存する。
`CurrencyStateStore.retime(now)` は評価時刻を設定し、`get()` と `pair()` は共通の純関数で confidence と `known_at` をその時刻に合わせて再計算する。
live は毎評価サイクルの `StoredFeatureSource.refresh(now)`、replay は毎回の `ReplayFeatureTimeline.advance(now)` から時刻を設定する。
同一の可視入力と評価時刻なら、full refresh の間も live と replay の confidence は一致する。
`directional_score` 自体は読み取り時刻によって変えない。

読み出し窓のスライドによる期限切れは従来どおり日付粒度とし、US2Y には既存の expiry instant を使う。
この期限切れの扱いを cadence の変更で拡張しない。

根拠実装: [cadence の算出](../src/trading/intelligence/normalization.py)、[freshness と retime](../src/trading/intelligence/currency.py)、[live と replay の更新](../src/trading/data/features.py)。

<a id="s5-8"></a>

### 5.8 通貨別 regime と global regime

regime は通貨別と全通貨共通の二層で保持する。
`CurrencyRegimeSnapshot` は `by_currency`、`global_regimes`、`known_at` を持ち、`active(currency)` はその通貨の集合と global の集合の和を返す。
global の risk-off は `GLOBAL_RISK_OFF` と表記する。

通貨別ルールは、判定に必要な feature が供給される通貨だけに定義する。
現行は USD の政策 hawkish、JPY の政策 hawkish と介入リスクを扱い、供給のない GBP / EUR の政策ルールを置かない。
介入リスクは JPY の状態として扱う。
global risk-off と global liquidity stress の既定ルールも、入力 feature が未供給のため空とする。

`CurrencyRegimeService.snapshot(now)` は feature store の現在値を読み、strategy が注入された clock から渡す時刻で snapshot を作る。
この service は repository を保持しない。

根拠実装: [通貨別 regime](../src/trading/intelligence/regime.py)。

<a id="s5-9"></a>

### 5.9 通貨 scope を持つ scheduled event gate

scheduled event risk は `EventRiskWindow` の `affected_currencies: frozenset[Currency]` と `propagation: EventPropagationPolicy` で表す。
window と別の `RiskEvent` モデルは導入しない。
`mode_for_instrument(spec, horizon, now)` は、そのペアに届く window の最大 severity を返す。
coverage がなく暦として判断できない場合は `None` とする。

| propagation | 適用範囲 |
| --- | --- |
| `DIRECT_LEGS` | affected と `{base, quote}` の共通部分があるペア |
| `GLOBAL_CRITICAL` | 全ペアの hard gate |
| `DEPENDENCY_GRAPH` | sensitivity 導出が未実装のため、現状は全ペアの hard gate |

FED の政策決定は `GLOBAL_CRITICAL` に固定する。
BOJ、BOE、ECB は通貨 leg の交差で適用し、ECB 会合だけで USDJPY を止めない。
`affected_currencies` が空の window は、scope の記載漏れに対して止める側へ倒すため全ペアに適用する。
通貨 scope を無視する `mode_for(horizon, now)` は、全 window を見る保守的な view として残す。

中央銀行会合の cluster は bank ごとに構築し、FED と BOJ など別の bank の近接会合を1 window に統合しない。
連続会合の切れ目のないリスク状態は、scope 付き window の重なりとペアごとの最大 severity によって維持する。

meeting file は window の材料となる schedule に限り BOE と ECB を許可する。
`PolicyMeeting` の facts と採点は FED / BOJ に限る。
bank と通貨の対応は `BANK_CURRENCIES` に従い、GBP / EUR ペアの shadow 開始前に会合日程を登録する。
日程を扱えることを、BOE / ECB 声明の採点が実装済みであることと混同しない。

根拠実装: [event calendar](../src/trading/risk/event_risk.py)、[会合 window と bank 対応](../src/trading/data/policy/risk_windows.py)、[meeting file](../src/trading/data/policy/meetings.py)。

<a id="s5-10"></a>

### 5.10 strategy への state 供給と replay の凍結

repository に到達する `CurrencyStateService` と `FactorSeriesSource` は strategy の外に置く。
`StrategyContext.currency_states` には `get` と `pair` だけを持つ `CurrencyStateView` を、`currency_regime` には repository を持たない `CurrencyRegimeService` を渡す。
更新 API は供給側の具象 `CurrencyStateStore` に閉じる。

`StoredFeatureSource` は feature と通貨 state を、同じ源の同じ時刻から生成して中身ごと置き換える。
供給が途切れた通貨を前回値のまま残さない。
1つも factor score が得られない通貨は store に入れず、`pair()` は両 leg がある場合だけ返す。
この判断は freshness が0かどうかではなく、観測に基づく factor score の有無で行う。

`frozen()`、`change_instants()`、`dataset_fingerprint()` は同じ入力行集合に基づく。
`frozen()` は strategy が参照する同一 store インスタンスを引き継ぐ。
凍結する macro 観測は `DEFAULT_FACTOR_INPUTS` の全系列を対象とし、系列ごとの幅は `MacroFactorSeries.read_windows()` に従う。
US2Y は feature 用の窓と RATES factor 用の窓の広い方を採る。
入力行が増えた場合は dataset fingerprint も変わる。

`ReplayFeatureTimeline` の既存 expiry は US2Y の窓幅を使う。
月次や四半期 factor の年単位の窓に対して追加される短い expiry は refresh の機会となり、系列自体の読み出し幅を短くしない。
state を strategy へ供給する配線は、strategy がこれを売買判断に採用済みであることを意味しない。
採用する strategy の挙動は個別の戦略仕様で定める。

根拠実装: [供給と凍結](../src/trading/data/features.py)、[StrategyContext](../src/trading/strategy/base.py)、[読み取り view](../src/trading/intelligence/currency.py)。

<a id="s5-11"></a>

### 5.11 swap と rollover の PIT broker cost

overnight carry は、MT5 `symbol_info` の観測に基づく broker cost data として扱う。
VPS 上の swap collector は `swap_mode`、`swap_long`、`swap_short`、`swap_rollover3days`、曜日別倍率を定期観測し、parsed 行を `swap_snapshots`、raw payload を events の `SWAP_SNAPSHOT_RAW` に保存する。
`known_at` は取得時刻とし、観測を backfill しない。
運用では collector の常駐タスクを登録し、6時間間隔または日次の `--once` で観測する。
collector が未稼働で snapshot がないことを、swap が0である証拠にはしない。

曜日倍率は broker が `swap_sunday` から `swap_saturday` を返す場合にはその値を採る。
返さない terminal では、broker の `swap_rollover3days` に指定された曜日を3倍、rollover が発生しない週末を0、残りを1とする。
水曜日を triple とする固定値は使わない。

rollover boundary は broker server の日付変更とする。
server 壁時計は NY より `broker_server_ahead_of_ny_hours` 時間先行する規約を replay と共有し、boundary は NY ローカルの `24 − ahead` 時（既定17:00）とする。
DST は `America/New_York` で追従し、独立した rollover 時刻設定は設けない。

replay clock が boundary を跨ぐと、open position ごとに `known_at <= boundary` の最新 snapshot で carry を計上する。
boundary より後に取得した snapshot で値付けしない。
snapshot がなければ金額を推測せず、`unpriced_rollovers` に数える。
対応する金額モデルは `SWAP_MODE_POINTS` のみとする。

```text
carry = points × 10^(-digits) × quantity × 曜日倍率
```

金額は quote 通貨建てで、符号は broker の値に従う。
未対応 mode は `UnsupportedSwapModeError` とし、対応追加には実測を伴わせる。
backtest metrics は `carry_total` と `unpriced_rollovers` を常時出力し、`execution_cost` から carry を除外して spread と slippage のコストと分離する。
research replay は可視範囲の `swap_snapshots` をロードする。
snapshot がない既存データでは carry 計上額は0のままだが、値付け不能な rollover の件数を残す。

受信順 replay では、ticket ごとの計上記録を使って遅着 tick を訂正する。
rollover 前の broker 時刻の決済が後から届いた場合は決済数量に応じて carry を取り消し、server midnight 前の時刻で建った遅着建玉には遡って計上する。
値付け不能と数えた境界越えも同様に取り消す。
訂正が金額を変えた場合は、同じ instant の保存済み snapshot を置き換える。

計上から訂正までの instant に記録した `high_water_mark`、`max_drawdown`、`equity_curve` は遡って再計算しない。
経路全体の正確な再計算には broker 時間軸での全 replay が必要であり、部分再計算は行わない。
このため取り消した carry に由来する経路集計の残差が残る。
採用時に想定した残差は取り消した1泊分の carry で、HWM の高止まりによる halt の早期化や drawdown の過大表示という保守側の制約として扱う。

根拠実装: [swap collector](../src/trading/data/swap/collector.py)、[金額モデル](../src/trading/domain/swap.py)、[rollover clock](../src/trading/backtest/rollover.py)、[計上と遅着訂正](../src/trading/backtest/engine.py)。

<a id="s6"></a>

## 6. Strategy の設定と評価

<a id="s6-1"></a>

### 6.1 パラメータと spread gate

Strategy パラメータは defaults と instrument ごとの override に分け、StrategyParameterResolver の解決結果だけを読む。
既存の flat なパラメータは defaults として扱う。
設定境界で spread_gate 群と absolute_max_spread_pips の型を確定し、不正な設定は起動時に拒否する。

Strategy の spread gate は spread / ATR の無次元比を主とし、必要ならペア別の absolute ceiling を併用する。
Risk は ATR を使わず、ペア別の `absolute_max_spread_pips` を安全上限にする。
上限未設定のペアは fail-close とし、旧 `RiskConfig.max_spread_pips` の互換シムは設けない。
非USDJPYの初期値は暫定であり、各ペアの昇格 gate で校正する。

<a id="s6-2"></a>

### 6.2 session と entry policy

session 関数は実時刻を表す aware datetime を受け取り、IANA timezone で開始と終了を判定する。
timezone 名は市場定義の定数とし、broker の時計や固定UTC窓を session 定義に使わない。

| Session | Timezone | ローカル時間 |
| --- | --- | --- |
| TOKYO | Asia/Tokyo | 09:00–18:00 |
| LONDON | Europe/London | 08:00–17:00 |
| NEW_YORK | America/New_York | 08:00–17:00 |

London と New York は各地の夏時間に従い、切替日が違う期間も区別する。
現行mainでは、broker ラベルの Bar.start をそのまま渡す Session VWAP の制約が残る（#100）。
正しい実時刻を入力する契約と、消費側での正規化の未解決部分を区別する。
本統合は未マージの ADR-034 を取り込まず、この制約を解消済みとはしない。

Strategy は `ctx.clock.now()` と、instrument が参照する session profile の policy で entry を制御する。
Runner、Portfolio、Risk に session 判定を移さず、StrategyContext に執行面を追加しない。
全 Strategy に同じ `StrategyConfig.session_profiles` を渡し、profile 参照先の存在は設定境界で検証する。

| Policy | RESEARCH_ONLY / BACKTEST_ELIGIBLE / SHADOW | MICRO_LIVE / LIMITED_LIVE / PRODUCTION |
| --- | --- | --- |
| PREFERRED / ALLOWED | entry可 | entry可 |
| SHADOW_ONLY | entry可 | entry不可 |
| DISABLED | entry不可 | entry不可 |
| 対象sessionが開いていない、またはprofileにないsessionだけが開いている | entry不可 | entry不可 |

session が重なる場合は DISABLED < SHADOW_ONLY < ALLOWED < PREFERRED のうち最も緩い policy を採る。
profile を参照しない instrument にはこの gate を設けない。
PREFERRED と ALLOWED は gate として同じであり、sizing の違いは定義しない。
live status の signal を session 単位で shadow に振り分ける機能は未実装である。
Scalping の profile は全session SHADOW_ONLYで、Scalping自体も RESEARCH_ONLY を維持する。

<a id="s6-3"></a>

### 6.3 session 閉鎖中の決済

gate 閉鎖中でも保有がある instrument は評価し、反対向きの setup が成立したら `exit_only=True` の signal を出す。
`desired_direction` は保有の逆向きとし、LONG / SHORT の既存の意味を維持する。
同方向の setup は増加につながるため出さないが、その分岐が後続の反対向き setup の評価を打ち切らないようにする。
保有がない instrument は閉鎖中の評価を止める。

Portfolio は最新の仮想 position が signal と逆向きの場合だけ CLOSE を生成し、OPEN / INCREASE は生成しない。
position がない場合、または既に反転して同方向になった場合は何もしない。
entry の setup memo は閉鎖中に消費せず、決済専用は `(symbol, direction, exit_only=True)` の別slotで重複を抑止する。
そのため session 開放後は同じ setup から通常の entry を評価できる。

signal の DB に exit_only 列は追加せず、trail の SESSION_CLOSED_EXIT_ONLY と CLOSE のみの intent で判別する。
VirtualPositionLedger は `(strategy_id, symbol)` の最新snapshotを索引で持ち、閉鎖中の市場eventごとに履歴全体を走査しない。
shadow では fill が届かず仮想 book が通常空なので、決済専用 signal は保有を記録した場合に限られる。

<a id="s6-4"></a>

### 6.4 複数symbolの shadow cycle

`primary_instruments` または繰り返し指定した `--symbol` の順で対象を選び、1 cycleを1 instant、1 dispatchとする。
Strategy は全instrumentsを一度のdispatchで評価し、symbolごとにdispatchを繰り返してsetupを消費しない。
account gateを先に評価し、口座snapshotが欠損またはstaleなら全symbolを停止して、dispatchとfeature refreshも行わない。

quoteの有無と鮮度、sizing、event mode、risk contextはsymbolごとに分離する。
fresh quoteを持つ対象symbolだけをsizeしてgradeし、他symbolの停止に巻き込まない。
対象symbolでquote欠損または数量が1 step未満のためintentを作れないsignalもtrailに残す。
対象外symbolのsignalは別プロセスの担当範囲として記録しない。

market eventのretrieved_atとknown_atにはcycle instantを使う。
対象が稼働Strategyのinstrumentsにない場合、またはplatform_enabledでない場合は起動を拒否し、黙って除外しない。
ShadowCycleは停止理由をsymbolごとに返し、1symbolでも停止なら `--once` は非ゼロで終了する。
InstrumentSpec取得時のMT5 initialize / shutdownはsymbolごとに行い、単一sessionへの最適化は未導入とする。

実装と検証: [設定解決](../src/trading/strategy/parameters.py)、[Strategy共通処理](../src/trading/strategy/base.py)、[session](../src/trading/indicators/session.py)、[shadow](../src/trading/live/shadow.py)、[session gateテスト](../tests/unit/test_session_entry_gate.py)。

<a id="s7"></a>

## 7. Portfolio の集約と裁定

<a id="s7-1"></a>

### 7.1 netting と fill 配分

Portfolio Manager はStrategyごとの仮想targetを集約し、brokerの目標net exposureを作る。
OMSは現在のbroker exposureとの差分だけを注文する。
既定は1commandにつき1primary strategy deltaとし、複数Strategyのbatchは明示的に行う。

意図的にbatchしたcommandのfillは、volume step単位に量子化した比例配分とlargest remainderで決定する。
端数の順序はstrategy_idで固定し、未配分の残りはpending virtual deltaに残す。
配分規則は取引前に固定し、約定後に都合よく改訂しない。
virtual_positionsは履歴snapshotであり、現在値は最大as_of、同時刻は挿入順seqの新しい行とする。

<a id="s7-2"></a>

### 7.2 同時 signal の裁定

Portfolio Arbitrator はsizingとRisk評価の間で、どの候補を順に評価するかを決める。
期限切れと取引不可を除外し、次の優先度の降順に並べる。

```text
expected_edge_r × confidence
− existing_exposure_penalty_r × 既存bookと同方向のleg数
```

同順位はstrategy_id、symbol、signal_idの順とし、入力の到来順に依存させない。
expected_edge_rはStrategyが推定を持つまで既定1R、expires_atはsignal生成時刻とexpected horizonから導く。
重複factorは `(currency, direction)` で表し、先に受理した候補と同じlegを持つ候補を退ける strongest signal wins を初期policyとする。
risk budget splitはOOS、portfolio backtest、Monte Carloで改善が確認されるまで採用しない。

triangleはInstrumentSpecの通貨から導出し、既存book、当cycleの受理候補、評価候補が持つdistinct symbol数を `max_pairs_per_triangle` 以下に制限する。
penaltyとtriangle上限はconfigで固定し、backtestで校正する。
LLMやruntimeが値を変更しない。

Riskの制限計算は複製せず、受理済み候補を加えたbookからcontextを作ってRiskEngineを順に呼ぶ。
bookへ加える基準はRisk承認ではなくArbitratorの受理である。
そのため先順位がRiskに拒否されても後順位はその候補を含むbookで評価し、実際より厳しい側に判定する。
Exitはrisk reducingなので裁定を経ない。
shadowではArbitratorだけにas-ifの取引許可を渡し、実際のinstrument policyはRiskが報告する。

裁定は受理も却下もarbitration_decisionsに保存する。
`recent()` はrisk decision起点なので、Riskへ届かなかった却下は返さない。
単一銘柄BacktestEngineにはArbitratorを配線しない。
rolling correlationのdynamic redundancyはreturns providerがないため未実装とし、構造的なoverlap penaltyを使う。

実装と検証: [配分](../src/trading/portfolio/allocation.py)、[Arbitrator](../src/trading/portfolio/arbitrator.py)、[配分テスト](../tests/unit/test_allocation.py)、[裁定テスト](../tests/unit/test_arbitrator.py)。

<a id="s8"></a>

## 8. OMS と broker 執行

<a id="s8-1"></a>

### 8.1 口座モード

起動時にMT5のACCOUNT_MARGIN_MODEを読み、NETTING / EXCHANGE / HEDGINGへ写像する。
`broker.expected_account_mode` と一致しなければpreflightのaccount_margin_modeを失敗させ、EXECUTION_DISABLEDにする。
検証済みmodeに応じて、OMSはnetting deltaまたはticket参照のcommand経路を選ぶ。
人の記憶や口座名からmodeを推定しない。

<a id="s8-2"></a>

### 8.2 Fill と broker-side protection

```text
Broker Deal
  → 既知の execution command に一致  → COMMAND_FILL
  → 自システムの position ではない   → UNTRACKED_FILL
  → 既知のpositionで理由がSL/TP/SO   → PROTECTION_FILL
  → その他                          → RECONCILIATION_REQUIRED
```

UNTRACKED_FILLはCRITICALとして新規リスクを停止する。
KPIのuntracked_fill=0は、COMMAND_FILLとPROTECTION_FILLをともに追跡済みとして数える。
commandがないという理由だけでbroker-sideの保護約定を未知のfillへ分類しない。
Micro Live以降、すべての新規positionにbroker-side SLを必須とする。
OPEN_UNPROTECTEDはCRITICALで、repair失敗時はCloseとHALTに進む。

exitはfresh position selectとticket整合を要求し、保護注文が既に決済したpositionへ裸の反対売買を送らない。
positionが見つからない場合は保護約定との競合も照合する。
HEDGINGのexitは対象ticketを参照する。

<a id="s8-3"></a>

### 8.3 状態遷移と claim の回収

```text
CREATED → RISK_APPROVED → READY → CLAIMED → SUBMITTING
        → ACKNOWLEDGED → PARTIAL_FILL → FILLED
異常系: REJECTED / CANCELLED / EXPIRED / UNKNOWN
```

上図は通常経路の要約であり、遷移の可否はstate machineで検査する。
CLAIMEDはbroker副作用がまだない状態、SUBMITTINGは副作用が存在しうる状態として分離する。
claimはPostgreSQLのFOR UPDATE SKIP LOCKEDを使う。
CLAIMEDのleaseが失効し、broker requestが未開始の場合だけREADYへ回収できる。
SUBMITTINGで停止したcommandはUNKNOWNへ送り、READYへ戻さない。
UNKNOWNはbroker履歴とのReconciliationでのみ解決し、再送しない。
Exactly Onceはidempotency、state machine、Reconciliationの組み合わせで近似する。

claimを保持するsave_stateでは、stateに加えclaimed_byとclaim_expires_atをIS NOT DISTINCT FROMで比較する。
比較値は書き込むcommandが持つclaim世代とし、古いworkerの保存はStaleCommandStateErrorにする。
CLAIMED→READYの回収などclaimを解放するcommandはclaim列をNULLにし、stateだけを比較する。

expires_atはexecution_commandsへ永続化してclaim時に復元する（migration 0009）。
queue側に期限を複製せず、commandの値を使う。
生成後の期限はsave_stateで変更しない。

<a id="s8-4"></a>

### 8.4 優先度 queue と送信前確認

DB claim後、SUBMITTINGへ移る前に、CLAIMEDだけをin-memory ExecutionQueueへ載せる。
優先度はemergency、close/reduce、protection repair、new entry、telemetryの5段階とする。
同順位はArbitratorのrank、enqueue連番の順に並べる。

sliding windowでsymbolごとの全requestと全symbol共通のmarket entryを制限する。
初期の毎秒上限はそれぞれ5件と1件で、実値はbroker.rate_limitから読む。
emergencyもbroker上限を迂回しない。

送信前にdispatcher lock、signal expiry、claim lease、rate limit、ticket付きexitのfresh select、pre-trade riskを確認する。
lease切れは送信せず、回収をrecovery sweepへ任せる。
position消滅やriskの不承認、承認数量の縮小はCANCELLED、期限切れはEXPIREDとする。
REJECTEDは作成時のrisk拒否とbroker拒否に予約し、CLAIMEDからのrevalidation拒否に使わない。
risk decisionはdispatch結果へ残す。

broker照会を挟んだ後は時刻を読み直し、送信確定直前にもexpiry、lease、dispatcher lockを検査する。
送信が確定したcommandだけがrate limitの窓を消費する。
workerはDispatch.commandを `save_state(expected_state=CLAIMED)` で永続化してからorder_sendへ進む契約とする。
worker全体の実運用配線とrevalidation用PreTradeContextの構築はM6の残作業であり、queue自体はRisk内部を知らない。

nettingではcurrent netとzero-cross clamp後のresulting netを比較し、結果が0ならCLOSE、絶対値が減るならREDUCEとする。
directionは縮小されるcurrent positionの符号、order sideはdeltaの符号とし、execution_sideと一致させる。
縮小はentry用の毎秒枠を消費しない。
idempotency keyは元intentのactionを使う。
queueでの再deltaは行わず、command_for_nettingのfresh net_exposureに依存する制約を残す。
PROTECTION_REPAIRとTELEMETRYは順位のみが定義され、producerは未実装である。

<a id="s8-5"></a>

### 8.5 単一 dispatcher の保持

ExecutionQueueはDispatchLockを必須とし、dispatchの先頭と送信確定直前でheld()を検査する。
未保持ならDispatcherNotHeldErrorとしてbrokerへ送らない。
processごとに独立したrate limiterで同時送信する構成を許可しない。

PostgresDispatchLockは2引数版pg_try_advisory_lockを使う。
classidはsubsystemの4byte ASCII tagをbig-endian整数にし、短いtagは右側をNULで埋める。
objidはsubsystem内で1から採番し用途を記録する。
OMSはclassid=0x4F4D5300（OMSとNUL）、dispatcherはobjid=1とする。
別subsystemは別tagを使う。

lockの取得と保持確認は、構築時に束縛した同一DB接続で行い、poolから別接続を取り直さない。
held()はpg_locksとpg_backend_pid()を照合し、接続エラーは未保持として扱う。
session lockはcommit/rollbackでは解放されず、接続終了で解放される。
異常切断をserverが認識するまで次のdispatcherは待つ。

送信確定直前に所有権を失ったcommandはCLAIMEDに残し、lease失効後に回収する。
最後の所有権確認からsave_stateとorder_sendまでの間に落ちる窓は残る。
brokerにfencing tokenの受け口がないため、この契約でその窓まで消えるとは扱わない。

実装と検証: [state machine](../src/trading/oms/state_machine.py)、[OMS](../src/trading/oms/service.py)、[queue](../src/trading/oms/queue.py)、[DB保存](../src/trading/storage/postgres.py)、[queueテスト](../tests/unit/test_oms_queue.py)。

<a id="s9"></a>

## 9. Backtest と再現性

StrategyコードはLiveと共有し、datetime.now()を直接呼ばずClockを注入する。
可視化条件はknown_at <= replay_clock.now()である。
実受信によるreplayと、研究用に復元した軸の役割は [§3](#s3) に従う。
実時間の遅着を再現した結果と、復元した市場時刻での研究結果を同一の証拠として扱わない。

Execution SimulatorはBid/Ask/Spread/Latency/Slippage/Partial/Reject/Gap/Protection Fillを扱い、固定spreadを持たない。
stressにはspread x2/x5/x10、fat-tail slippage、reject burst、stop-throughを含める。
carryは [§5](#s5) のPIT snapshot契約を使う。
単一銘柄engineへ未実装の複数銘柄replayやArbitratorの挙動を読み込まない。
入力データ、設定、コード版とseedを記録し、同一入力による判断を再現できるようにする。

<a id="s10"></a>

## 10. 昇格と完成条件

<a id="s10-1"></a>

### 10.1 Micro Live の最低条件

以下は実機と運用の証拠で埋めるチェックリストであり、コードの存在や本書の発行だけでは達成としない。

```text
[ ] Account mode automatically verified     [ ] Broker-side SL verified
[ ] Position OPEN verified                  [ ] Broker-side TP verified
[ ] Partial REDUCE verified                 [ ] PROTECTION_FILL verified
[ ] CLOSE verified                          [ ] Protection/System Exit race tested
[ ] UNKNOWN recovery tested                 [ ] Duplicate command tested
[ ] Claim worker crash tested               [ ] Restart reconciliation tested
[ ] account_snapshots verified              [ ] Daily JST risk tested
[ ] Rolling 24h risk tested                 [ ] HWM drawdown tested
[ ] Tick persistence verified               [ ] Replay deterministic
[ ] Shadow result available
```

<a id="s10-2"></a>

### 10.2 非USDJPYペアの gate

昇格順はUSDJPY → EURUSD → GBPUSD → GBPJPYとする。
EURUSDを2本目のlive pairとして有効にする前にv2.0を発行する。
発行は取引許可ではなく、instrumentごとの設定、gateの判断、運用開始は別の操作とする。
ScalpingはRESEARCH_ONLYを維持する。

#57のVPS多ペア収集と履歴、#59のGBP/EURデータ調達、#60のswap実測、#66のOOS、Monte Carlo、shadow、demo forward、small liveの検証は残る。
policy-pathやPIT provenanceが不足した入力を、confidenceの表示だけでlive品質へ昇格させない。
GBPJPYは単体PFだけでなく、portfolioへの限界寄与（Sharpe、return/DD、集中度）で採否を判断する。
4ペアすべてで利益を出すことをplatformの完成条件にはしない。

<a id="s10-3"></a>

### 10.3 完成の定義

Platform Operational Completionは利益ではなく、同一入力なら同じ判断、重複positionなし、無保護live positionなし、説明できないposition/fillなし、盲目的再送なし、意図しない反転なし、look-aheadなしを満たすこととする。
すべてのRisk判断を監査でき、取引を帰属でき、restartが安全であることを要求する。

その後のBacktest、Out-of-Sample、Walk Forward、Shadow、Micro Liveの結果でStrategy Edgeを判定する。
有料データやインフラ増強はedgeに基づくupgradeであり、architecture完成の条件ではない。
Data Upgrade Gateは期待増分価値 > 2 × データコストとする。

<a id="s11"></a>

## 11. ADR の移管先

以下のADRは規範を本書へ移管し、本文を履歴として保持する。
古い本文にある「後続で実装予定」は当時の状態であり、本書では後続ADRに従って更新済みの規則と現存制約を区別する。

| ADR | 現行規範の移管先 | 後続決定と制約 |
| --- | --- | --- |
| [ADR-001](adr/ADR-001-account-mode.md) | [§8.1](#s8-1) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-002](adr/ADR-002-broker-protection.md) | [§8.2](#s8-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-003](adr/ADR-003-netting-allocation.md) | [§7.1](#s7-1) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-004](adr/ADR-004-risk-day.md) | [§4.3](#s4-3) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-005](adr/ADR-005-bar-visibility-clock.md) | [§3.1](#s3-1)、[§3.2](#s3-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-006](adr/ADR-006-long-timeframe-grid.md) | [§3.2](#s3-2)、[§3.3](#s3-3) | 時計逆行の未対応は ADR-033 の規則と現存制約へ更新。 |
| [ADR-007](adr/ADR-007-instrument-base-quote-currency.md) | [§4.1](#s4-1) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-008](adr/ADR-008-strict-money-type.md) | [§4.1](#s4-1)、[§4.2](#s4-2) | 換算の適用範囲は ADR-009/010 の決定へ更新。 |
| [ADR-009](adr/ADR-009-conversion-use-time-staleness.md) | [§4.2](#s4-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-010](adr/ADR-010-exit-not-blocked-by-conversion.md) | [§4.2](#s4-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-011](adr/ADR-011-position-limits-per-symbol-and-portfolio.md) | [§4.3](#s4-3) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-012](adr/ADR-012-platform-enabled-vs-trading-enabled.md) | [§4.4](#s4-4)、[§6.4](#s6-4) | shadow 配線の保留は ADR-027 で更新。 |
| [ADR-013](adr/ADR-013-portfolio-stop-risk-and-currency-exposure.md) | [§4.3](#s4-3)、[§7.2](#s7-2) | triangle の独立制約は ADR-029 の裁定段階へ追加。 |
| [ADR-014](adr/ADR-014-reconstructed-replay-axis.md) | [§3.4](#s3-4) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-015](adr/ADR-015-gbp-eur-official-data-acquisition.md) | [§5.1](#s5-1)、[§5.2](#s5-2)、[§5.9](#s5-9)、[§10.2](#s10-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-016](adr/ADR-016-swap-rollover-pit-broker-cost.md) | [§5.11](#s5-11) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-017](adr/ADR-017-currency-scoped-event-risk.md) | [§5.9](#s5-9)、[§5.6](#s5-6) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-018](adr/ADR-018-currency-first-state-and-score-normalization.md) | [§5.4](#s5-4)、[§5.5](#s5-5)、[§5.6](#s5-6)、[§5.7](#s5-7)、[§5.8](#s5-8) | POLICY は ADR-021、供給は ADR-019/022、鮮度は ADR-025 へ更新。 |
| [ADR-019](adr/ADR-019-macro-series-to-factor-input.md) | [§5.4](#s5-4)、[§5.5](#s5-5) | RATES と POLICY は ADR-020/021 へ更新。JPY factor 未接続は維持。 |
| [ADR-020](adr/ADR-020-gbp-eur-rates-proxy.md) | [§5.2](#s5-2)、[§5.4](#s5-4) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-021](adr/ADR-021-policy-factor-bounded-scale.md) | [§5.4](#s5-4)、[§5.5](#s5-5) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-022](adr/ADR-022-currency-state-reaches-the-strategy.md) | [§5.10](#s5-10)、[§5.7](#s5-7)、[§5.8](#s5-8) | 鮮度の既知の不具合は ADR-025 で更新。 |
| [ADR-023](adr/ADR-023-per-instrument-parameters-and-spread-session-gates.md) | [§6.1](#s6-1)、[§6.2](#s6-2)、[§6.3](#s6-3) | entry gate は ADR-028、exit 継続は ADR-031 へ更新。 |
| [ADR-024](adr/ADR-024-sessions-independent-of-mt5-server-timezone.md) | [§6.2](#s6-2) | Session VWAP の呼出側制約は #100 として維持。 |
| [ADR-025](adr/ADR-025-freshness-follows-series-cadence.md) | [§5.7](#s5-7) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-026](adr/ADR-026-jgb-2y-yield-pit-bound.md) | [§5.3](#s5-3) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-027](adr/ADR-027-multi-symbol-shadow-cycle.md) | [§6.4](#s6-4) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-028](adr/ADR-028-session-profile-entry-gate.md) | [§6.2](#s6-2)、[§6.3](#s6-3) | session 外での全評価停止は ADR-031 の exit 継続へ更新。 |
| [ADR-029](adr/ADR-029-portfolio-arbitrator-owns-signal-selection.md) | [§7.2](#s7-2) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-030](adr/ADR-030-oms-priority-queue-and-send-time-revalidation.md) | [§8.4](#s8-4)、[§8.5](#s8-5) | dispatcher、送信直前確認、期限永続化は ADR-032 へ更新。worker 全体の配線は未完了。 |
| [ADR-031](adr/ADR-031-exit-only-signal-through-session-gate.md) | [§6.3](#s6-3) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-032](adr/ADR-032-oms-worker-send-path-contract.md) | [§8.3](#s8-3)、[§8.4](#s8-4)、[§8.5](#s8-5) | 決定と現存する制約を移管。背景と過去の実測は ADR に保持。  |
| [ADR-033](adr/ADR-033-bar-bucket-clock-step.md) | [§3.3](#s3-3) | 保存系列、setup_id、anchor 実測の制約を維持。 |

[ADR-035](adr/ADR-035-system-spec-v2-consolidation.md) は本統合の承認と発行条件を定める採用済みの手続き上の決定であり、上表の履歴化対象には含めない。
