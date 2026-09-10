# 日中戦略のマクロ確認レッグの ablation（H5）と判定

日付: 2026-09-10
対象: `post_event_failed_breakout`（USD/JPY、intraday）
判定: **判定不能（差が検出できない）。確認レッグは維持したまま再測定** ―― ただし両腕とも損失で、戦略そのものを昇格させる根拠は無い

## 背景

H5 は「日中の failed breakout にマクロ確認（米 2 年金利の 1 日・5 日ドリフト、BOJ 声明スコア）を加えると質が上がる」という仮説。確認レッグは棄却済みの H1（日米の政策収斂が円高を予測する）と同じ金利差情報を使っているため、H1 棄却後は寄与を疑う必要があり、レッグの寄与を分離して測ることにした（PR #124 で ablation ハーネスを追加）。

## 方法

同じ保存 tick・同じ期間・同じ seed で、`macro_confirmation_enabled` を true / false にした 2 本の research リプレイを流し、`trading.backtest.ablation_compare` で約定ごとの純損益を比べる。

判定規則は実行前に固定した（`ablation_compare.judge`、上から順に評価）:

| 条件 | 判定 |
|---|---|
| 確認ありの平均が高く、差の CI90 下限が 0 より大きい | 維持（寄与あり） |
| 確認なしの約定数が 2 倍以上で、平均が確認あり以上 | 外す（絞るだけで質が上がらない） |
| どちらかの腕の約定が 10 件未満 | 判定不能（標本不足）。維持したまま再測定 |
| それ以外 | 判定不能（差が検出できない）。維持したまま再測定 |

差の CI90 は bootstrap（`BOOTSTRAP_SAMPLES` 回、seed 42）。部分決済は 1 エントリーを 1 標本に集約する（issue #126、PR #140）。両腕の共通注文には同じ執行ショックを割り当てる（issue #125 / #134、PR #129 / #140）ので、差に執行ノイズの引き直しは混ざらない。

## データ

| 項目 | 値 |
|---|---|
| コード | commit `4945335`（PR #140 マージ直後の main）。両腕とも `git_dirty=True` だが `git_diff_sha256` は一致（VPS 直下の未追跡ファイルによるもので、比較 CLI の検証を通過） |
| 期間 | 2024-08-01 〜 2026-08-29（broker ラベル、終端は排他） |
| tick | `dataset_hash=2432a1cd…`（同じハッシュの run で 98,892,247 本） |
| PIT 行 | `feature_dataset_hash=5d0d0827…`（両腕で一致） |
| scenario / seed | normal / 42 |
| 確認あり | `reports/h5_with3/43fe0895-97f8-4fb8-afb7-451b1c4d2150` |
| 確認なし | `reports/h5_without3/09ffb244-b22d-41fc-af2c-10a6464edcd5` |

## 結果

| 指標 | 確認あり | 確認なし |
|---|---|---|
| 約定（round trip） | 239 | 442 |
| net PnL 合計 | −9,327.21 | −22,287.37 |
| 粗利（仲値） | −6,173.50 | −16,671.50 |
| 執行コスト | 3,153.71 | 5,615.87 |
| 約定あたり平均（expectancy） | −39.03 | −50.42 |
| 平均の CI90 | [−92.97, +28.98] | [−68.97, −28.09] |
| 勝率（net > 0） | 1.3%（約 3 件） | 11.5%（約 51 件） |
| 最大ドローダウン | 30,115.26 | 30,250.84 |
| protection fill | 236 / 239 | 391 / 442 |
| carry / unpriced rollovers | 0 / 653 | 0 / 651 |

差の平均（確認あり − 確認なし）: **+11.40**、CI90 **[−45.37, +81.31]**。

## 判定

規則を上から当てると次のとおり。確認ありの平均は高いが差の CI90 下限が負なので「維持」にならない。確認なしの約定数 442 は 2 × 239 = 478 に届かず「外す」にもならない。両腕とも 10 件以上なので、**「判定不能（差が検出できない）」** に落ちる。

