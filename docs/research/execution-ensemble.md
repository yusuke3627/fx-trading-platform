# 執行条件を変えた研究リプレイの反復

`trading.backtest.execution_ensemble` は、事前に指定した seed と cost scenario の全組を、既存の `trading.backtest.research` で実行する。既定は直列実行で、`--max-parallel N` に正の整数を指定すると最大 N 試行を同時に実行する。価格履歴をシャッフルせず、既存の PIT、Strategy → Portfolio → Risk → OMS → ExecutionSimulator を使う。外部フレームワークは追加しない。

結果は**固定した履歴と仮定した執行モデルへの感度**である。seed は同じモデルの乱数系列を変えるだけで、将来の市場経路を生成しない。損失試行割合は将来の損失確率ではなく、戦略の採用・本番昇格の根拠には単独で使えない。異なる scenario の試行を混ぜた分布は出さない。

## 実行

リポジトリルートから実行する。研究用データベースの DSN は、専用の環境変数へ事前に設定し、その**変数名**を `--dsn-env` で必ず指定する。引数・保存物に DSN の値を含めない。子プロセスには指定された DSN を渡し、継承した `TRADING_DB_DSN` を暗黙に採用しない。DB 入力は既存 research の読み取り経路を使い、Broker には接続しない。

```bash
python -m trading.backtest.execution_ensemble \
  --dsn-env RESEARCH_DB_DSN \
  --strategy post_event_failed_breakout --symbol USDJPY \
  --from 2026-08-18T00:00:00+00:00 --to 2026-08-23T00:00:00+00:00 \
  --seeds 11 22 33 44 55 \
  --scenarios normal spread_x2 spread_x5 \
  --purpose '同じ期間・戦略で執行コストへの感度を比較する' \
  --risk-mode research \
  --risk-basis 'エッジ測定用の backtest 設定。運用損失上限の評価には使わない' \
  --out reports/execution-ensembles
```

この例は実データ測定結果ではない。`--from/--to` は既存 research と同じ broker-clock の半開区間で、UTC 表記のラベルのみを受け付ける。warmup は戦略の宣言を使い、必要なら `--warmup-days` を指定する。`--param KEY=VALUE` は既存 research と同様に扱い、銘柄別設定が最優先となる。これらの条件は全試行で固定する。seed/scenario の重複はエラーにする。

`--max-parallel N` の完了時間は `ceil(試行数 / N)` 波で決まる。波数が減らない N を指定しても完了は早くならず、資源競合だけが増える。たとえば 6 試行では 4 並列・5 並列とも 3 並列と同じ 2 波で、3 並列より速くなるのは 6 並列（1 波）だけである。

2026-09-23 に Windows VPS で、2026 年 7 月の `range_edge_reversal`（1 本 416 万 tick）を実測した。研究の子を BelowNormal で起動した修正前（`5405716`）と、Idle にした修正後（`4f3a643`）で、live のティック収集の取り込み遅延（`received_at − event_time`、broker は UTC+3）は次のとおりだった。

| 条件 | p95（秒） | p99（秒） | 最大（秒） | 2 秒超（件） |
| --- | ---: | ---: | ---: | ---: |
| 修正前・直前 | 0.22 | 0.73 | 2.1 | 2 |
| 修正前・4 並列（BelowNormal） | 2.44 | 11.5 | 19.2 | 233 |
| 修正後・直前 | 0.22 | 0.89 | 2.5 | 12 |
| 修正後・単独（Idle） | 0.59 | 3.18 | 5.9 | 82 |
| 修正後・4 並列（Idle） | 7.96 | 15.8 | 22.0 | 586 |
| 修正後・直後 | 0.21 | 0.22 | 1.5 | 0 |

研究側は、修正前の単独が 554 秒（全体 559 秒）、4 並列が 641 秒ずつ（全体 646 秒、単独比 3.43 倍、1 本あたり約 16% 増）だった。Idle では単独 577 秒、4 並列 660 秒ずつ（全体 664 秒、Idle 単独比 3.48 倍）だった。

**研究の子を Idle 優先度にしても、収集遅延は改善しなかったため、この優先度指定は取り下げた。** ただし Idle の 4 並列時は市場が活発で、live のティック数が修正前の 4 並列時より約 4 割多く、優先度だけの効果を比較できる条件ではない。研究の読み出しを処理する PostgreSQL（通常優先度のサービス）側のディスク I/O・キャッシュ・WAL の競合が疑われるが、直接の証拠はなく、原因は未特定である。

**live 収集と同居するホストで並列実行すると、収集の取り込みが遅れる。** 並列度は小さい値から始め、研究の所要時間と収集遅延を実測して上げる。live のティック収集や MT5 と同居するホストでは、`--max-parallel` をコア数より小さくする。研究対象の過去区間へのバックフィルは同時に実行しない。

現時点の `config/backtest.yaml` は日次損失・直近24時間損失・最高資産からのドローダウンによる停止値をすべて **100% に緩和した研究用設定**である。これを運用損失制限下の実験とは表示しない。`--risk-mode operational-limits` は、取引が有効で、この3つの停止値がすべて 0% 超・100% 未満の場合だけ受け付ける。設定は自動変更しない。別の設定を使う場合も `--env` と `--risk-basis` に評価条件と根拠を明示する。この区分は運用環境との同等性や安全性を保証しない。

## 保存物と成功の条件

`--out` の下へ実験ごとの UUID ディレクトリを作る。再試行は新しい実験として実行し、過去の試行を選別して再集計・上書きする機能は持たせない。出力先は `reports/` など gitignore 対象、またはリポジトリ外を使う。追跡対象の場所へ出力すると git state が変わり、再現性検証に失敗する。

