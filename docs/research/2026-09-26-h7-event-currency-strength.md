# H7: 米国指標の発表後、ドル全面の動きは続くか（探索期間の事前登録）

- 登録日: 2026-09-26。**この文書の「結果」より上と、Plan・イベント一覧だけを測定前に単独でコミットし、タグ `prereg/h7-explore` を打つ**
- 判定（測定後に追記）: 未測定
- 設計と判定規則の正本: [設計ノート](2026-09-24-h7-event-currency-strength-design.md)（PR #234）。本文書は、設計ノートを実データに当てるときに固定する値と手順だけを書く
- ハーネス: [event_currency_strength_study.py](../../src/trading/backtest/event_currency_strength_study.py)
- 関連: Issue #157 の候補 3

## 固定するもの

| 対象 | 置き場所 | sha256 |
| --- | --- | --- |
| Plan | [2026-09-25-h7-event-currency-strength.plan.json](2026-09-25-h7-event-currency-strength.plan.json) | `4419cc67adf0a07d50b5188357cbd154e8bb1b898da69cc950fe59ab066517c3` |
| イベント一覧 | [2026-09-25-h7-event-currency-strength.events.json](2026-09-25-h7-event-currency-strength.events.json) | `61d3e37f23293324f413b3bdb4e098ee1010b9f2c5b31f74063fd51273d4ed67` |
| 探索期間の tick | `tmp/h7/explore-ticks.csv.gz`（大きいのでコミットしない） | `6ea8ef5d4254fa88324488adb26e93b67bc0f023a81e11cc04ed21c9083a6866` |

- ハーネスのコミット: `870cf5f8f0d47086c2dfc60f069dc75f6e13520a`（本文書を足すコミットの親）
- tick の書き出し元: Mac の研究用 DB（`trading_research`）。VPS から 2026-09-26 に同期したもの（同期の天井 `id <= 184294371`、5,648 万行）。書き出しの天井は `id <= 184294368`、行数は USDJPY `9,134,100`、EURUSD `4,915,426`
- 探索期間の USD/JPY は OANDA の MT5 系列、EUR/USD は Dukascopy の系列である（設計ノートの「期間とデータ」）

### イベントと対の数

| 段階 | イベント | プラセボ候補がある | 取り込み・書き出しの窓 |
| --- | ---: | ---: | ---: |
| 探索（2024-08-01〜2026-08-28） | 79 | 79 | 212 日 |
| 確認（2022-01-01〜2024-07-22） | 101 | 99 | 278 日 |

確認の段階の数は、確認のためのユーロドルの取り込みに使う窓の一覧として、ここで固定する。

## 設計ノートを実データに当てるときの決めごと

- **除外日**は、為替介入の 7 日（2022-09-22、2022-10-21、2022-10-24、2024-04-29、2024-05-01、2024-07-11、2024-07-12）。イベントにもプラセボ候補にも当てる
- **プラセボ候補から外す「発表日」**は、設計ノートの定義のイベント日（4 系列のどれかで新しい観測期間が初めて公表された日）とする。
  段階を問わず全期間のイベント日を使う。改定だけの公表日（GDP の改定値など）は外さない。プラセボ側に米国のニュースが混じる向きの
  違いなので、支持しにくい方向にだけ働く
- **プラセボ候補は、各段階の期間の内側に限る。** 探索の最初のイベント（2024-08 初め）の 1 週間前は期間の外なので、1 週間後の候補を使う
- 探索では `confirm_extra_cost_pips` を使わない（Plan では 0）

## 確認期間のコストの上乗せ（規則だけを今固定する）

確認期間の USD/JPY は Dukascopy の気配で、OANDA とはスプレッドが違う。副の規則の損益は、建てと決済でそれぞれスプレッドの
半分ずつを払うので、1 取引あたりの差は次の値になる。

```
上乗せ = max(0, ((OANDA の post の中央値 + OANDA の end の中央値) − (Dukascopy の post の中央値 + Dukascopy の end の中央値)) / 2)
```

- OANDA の中央値は、探索期間の tick から `spreads --stage explore` で求める
- Dukascopy の中央値は、確認期間の tick を書き出した後、`spreads --stage confirm` で求める。`spreads` はリターンも統計量も計算しない
- 算出は確認の測定の直前に機械的に行い、値を変えた確認用の Plan（この Plan と `confirm_extra_cost_pips` だけが違う）を、
  2 つ目の事前登録として測定前にコミットしてタグを打つ

## 測定の手順

```sh
TRADING_DB_DSN=postgresql://localhost/trading_research \
  python -m trading.backtest.event_currency_strength_study events \
  --plan docs/research/2026-09-25-h7-event-currency-strength.plan.json \
  --output docs/research/2026-09-25-h7-event-currency-strength.events.json
TRADING_DB_DSN=postgresql://localhost/trading_research \
  python -m trading.backtest.event_currency_strength_study export \
  --plan docs/research/2026-09-25-h7-event-currency-strength.plan.json \
  --events docs/research/2026-09-25-h7-event-currency-strength.events.json \
  --stage explore --output tmp/h7/explore-ticks.csv.gz
# ここまでの出力の sha256 を上の表に記録し、本文書と Plan・イベント一覧をコミットしてタグを打つ
python -m trading.backtest.event_currency_strength_study measure \
  --plan docs/research/2026-09-25-h7-event-currency-strength.plan.json \
  --events docs/research/2026-09-25-h7-event-currency-strength.events.json \
  --ticks tmp/h7/explore-ticks.csv.gz --stage explore --output-dir tmp/h7/explore-run-1
```

測定は、タグを打ったコミットで作業ツリーに差分がない状態（report の `git_dirty` が false）で 1 回だけ行う。

## 結果の読み方（事前に決めておく）

- 判定の規則は設計ノートのとおり。探索では、プラセボと対になったイベントで `A` と `C` の点推定がどちらも 0.10 以上なら
  「確認へ進む」、それ以外は「止める」
- 「止める」なら候補 3 はここで止め、確認期間のユーロドルは取り込まない
- 探索の期間は H5 が発表後のドル円を使っている。探索の結果は確認へ進むかの根拠にだけ使い、それ自体を支持の証拠とは呼ばない
- 探索期間の OANDA のスプレッドの分布（`spreads --stage explore`）も測定後に記録する。これは上のコストの規則の入力で、判定には使わない

## 結果
