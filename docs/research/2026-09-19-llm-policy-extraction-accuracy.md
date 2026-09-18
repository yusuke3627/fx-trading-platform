# LLMによる政策文書抽出の精度測定（段階1・事前登録）

- 日付: 2026-09-19
- 対象: `config/policy_meetings.yaml` の verified な42件（BOJ 21 / FED 21、2024-03-19〜2026-09-18）
- 判定: **人手転記に匹敵する。段階2へ進める。** 比較できた166フィールドのうち165が一致（99.4%）。唯一の不一致はBOJ 2026-07-31の`explicit_future_hike_language`で、人手の`false`が正しく抽出が誤り。42件すべて取得・抽出に成功。費用は$0.0235、所要25分（`gpt-5.6-luna`、Batch）
- 関連: [政策スコアの研究方針](2026-08-15-pit-macro-policy-intervention.md)、[設計の正本](../SYSTEM_SPEC.md)、[正解の定義](../../src/trading/data/policy/meetings.py)、[測定ハーネス](../../src/trading/data/policy/extraction_study.py)

## 目的と範囲

TradingAgentsはフレームワークとして採用しない。LLMに売買判断や執行権限を与える構造は、このプロジェクトの境界に合わない。採用を検討するのは「原文から構造化イベントの材料を抽出する」部分だけである。

年8回の政策会合だけなら、人手転記の方が信頼できる。LLM抽出の意義は、Summary of Opinions、Minutes、記者会見、発言など人手で追えない分量へ対象を広げられるかにある。段階1では、その前に既知の正解に対する抽出精度を測る。段階2の対象文書拡張と段階3の増分価値測定は別の変更として扱う。

本ハーネスはオフライン研究専用である。`scoring.py`、`intelligence/llm.py`、正解YAML、DB・Strategy・OMSは変更せず、スコアも出力させない。Data Upgrade Gate（期待増分価値 > 2 × データコスト）は段階3の判断に使い、段階1用の合格率を新設しない。

## 対象と入力の事前確認

計画にあった「2026-12まで」は会合日程の収録範囲だった。既存ローダが返す実際の採点済み会合は2026-09-18までの42件で、すべてverifiedだった（実装開始時のHEAD: `8abf5a8`）。日程の将来会合を正解データへ追加しない。

`source_uri` は政策声明を指し、BOJがHTML 3件・PDF 18件、FEDがHTML 21件。両銀行とも、1回の抽出に「対象会合の声明＋corpus内の同じ銀行の直前会合の声明」の2文書を入力する。YAMLの並び順ではなく会合日順で直前を選び、前回声明の公表時刻が対象声明より過去であることも確認する。corpusの外へ取りに行ったり、文書内のリンクを巡回したりしない。

`inflation_forecast_change` は同一年度の今回・前回の中央値を比較する。SEP・展望レポートの原文を入力していないため、42件とも入力不足として分離する。見通し非公表回の0も「声明に改定が書かれていない」だけでは確定しない。YAMLの期待値が0かどうかで除外を決めない。

BOJの声明は、新しい金融市場調節方針の水準を記載する形式で、単独では変更幅が一意に取れるとは限らない。例えば[2024-07-31の声明](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k240731a.pdf)は変更後の水準を記載するが、前回水準を併記しない。[2024-10-31の声明](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k241031a.pdf)も水準だけである。金利変更は既存の機械採点で±2.0と最大の重みを持つため、半数を評価できないままでは段階1の主要な問いに答えられない。前回声明を加えて、新旧の決定金利の差を原文から求める。モデルの訓練データから前回水準を思い出させないための入力であり、FEDにも同じ規則を適用する。

corpus内に前回会合がない**BOJ 2024-03-19とFED 2024-03-20だけは、対象声明1文書を入力し、rate_change_bpを入力不足として残す**。この2件では対象声明に変更幅の明記があっても、金利変更幅を評価しない。残る40件（BOJ 20 / FED 20）は2文書の比較対象とする。

プロンプトと各文書の直前のラベルで、銀行コード・会合日と「対象会合」「前回会合」を明示する。**5フィールドはすべて対象会合について返し、前回声明は金利水準の比較にのみ使う。** 反対票数、物価見通し、将来の利上げ文言を前回会合から転記しない。見通し資料の公表予定やリンクが声明にあっても、今回・前回のSEPや展望レポート本文を入力したことにはならない。

