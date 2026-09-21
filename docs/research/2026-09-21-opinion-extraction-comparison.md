# 政策意見の抽出比較ツール

Issue #199。BOJ「主な意見」20文書290意見について、現行Luna、意見IDを固定したLuna、
同じID・本文・4分類を使うJevの比較を準備する。既存の段階1・段階3aの結果は変更しない。
CLIはローカルファイルだけを扱い、API送信・原文再取得・DB書込・注文を行わない。

実装済みなのは要求生成、保存応答の取込、評価である。JevやID固定Lunaの実API比較は未実施。
人手ラベルは未作成のため、モデル予測を入れない確認用下書きから進める。
人手確認が終わるまでは意味精度を判定しない。合成応答のテスト結果をモデル性能と扱わない。

## 入力と方式

- `prepare`は段階3aの`manifest.json`と`cache/*.source`を読み、原文hashを全件検証する。
  探索用は既存の20会合・290意見に固定する。PDFを再抽出せず、元研究が保存した同じ本文を使う。
- `luna-legacy`: `opinions_signal_study.build_request`のbodyを維持する。
  model=`gpt-5.6-luna`、reasoning effort=`medium`。可変長配列を原文順で返す。
- `luna-ids`: 同じ本文に固定IDを付け、全IDをrequiredにしたobjectのstrict schemaを返す。
  model・effortは現行方式と同じ。追加propertiesを認めない。
- `jev-ids`: ID固定Lunaと同じstateを使い、各IDをchoice質問にする。
  質問keyだけでなくinstructionsにもIDを含める。model=`jev-1.13.0`。
- ①対②は入力・出力設計、②対③はモデルを含むAPI方式の比較である。
  ①対③だけを比較してモデル交換の効果と解釈しない。

分類は`HIKE / HOLD / CUT / UNSPECIFIED`。条件付きでも方向が明示されていれば対象にする。
方向がない・複数方向から一意に決められない場合は`UNSPECIFIED`であり、棄権ではない。
否定、過去決定だけへの言及、一般的な正常化、国債買入れを政策金利の方向へ読み替えない。

## 実行手順

以下のパスは利用環境に合わせる。`prepare`と`evaluate`の出力先は新しいディレクトリを指定する。
同じ取込先の再利用も拒否するため、元データや評価結果を上書きしない。

```bash
python -m trading.data.policy.opinions_comparison prepare \
  --source-manifest tmp/opinions-signal/run-2/manifest.json \
  --cache-dir tmp/opinions-signal/cache \
  --run-dir tmp/opinions-comparison/run-1 --repeats 3
```

出力は比較`manifest.json`、元manifestのコピー、方式・反復ごとの`*.requests.jsonl`、
`labels.draft.jsonl`、`annotation-guide.md`。
固定ID、原文・本文・corpus・要求body・promptのhash、要求model、研究版を記録する。
要求に人手ラベルや既存の政策スコアは入れない。

### 画面で人手ラベルを確認する

追加の依存関係は不要。次のコマンドで起動し、表示された `http://127.0.0.1:8765` を開く。
`--output` の親ディレクトリは事前に用意し、初回は存在しないファイルを指定する。

```bash
python -m trading.data.policy.opinions_review \
  --run-dir tmp/opinions-comparison/run-1 \
  --output tmp/opinions-comparison/run-1/labels.reviewed.jsonl
```

本文と4分類の定義を読み、分類と確認者名を入力して「確認して保存・次の未確認へ」を押す。
未確認の意見には初期選択を置かない。選択・入力・移動だけでは確認済みにならず、
明示保存時に `status="reviewed"`、確認者とタイムゾーン付き確認日時を記録する。
前後の意見へ戻って訂正でき、「未確認に戻す」で分類・確認者・日時を消せる。
モデルの予測・応答は画面へ読み込まない。確認ルールは画面内で開ける。

保存は意見ごとに指定JSONLへ反映する。終了は `Ctrl+C`。再開時は同じコマンドに
`--resume` を付ける。既存ファイルへの初回上書きと元の `labels.draft.jsonl` への保存は拒否する。
別の下書きから始める場合だけ `--labels <JSONL>` を追加する。そのファイルも保存先にできない。
同じ保存先で複数の画面サーバーを起動せず、起動中は別のエディタで編集しない。
古いタブや外部更新を検出した場合は保存を止める。表示された手順で再読み込み・再開する。

「保存済みJSONLをダウンロード」は途中でも使える。ダウンロードしたファイルは
`--output <ダウンロード先> --resume` で再開できる。ID・本文・hashは評価処理と同じ検査で
照合し、欠損や重複のあるラベルは受け付けない。ローカル画面は `127.0.0.1` のみで待ち受け、
外部APIやDBには接続しない。ポートが使用中なら `--port <番号>` を指定する。

全290意見が確認済みになるまで意味指標は `null`、statusは `pending_human_review` になる。
途中のJSONLを評価に渡しても、この全件確認の条件は変わらない。
一部だけ確認しても、確認しやすい意見だけで精度を発表しない。

既存Lunaの保存Batch応答は、そのまま取り込める。

```bash
python -m trading.data.policy.opinions_comparison import-responses \
  --run-dir tmp/opinions-comparison/run-1 --arm luna-legacy \
  --requests tmp/opinions-signal/run-2/input.jsonl \
  --responses tmp/opinions-signal/run-2/output_file_id.jsonl \
  --batch-metadata tmp/opinions-signal/run-2/batch.json \
  --run-metadata tmp/opinions-signal/run-2/report.json
```