## 解釈

判定は H5 について「分からない」だが、出力には H5 より重要な事実が 2 つある。

1. **戦略はどちらの腕でも損失。** 確認なしは平均 −50.42 で CI90 が 0 を外し、明確に負。確認ありは平均 −39.03 で CI90 が 0 を含むが、点推定は負で、239 件中の勝ちが約 3 件しかない。初期資金 100 万に対して −0.93%（確認あり）と −2.23%（確認なし）。金利コストは両腕とも未モデル化（`carry_total=0`、`unpriced_rollovers` 650 件超）なので、実際はこれより悪い。
2. **確認レッグは損失を減らすが、優位性を作らない。** レッグは約定を 442 → 239 に絞る（46% を落とす）。落とされた 203 件の平均は約 −63.9 で、通した 239 件の −39.0 より悪いので、フィルタとしては機能している。しかし落とした中に勝ちの大半（約 51 件のうち約 48 件）も含まれ、残った 239 件はほぼ全部が小さな負け（決済の 236 / 239 が protection fill）になる。

つまり H5 の「質が上がる」は、「悪いセットアップを減らす」という意味では方向として正しく、「取引可能な優位性になる」という意味では否定される。**`post_event_failed_breakout` を live へ昇格させる根拠は現時点で無い。**

「維持したまま再測定」を文字どおり受け取るのは勧めない。差の分散は執行ノイズではなく約定そのもののばらつきから来ているので、seed を増やしても縮まらない。期間もすでに 2 年分で、標本を増やすには時間を待つしかない。再測定より、決済のほぼ全部が protection fill になっている構造（利確・損切りの設計）を先に見直すべき。

## 教訓

- **ablation の 2 腕は並列で流す。** 逐次で流した 1 回目は、1 本目の実行中に collector が `INTERVENTION_GOVERNMENT_CONFIRMED`（known_at 2026-08-03、窓の内側）を書き込み、`feature_dataset_hash` が食い違って比較が拒否された。fingerprint は実行開始時に固定される（`StoredFeatureSource.frozen`）ので、腕ごとの開始時刻が数時間ずれるとその間の PIT 行の変化がそのまま差になる。ガードは正しく働いた。
- 両腕を流す前に、YAML 由来の collector（policy / intervention）を流し切り、`fx-macro` を止める。tick / bar のタスクは止めない（fingerprint に入らず、止めると系列に穴が空く）。
- PR #140 で `trades.csv` に `entry_id` が入り、それ以前の run は比較 CLI で読めなくなった（`SystemExit: no entry_id`）。ハーネスを変えたら両腕を取り直す。

## 次のステップ

- H5 は「差が検出できない」で閉じ、再測定はデータが実質的に増えるまで行わない。
- `post_event_failed_breakout` は shadow のまま。昇格の判断は、protection fill 偏重の決済構造を直したうえで改めて測る。
- 研究資源は H4（failed spike reversal の基礎エッジ、Model A）へ移す。
- 金利コストの未モデル化（issue #60、swap の PIT 収集）は、intraday でも 650 件超のロールオーバーをまたいでいるので、次の判定までに接続する。

## 再現

VPS で collector を止めたうえで、2 つのターミナルからほぼ同時に:

```
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy post_event_failed_breakout --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --out reports/h5_with3
python -m trading.backtest.research --env backtest --symbol USDJPY --strategy post_event_failed_breakout --from 2024-08-01T00:00:00+00:00 --to 2026-08-29T00:00:00+00:00 --seed 42 --param macro_confirmation_enabled=false --out reports/h5_without3
python -m trading.backtest.ablation_compare --with reports/h5_with3/<run_id> --without reports/h5_without3/<run_id> --seed 42
```

比較 CLI の冒頭に出る `git_commit` / `dataset_hash` / `feature_dataset_hash` が本ノートの値と一致すれば同じ結果になる。