入力不足はモデルに判定させない。送信前に参照先の種類と会合日を照合し、会合ごとの除外理由と前回声明の参照先をmanifestへ保存する。未確認のURL形式では実行を止め、資料の種類を確認する。対象・前回のそれぞれについて実形式と原文のSHA-256も保存する。同じ声明は1回だけ取得し、対象用・前回用で同じバイト列を使う。

| フィールド | 予定対象数 | 入力が揃う集合 | 入力不足 |
|---|---:|---|---:|
| rate_change_bp | 42 | 40件（BOJ 20 / FED 20） | 2件（各銀行の先頭1件） |
| hawkish_dissents | 42 | BOJ・FED 42件 | 0 |
| dovish_dissents | 42 | BOJ・FED 42件 | 0 |
| inflation_forecast_change | 42 | 0件 | 42 |
| explicit_future_hike_language | 42 | BOJ・FED 42件 | 0 |

以上は取得成功前の予定数である。実行時に取得失敗やAPI失敗があれば、実際に比較できた件数は減る。対象または前回声明の取得に失敗した会合は送信せず、どちらの取得が失敗したかを記録する。取得失敗を「前回会合がcorpusにない」と読み替えて分母を減らさず、より古い会合の声明にも差し替えない。失敗の影響は、その原文を対象または前回として必要とする会合に限る。利上げ反対・利下げ反対の方向は対象会合の決定金利との比較で判定し、QT・買入れ・声明文言への反対を含めない。利上げの明示は対象声明本文だけを対象とし、条件付きの明示は含めるが、一般的な調整の可能性は含めない。

### 参照先一覧

下表は事前登録時に整理した参照先である。**実測ではハーネスが42件すべての取得に成功した**（HTML 24件・PDF 18件、取得失敗0件）。事前登録の段階ではBOJ 2026-09-18をWebツールのエラーで確認できずに残していたが、実測時の取得で解消した。