保存した要求のbody hash、method、urlを生成済み要求と照合する。要求の一部分だけを再試行した
JSONLも受け付ける。`--responses`にはoutputとerrorの複数ファイルを渡せる。
デフォルトは`--repeat 1 --attempt 1`。新しい反復はrepeatを、同じ反復の再試行はattemptを増やす。
再試行は直前のattemptの取込後に行う。CLI自体は送信・再試行を実行しない。

Lunaは標準Batch形式を使う。Jevの直接API応答は各行を次の形で保存する。
`body`は未加工のAPI JSON、失敗時はHTTP statusとerrorを残す。APIへの送信は別途許可された
実行者が行うもので、このツールには送信コマンドもSDK依存もない。

```json
{"custom_id":"BOJ-YYYY-MM-DD","response":{"status_code":200,"body":{"model":"jev-1.13.0","answers":{},"usage":{"input_tokens":123,"output_tokens":45}}},"error":null}
```

`answers`には要求と同じ全IDの`type / choice / probabilities / confidence`が必要。
この例の空のanswersは形式説明用で、有効な成功応答ではない。

```bash
python -m trading.data.policy.opinions_comparison evaluate \
  --run-dir tmp/opinions-comparison/run-1 \
  --labels tmp/opinions-comparison/run-1/labels.reviewed.jsonl \
  --output-dir tmp/opinions-comparison/evaluation-1
```

CLIの終了コードは正常処理0、保存応答の失敗・欠測を含む取込1、入力不正2。
`evaluate`の0はレポート生成の成功であり、精度の確認完了ではない。
ラベル確認状態と各方式の有効文書数はレポートで確認する。

## 費用・時間・失敗の記録

`--telemetry`は要求IDをキーとするJSONを受け付ける。

```json
{"BOJ-YYYY-MM-DD":{"client_seconds":3.2,"cost_usd":"0.000012"}}
```

観測は要求単位で保存する。応答が欠落しても既知の観測を残し、重複応答で二重加算しない。
`cost_usd`は実測額のみをDecimal文字列で指定する。費用が不明なら省略またはnullにする。
`--prices`で`input / cached_input / output`（USD/100万tokenの文字列）と`source`を渡すと、
usageから別項目の推計費用を出せる。契約・Batch適用後の単価を指定し、実請求額と混同しない。
要求方式に合う単価が不明なら指定しない。出力無料という確認済み料金だけは明示的な0を使える。

全attemptを集計する。不明値を0にせず、既知分の小計、欠測件数、全体値を分ける。
重複応答のusageから課金回数を決められないため、その応答の推計費用・サーバー時間は不明とする。
失敗応答でもusageがありmodelが一致する場合は推計に含める。
要求に帰属できない余分な応答は`unattributed_response_rows`として残し、
そのusageやサーバー時間を比較対象の要求合計へ加えない。

- 応答の`completed_at - created_at`: サーバー処理時間。
- telemetryの`client_seconds`: 呼出側で観測した各要求の経過時間。
- Batch metadataの作成から完了まで: Batch全体の経過時間。
- 元reportの`elapsed_seconds`: 原文準備やポーリング等を含む元run全体の経過時間。

各要求時間の合計を、並列実行したrun全体の待ち時間とみなさない。
元runの費用合計だけを個別要求へ均等配賦しない。

## 評価の読み方

欠損・重複・余分ID・不正JSON・拒否・モデル表記相違を失敗として残し、原応答も保存する。
旧形式の件数不一致は文書全体を対応不能とする。件数一致時も位置対応に限られ、
出力順が正しいことまで機械的には保証しない。

人手確認後は一致率、分類別混同行列・precision/recall、評価可能率、全290意見に対する正解数を出す。
同じ反復の3方式共通範囲も別に比較する。Jevの有効な4分類確率についてのみBrierを計算し、
Lunaへ確率を作らない。confidence閾値による棄権、校正器の学習や最適化は行わない。
再試行は最初の有効応答を採用し、正解に近い応答を選ばない。
反復一致は同じ方式・入力・モデル表記の有効応答間で測り、欠測・失敗と比較可能件数を併記する。

Lunaの要求・応答表記`gpt-5.6-luna`は保存するが、immutable snapshot版の固定保証はない。
model欠測・不一致は意味評価へ入れない。Jevも要求版と応答modelを照合する。

この20会合は探索用である。確認用は別の原文manifestを用意し、
`prepare --split confirmation --exploratory-manifest <探索run/manifest.json>`で会合重複を検出する。
この検査だけで過去の全研究から未使用と証明できるわけではないため、研究者が使用履歴も確認する。
現在の再分類結果を過去のknown_atで利用できた特徴量と扱わず、PIT storeへは接続しない。
抽出改善を理由に、段階3bの市場反応判定`not_established`を変更しない。

## 一次仕様

2026-09-21確認。

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [TypeSafe API](https://docs.typesafe.ai/api)
- [TypeSafe models](https://docs.typesafe.ai/models)
- [既存段階3a](2026-09-20-opinions-signal-redundancy-screen.md)
- [既存段階1](2026-09-19-llm-policy-extraction-accuracy.md)