- `plan.json`: **実行前**に全 seed × scenario の計画、argv、目的、Risk 設定の根拠、解決済み設定、cost model、コード状態、期間・戦略・warmup を保存する。
- `results.json`: 各試行の `planned` / `running` / `succeeded` / `failed`、終了コード、エラー、研究出力先、成功時の金額・件数を保存する。
- `summary.json`: scenario 別の計画・成功・失敗・実行中・未着手件数と分布。目的・Risk 停止値・解釈上の制約も含む。
- 各試行のディレクトリ: `stdout.json`、`stderr.log` と既存 research が作る manifest・summary・約定・エクイティ・バーの記録。

各 manifest を実行前の git commit/差分、config hash、期間、戦略 ID/version、engine version、symbol、warmup、パラメータ、Python version に照合する。計画順で最初に成功した試行を基準として、tick/feature/swap の fingerprint と tick 件数、初期資産を含む再現入力の一致を確認する。全 manifest のうち seed、scenario、run ID、作成時刻以外は同じでなければならない。実行中のコード変更も検出する。データ更新などで入力が変わった場合、異なる試行を成功分布へ混ぜない。

子プロセスの失敗、出力欠損、不正な金額・件数、入力不一致、未値付け swap（`unpriced_rollovers > 0`）、期末に残った執行コマンド（`pending_commands_at_end > 0`）は失敗として残し、後続の計画済み試行を続ける。scenario に1件でも失敗・未着手・実行中があれば、その scenario の分布を `null` にする。ほかの scenario が完了しても、実験全体は `incomplete`、CLI の終了コードは1になる。全件成功時だけ `complete`、終了コード0とする。

Ctrl-C は検証が確定していない開始済みの試行を失敗として保存し、未開始の試行を起動しない。実行中の子プロセスを終了してから中断する。Windows では逐次・並列とも子を一時停止状態で起動し、ジョブオブジェクトに入れてから再開するので、親プロセス終了時に子も終了する。ただし子の生成からジョブへの割り当てまでのごく短い間に親が強制終了された場合は、一時停止したままの子（何も実行せず CPU も使わない `python.exe`）が残ることがあり、その場合は手で終了する。`Stop-ScheduledTask` などの強制終了では後始末が走らず、`results.json` は最後の `running` のまま残るが、子はジョブオブジェクトで終了する。状態を保存して止めるには **Ctrl-C が望ましい**。ホスト停止でも記録が `running` のまま残ることがある。保存に失敗した場合は成功の終了コードを返さない。

JSON の差し替えは、一時的なファイルロックによる `PermissionError` の場合だけ 0.5 秒間隔で最大 5 回再試行する（待機は合計最大 2.5 秒）。解消しなければ例外を返す。CLI 最後の JSON 出力はファイル・パイプへのリダイレクト時も UTF-8 とする。

## 分布の定義

金額は `Decimal` の文字列として読み書きし、浮動小数点へ変換しない。現在の research が対応するデータセット定義では金額の通貨は JPY である。

| 出力 | 定義 |
| --- | --- |
| `loss_fraction` | `net_pnl < 0` の試行数 / 当該 scenario の計画試行数。全件成功時だけ出す。ゼロ損益は損失に数えない |
| `lower_net_pnl` | `net_pnl` の下位分位点。既定 `p=0.05` |
| `upper_max_drawdown` | 既存エンジンが記録する資産の最大ドローダウン金額の上位分位点。既定 `p=0.95` |

分位点は昇順に並べた `ceil(n × p)` 番目（1始まり）を使う nearest-rank 方式で、補間しない。`--pnl-quantile` は 0 超・0.5 以下、`--drawdown-quantile` は 0.5 以上・1 以下を指定できる。少数試行では端の分位点が単に最小・最大値になる。標本数と個別試行を合わせて読む。

`net_pnl` は実現損益と期末の含み損益の和で、値付け済み carry を含む。期末ポジションは既存エンジンの Bid/Ask 時価評価を使い、強制決済しない。強制決済の追加コストや期間終了後のリスクは含まない。`open_position_trials` と各試行の `open_positions_at_end` / `unrealized_pnl` で識別する。未値付け carry がある試行は分布へ入れない。

約定ゼロの試行も計画から除かず、ゼロ損益の試行として残す。`no_fill_trials`、`no_closed_trade_trials` を別に数えるため、ゼロ損失割合を売買実績の証拠と取り違えない。決済済み取引ゼロでも未決済ポジションがあれば、時価評価額で扱う。

## DB・Broker を使わない動作確認

次は600 tick の合成履歴と検証用戦略を使う。fixture は DB 境界のみを置換し、各試行を実際の research CLI の子プロセスとして起動する。Risk/OMS/ExecutionSimulator は本物を使う。実データの収益性・長時間実行は検証対象外。

```bash
env -u TRADING_DB_DSN \
  PYTHONPATH="tests/fixtures/execution_ensemble:." \
  ENSEMBLE_FIXTURE_DSN=synthetic-fixture-no-network \
  .venv/bin/python -m trading.backtest.execution_ensemble \
  --dsn-env ENSEMBLE_FIXTURE_DSN --strategy ensemble_probe \
  --from 2026-01-05T10:00:00+00:00 --to 2026-01-05T10:10:00+00:00 \
  --seeds 7 8 --scenarios normal spread_x2 \
  --purpose '合成CLI動作確認' --risk-basis '研究用の合成入力' \
  --out reports/execution-ensemble-smoke
```

fixture の `PYTHONPATH` はこの合成確認にのみ指定する。実データ測定では設定しない。回帰テストは `env -u TRADING_DB_DSN .venv/bin/pytest tests/unit/test_execution_ensemble.py tests/replay/test_execution_ensemble.py -q` で実行できる。