| bank | decision_date | source_uriの形式 | 前回会合の日付（同じ銀行） | 評価から分離するフィールド | 今回の確認 |
|---|---|---|---|---|---|
| BOJ | 2024-03-19 | [HTML](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240319a.htm) | なし（corpus先頭） | 物価見通し・金利変更幅 | Webで到達確認 |
| FED | 2024-03-20 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240320a.htm) | なし（corpus先頭） | 物価見通し・金利変更幅 | Webで到達確認 |
| BOJ | 2024-04-26 | [HTML](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240426a.htm) | 2024-03-19 | 物価見通し | Webで到達確認 |
| FED | 2024-05-01 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240501a.htm) | 2024-03-20 | 物価見通し | Webで到達確認 |
| FED | 2024-06-12 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240612a.htm) | 2024-05-01 | 物価見通し | Webで到達確認 |
| BOJ | 2024-06-14 | [HTML](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240614a.htm) | 2024-04-26 | 物価見通し | Webで到達確認 |
| BOJ | 2024-07-31 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k240731a.pdf) | 2024-06-14 | 物価見通し | Webで到達確認 |
| FED | 2024-07-31 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240731a.htm) | 2024-06-12 | 物価見通し | Webで到達確認 |
| FED | 2024-09-18 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm) | 2024-07-31 | 物価見通し | Webで到達確認 |
| BOJ | 2024-09-20 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k240920a.pdf) | 2024-07-31 | 物価見通し | Webで到達確認 |
| BOJ | 2024-10-31 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k241031a.pdf) | 2024-09-20 | 物価見通し | Webで到達確認 |
| FED | 2024-11-07 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20241107a.htm) | 2024-09-18 | 物価見通し | Webで到達確認 |
| FED | 2024-12-18 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20241218a.htm) | 2024-11-07 | 物価見通し | Webで到達確認 |
| BOJ | 2024-12-19 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2024/k241219a.pdf) | 2024-10-31 | 物価見通し | Webで到達確認 |
| BOJ | 2025-01-24 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250124a.pdf) | 2024-12-19 | 物価見通し | Webで到達確認 |
| FED | 2025-01-29 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250129a.htm) | 2024-12-18 | 物価見通し | Webで到達確認 |
| BOJ | 2025-03-19 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250319a.pdf) | 2025-01-24 | 物価見通し | Webで到達確認 |
| FED | 2025-03-19 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250319a.htm) | 2025-01-29 | 物価見通し | Webで到達確認 |
| BOJ | 2025-05-01 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250501a.pdf) | 2025-03-19 | 物価見通し | Webで到達確認 |
| FED | 2025-05-07 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250507a.htm) | 2025-03-19 | 物価見通し | Webで到達確認 |
| BOJ | 2025-06-17 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250617a.pdf) | 2025-05-01 | 物価見通し | Webで到達確認 |
| FED | 2025-06-18 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250618a.htm) | 2025-05-07 | 物価見通し | Webで到達確認 |
| FED | 2025-07-30 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250730a.htm) | 2025-06-18 | 物価見通し | Webで到達確認 |
| BOJ | 2025-07-31 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250731a.pdf) | 2025-06-17 | 物価見通し | Webで到達確認 |
| FED | 2025-09-17 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20250917a.htm) | 2025-07-30 | 物価見通し | Webで到達確認 |
| BOJ | 2025-09-19 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k250919a.pdf) | 2025-07-31 | 物価見通し | Webで到達確認 |
| FED | 2025-10-29 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20251029a.htm) | 2025-09-17 | 物価見通し | Webで到達確認 |
| BOJ | 2025-10-30 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k251030a.pdf) | 2025-09-19 | 物価見通し | Webで到達確認 |
| FED | 2025-12-10 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20251210a.htm) | 2025-10-29 | 物価見通し | Webで到達確認 |
| BOJ | 2025-12-19 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2025/k251219a.pdf) | 2025-10-30 | 物価見通し | Webで到達確認 |
| BOJ | 2026-01-23 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260123a.pdf) | 2025-12-19 | 物価見通し | Webで到達確認 |
| FED | 2026-01-28 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260128a.htm) | 2025-12-10 | 物価見通し | Webで到達確認 |
| FED | 2026-03-18 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm) | 2026-01-28 | 物価見通し | Webで到達確認 |
| BOJ | 2026-03-19 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260319a.pdf) | 2026-01-23 | 物価見通し | Webで到達確認 |
| BOJ | 2026-04-28 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260428a.pdf) | 2026-03-19 | 物価見通し | Webで到達確認 |
| FED | 2026-04-29 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm) | 2026-03-18 | 物価見通し | Webで到達確認 |
| BOJ | 2026-06-16 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260616a.pdf) | 2026-04-28 | 物価見通し | Webで到達確認 |
| FED | 2026-06-17 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260617a.htm) | 2026-04-29 | 物価見通し | Webで到達確認 |
| FED | 2026-07-29 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm) | 2026-06-17 | 物価見通し | Webで到達確認 |
| BOJ | 2026-07-31 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260731a.pdf) | 2026-06-16 | 物価見通し | Webで到達確認 |
| FED | 2026-09-16 | [HTML](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm) | 2026-07-29 | 物価見通し | Webで到達確認 |
| BOJ | 2026-09-18 | [PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260918a.pdf) | 2026-07-31 | 物価見通し | Web取得エラー（本文未確認） |

## API経路と抽出形式

2026-09-19にcontext7でOpenAI APIの資料を検索し、OpenAI公式ドキュメント本文で次を確認した。

- [Batch API](https://developers.openai.com/api/docs/guides/batch): `/v1/responses` を利用でき、各行のbodyは通常のResponses APIと同じパラメータを使う。結果順は保証されない。
- [File inputs](https://developers.openai.com/api/docs/guides/file-inputs): Responses APIのPDFは`input_file`に`file_id`を渡せる。vision対応モデルでは抽出テキストとページ画像が入力される。
- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna): 指定モデルはtext/image入力、Batch、Structured Outputsに対応する。
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): `text.format` のjson_schemaとstrictを使う。全フィールドrequired、`additionalProperties: false`にする。

