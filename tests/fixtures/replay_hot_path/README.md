# リプレイ同一性の固定入力

`20241107T19Z.bi5` と `20241107T20Z.bi5` は、既存の
`reports/jev-policy-20260920T132722Z/market/` から内容を変えずに複製した
USDJPY の実 tick（計 33,054 件）。ネットワークやローカル reports に依存せず回帰テストを実行するために置く。
ファイル名は実 UTC。テストでは既存の decoder と時刻変換を使い、broker ラベルから known time を再構成する。

| ファイル | SHA-256 |
|---|---|
| `20241107T19Z.bi5` | `a7e98d835f1e4a8ff87bc23809b8d5ddf37f95ea1e71570721e10fbf63273db7` |
| `20241107T20Z.bi5` | `3bde9f613283c08b32d982ad63a1ce237d8911c45a164efb96c5fac30149a6b3` |

`tests/replay/hot_path_inputs.py` に合成 tick（seed 73、300ms 間隔、12,000 件）と
両戦略の検証用設定を固定している。先頭 30 分を warmup にする。
戦略の本番既定値の性能測定ではなく、短いデータで売買を含む処理を通すための設定である。

`test_hot_path_equivalence.py` の期待 digest は、本体を変更する前に
origin/main `4b4b09d919b944332ba3f35f56a8e667e2d21b10` の実装で各ケースを 2 回実行し、
一致を確認して固定した。対象は `BacktestResult` 全フィールド、各 CSV の全内容、
再構成した tick の `TickDigest`。Decimal は文字列表現を維持し、float は `hex()` で正準化する。
無作為な ID を除去するなどの結果加工は行わない。

```sh
.venv/bin/pytest tests/replay/test_hot_path_equivalence.py -q
```