採用経路は、PDFをFiles APIへ`purpose="user_data"`でアップロードし、取得したfile_idをResponsesリクエストへ入れ、JSONLを`purpose="batch"`でアップロードする方式である。`batches.create(endpoint="/v1/responses", completion_window="24h")`へ渡す。PDFはローカルでテキスト化せず、原本を渡して脚注や表の構造を残す。HTMLはscript・style・headを除き、本文・脚注をテキストにする。対象と前回で形式が異なっても、それぞれの形式に合わせた入力を送る。同じPDFのfile_idは対象用・前回用で再利用し、1リクエストのPDF合計が50 MB未満であることも確認する。APIに外部URLを取りに行かせないので、取得失敗を手元で会合ごとに記録できる。

モデルの既定は`gpt-5.6-luna`、reasoning effortはmedium、最大出力は4096 tokens。モデルIDだけはCLIで変更できる。モデルを変更した測定は別runに保存し、主測定と混ぜない。ブラウザや執行ツールをLLMへ与えず、正解値・YAMLのコメントも送信しない。

出力キーは指定の5つだけとする。入力が揃う欄は指定のint / bool型で強制し、**入力不足の欄だけはスキーマでnullに固定**する。このnullは抽出値ではなく、評価対象外を表すハーネスの記録である。0やfalseで埋めると正解と一致したように見えるため使わない。受信側でも型・範囲・欠損・余分なキー・nullの位置を検証する。拒否応答、出力打ち切り、不正JSONは抽出失敗とする。

## 測定と照合の事前登録

1. 既存ローダで正解を読み、全件verified・重複なしを確認する。同じ銀行の会合を日付順に並べて直前会合を選び、公表時刻の前後関係を確認する。正解ラベル、対象・前回の文書範囲、モデル、単価、開始時刻、YAMLハッシュをrunのmanifestへ固定する。
2. corpusの42文書を取得し、空・形式違い・HTMLエラーページ・PDF途中切れを検出する。2文書ずつ使っても取得対象を84文書に増やさない。原文はURLのハッシュをキーに`tmp/policy-extraction/cache`へ保存する。既存キャッシュを再利用し、再取得したい場合は別の`--cache-dir`を使う。
3. 必要な声明の取得に成功した会合を1つのBatchへ送信する。各銀行の先頭会合は対象声明だけ、残りは対象と直前会合の2文書を送る。42件すべてを予定対象に残し、対象または前回声明の取得失敗は送信せず失敗一覧に載せる。Batch作成を自動リトライしない。
4. `custom_id = bank-decision_date`で応答を照合する。出力ファイルとエラーファイルの両方を保存する。未知・重複IDは不正な出力として停止する。欠落IDはその会合の抽出失敗とする。
5. Batch全体が`completed`の場合だけ、完了した個別応答の型検証を通った値を比較する。`failed / expired / cancelled`の部分出力は成功数へ入れない。`validating / in_progress / finalizing / cancelling`は未完了として待ち、待機上限に達したら再開方法を表示する。
6. フィールドごとに次を出す。分母が0なら率はnull（算出不能）とし、0%や100%と表示しない。

| 出力 | 定義 |
|---|---|
| total | 予定対象の全会合数 |
| input_insufficient | そのフィールドの資料が不足している会合数 |
| eligible | total − input_insufficient |
| compared | eligibleのうち取得・API・型検証が成功し、比較できた数 |
| matched / mismatched | 比較した値が正解と一致 / 不一致だった数 |
| fetch_failed / extraction_failed | eligibleのうち取得 / 抽出に失敗した数 |
| match_rate | matched / compared |
| coverage | compared / eligible |
| matched_over_eligible | matched / eligible（処理失敗を分母に残す到達率） |

各フィールドで`total = input_insufficient + eligible`、`eligible = matched + mismatched + fetch_failed + extraction_failed`が成り立つ。取得失敗は入力不足のある会合でも会合単位の失敗一覧に残す。不一致一覧はbank、decision_date、field、expected、extractedを含む。入力不足一覧にはbank、decision_date、field、理由を含む。

不一致を見てからプロンプトや除外条件を調整した結果は、初回の成績へ上書きしない。初回結果を保存し、別runの探索的測定として扱う。この42件の一致だけで未収集文書への汎化性能や取引の増分価値が証明されたとは判断しない。

## 使用量と概算費用

`usage.input_tokens / output_tokens`と、`input_tokens_details.cached_tokens`を記録する。拒否や未完了の応答も使用量があれば費用へ含める。usageを得られないリクエスト数を別記し、その場合は費用を確認できた分の小計として扱う。

[公式料金表](https://developers.openai.com/api/docs/pricing)で確認したLunaのBatch単価は、100万tokenあたり入力$0.10、キャッシュ入力$0.01、出力$0.60（通常料金の50%）。概算は`((入力 − キャッシュ) × 0.10 + キャッシュ × 0.01 + 出力 × 0.60) / 1,000,000`で、Decimalで計算する。1リクエストの入力が272,000 tokensを超える場合、入力・キャッシュは2倍、出力は1.5倍を適用する。PDF画像の処理分もAPIのusageを使う。

別モデルにLunaの単価を流用しない。別モデルで単価を指定しなければトークン数だけを示し、概算費用はnullとする。`--input-price / --cached-input-price / --output-price`は3種類まとめてBatch適用後の単価を渡す。別モデルの文脈長別料金は自動判定しない。請求額の確定値ではなく、返されたusageに基づく推定である。

## 実行・再開

リポジトリルートから実行する。OPENAI_API_KEYはOpenAI APIプラットフォームの環境変数を使い、Codexのサブスク認証は使わない。キー未設定なら、原文取得やrun作成より前に明確なエラーで終了する。import・照合テスト・`--help`にはキーもSDKも必要ない。

```bash
.venv/bin/pip install -e '.[dev,db,llm]'
.venv/bin/python -m trading.data.policy.extraction_study \
  --run-dir tmp/policy-extraction/luna-01
```

初回は最大24時間、60秒おきに状態を確認する。短く待ちたい場合は`--wait-seconds 60`を付ける。未完了・通信中断後は、保存されたBatchを再送せず結果取得だけ再開する。

```bash
.venv/bin/python -m trading.data.policy.extraction_study \
  --resume tmp/policy-extraction/luna-01
```

runには`manifest.json`、`input.jsonl`、`batch.json`、取得した出力・エラーJSONL、`report.json`を保存する。結果は標準出力にもJSONで表示する。resumeでは保存したラベル・モデル・単価を使い、現在のYAMLで採点し直さない。既存runの上書きは許可しない。

Batch作成の通信応答を失い`batch.json`がない場合は、このresumeは使えない。新規送信する前にOpenAI側で作成済みBatchを確認する。アップロードしたファイルは自動削除しない。出力はOpenAI側の保持期限までに回収する（公式Batchガイドは完了後30日と記載）。

終了コード0は、Batchがcompletedで全会合の取得・抽出に成功したことを示す。値の不一致や入力不足がないという意味ではない。取得・抽出失敗、Batch未完了は1、キー未設定などCLI引数の問題は2。

## 結果

**実施済み。** 事前登録した手順をそのまま適用した。閾値・プロンプト・入力範囲は測定後に変えていない。

- モデル: `gpt-5.6-luna` / Batch ID: `batch_6aad69acdd8c8190a241299fde17b2ef` / エンドポイント: `/v1/responses`
- Batch 状態: `completed`。42件すべて取得・抽出に成功（取得失敗0、API失敗0、`custom_id` 欠落0）
- 経過: 1,505秒（約25分）
- 使用量: 入力 203,907 token / 出力 5,254 token / キャッシュ 0
- 費用: **$0.0235**（Batch適用後の単価 入力$0.10・キャッシュ$0.01・出力$0.60 / 100万token）。全42件の使用量が取れているため総計であり小計ではない

| フィールド | 入力が揃う | 比較 | 一致 | 不一致 | 入力不足 | 一致率 |
|---|---:|---:|---:|---:|---:|---:|
| rate_change_bp | 40 | 40 | 40 | 0 | 2 | 100.0% |
| hawkish_dissents | 42 | 42 | 42 | 0 | 0 | 100.0% |
| dovish_dissents | 42 | 42 | 42 | 0 | 0 | 100.0% |
| inflation_forecast_change | 0 | 0 | 0 | 0 | 42 | 測定せず |
| explicit_future_hike_language | 42 | 42 | 41 | 1 | 0 | 97.6% |

比較できたのは166フィールドで、165が一致した（99.4%）。coverage は測定した4フィールドとも1.0で、「比較できた分だけの一致率」が失敗を隠していない。

### 唯一の不一致

BOJ 2026-07-31 の `explicit_future_hike_language`。人手転記が `false`、抽出が `true`。

原文（[k260731a.pdf](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260731a.pdf)）を確認したところ、**人手転記が正しい**。この声明は最小形式で、無担保コールレートを1.0%程度に据え置く決定（賛成8反対1）、反対した高田委員の議案、出席者、公表予定しか載っていない。日本銀行自身による今後の利上げへの言及はない。

抽出が `true` を返した原因は、反対意見の記述を将来の利上げの明示と読んだことにあると考えられる。高田委員は1.25%程度への引き上げを提案して否決されており、その理由に「機動的な対応が必要な新たな局面」という文言がある。事前登録したプロンプトは参照範囲を当日声明本文に限り、別資料の文言を除いたが、**否決された反対議案を本文から除く指示は書いていなかった**。

これは確率的な揺らぎではなく、指示の欠落として特定できる1件である。ただし**このPRではプロンプトを直さない。** 正解データを見てから指示を足して測り直すのは、テスト集合への当てはめになる。修正案は新しい実験として扱い、別の会合集合か、拡張後の文書集合で測る。

### 入力不足として分離した項目

事前登録どおり、`inflation_forecast_change` 42件と `rate_change_bp` 2件（BOJ 2024-03-19・FED 2024-03-20）をモデルに推測させず `null` で返させた。抽出側がこれに違反して値を埋めた例はなかった。前者はSEP・展望レポート本文を入力していないため、後者はcorpus内に比較する前回声明がないためで、いずれも精度の問題ではない。

### レビューを受けて足した歯止め

`--meetings` に会合が途中で抜けた部分コーパスを渡すと、ハーネスは抜けの前の古い会合を「直前会合」として採用しうる。そうなるとモデルには複数会合分の金利差を計算させながら、単一会合の正解と突き合わせることになり、一致・不一致のどちらも誤って報告される。

隣接判定を足した。BOJ・FEDとも定例は年8回で、隣接する会合の間隔は長くても8週ほどである。間に1回抜けると最短でも10週まで開くので、9週（63日）をその空白帯の境にした。これを超える間隔の会合は `rate_change_bp` を入力不足へ倒す。疑わしいときにデータ点を1つ失う側へ倒しており、誤った一致率を出す側へは倒していない。

**直前と確かめられない会合は、入力にも依存にも使わない。** 最初の修正は `rate_change_bp` を入力不足にするだけで、非隣接の古い声明を `previous_statement` に残していた。これには2つの問題がある。送ればモデルには「前回会合」と偽ることになり、さらにその古い声明の取得に失敗すると、対象声明だけで測れる反対票数や利上げ文言まで巻き込んで `fetch_failed` になる。非隣接と判定した時点で参照自体を作らず、corpus内に前回会合がない先頭の会合と同じ扱いに揃えた。

**この歯止めは今回の測定結果を変えない。** 実測に使った42件の会合間隔は、BOJが35〜55日、FEDが41〜50日で、63日を超える箇所はない。すべての「前回」が真の直前会合だった。

### この結果が意味すること

年16回の政策声明について、この抽出は人手転記と同じ値を返す。ただし段階1の目的は転記の自動化ではない。`meetings.py` が自動パースをスコープ外にした理由は「この分量なら人手が最も信頼できるパーサだから」という量の議論であり、その判断はこの結果でも変わらない。

得られたのは、**同じ抽出器を人手で追えない分量の文書（Summary of Opinions・Minutes・記者会見・総裁発言）へ向ける根拠**である。42件2文書・約20万tokenで$0.0235という実測値は、段階3で Data Upgrade Gate（期待増分価値 > 2 × データコスト）を評価するときのコスト側の実数になる。

段階2（未収集文書のPIT収集）と段階3（新featureのablation）は別の変更として扱う。本PRはランタイムに接続していない。
