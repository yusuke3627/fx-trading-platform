# issue #176: Dukascopy 取り込みが USDJPY 以外を受け付けない（`POINT_SCALES` の未登録）

ブランチ: `fix/issue-176-dukascopy-point-scales`
worktree: `/Users/yusuke/Products/fx-trading-platform/.claude/worktrees/fix+issue-176-dukascopy-point-scales`

## 先に読むもの

- `AGENTS.md`（正本。特に「テストルール」「変更管理」「AIレビュー指示」）
- `.claude/rules/change-management.md`、`.claude/rules/testing-project.md`
- `src/trading/data/market/dukascopy.py`（`POINT_SCALES`、`decode_bi5`、`main` の symbol 検査）
- `tests/unit/test_dukascopy_importer.py`（`bi5_payload` ヘルパーと `test_decode_bi5_scales_points_without_float_conversion`）

## 背景と課題

`POINT_SCALES` に USDJPY しか登録されておらず、`main` が `symbol not in POINT_SCALES` で他ペアを
`parser.error("unsupported Dukascopy symbol: ...")` として弾く。EURUSD / GBPUSD / GBPJPY のライブ収集は
既に始まっているため、過去 tick の調達がここで止まる。

Dukascopy の bi5 は価格を整数（point）で持ち、通貨ペアごとの倍率で実価格に戻す。倍率は Dukascopy の小数桁に
対応し、JPY クロスが 3 桁で `0.001`、対ドルが 5 桁で `0.00001`。

## 実測値（2026-09-10 10:00 UTC の bi5、先頭レコード）

| symbol | 生の bid | 倍率 0.00001 | 倍率 0.001 | 採用 |
|---|---|---|---|---|
| USDJPY | 153705 | 1.53705 | **153.705** | `0.001` |
| EURUSD | 116343 | **1.16343** | 116.343 | `0.00001` |
| GBPUSD | 135490 | **1.35490** | 135.490 | `0.00001` |
| GBPJPY | 208250 | 2.08250 | **208.250** | `0.001` |

## 方針

- 倍率は Dukascopy 側の表（`POINT_SCALES`）として持つ。`InstrumentSpec` からは導出しない
  （broker の symbol alias で壊れるため。ユーザー判断で確定済み）

## 変更範囲

### 1. `src/trading/data/market/dukascopy.py`

`POINT_SCALES` に 3 ペアを足す。

```python
POINT_SCALES: dict[str, Decimal] = {
    "USDJPY": Decimal("0.001"),
    "EURUSD": Decimal("0.00001"),
    "GBPUSD": Decimal("0.00001"),
    "GBPJPY": Decimal("0.001"),
}
```

既存コメント「Dukascopy の point 単位は通貨ペア依存なので、対応ペアの追加時はここへ追記する」に、倍率の由来を
1〜2 行足す（Dukascopy の小数桁に対応し、JPY クロスは 3 桁で `0.001`、対ドルは 5 桁で `0.00001`。
実際の bi5 を復号して確認済み）。issue 番号・日付・コミット文脈に依存する記述は書かない。

### 2. テスト（`tests/unit/test_dukascopy_importer.py`）

`test_decode_bi5_scales_points_without_float_conversion` の隣に、対ドルのペア（5 桁）のテストを 1 本足す。
既存と同じ流儀で `bi5_payload((msec, ask_point, bid_point))` を組み立てて `decode_bi5` に渡し、
`0.00001` 倍で戻ることを固定する。例:

- symbol `"EURUSD"`、生の bid `116343` / ask `116345`（上の実測値に合わせる）
- `ticks[0].bid == Decimal("1.16343")`、`ticks[0].ask == Decimal("1.16345")`

実ネットワークを叩くテストは足さない。`main` の symbol 検査のテストは現在無いので足さなくてよい。
既存のテストは無変更で通ること。

## やらないこと

- 取得・再試行・障害待避（`RETRY_WAITS` / `OUTAGE_THRESHOLD` 等）のロジック変更
- `InstrumentSpec` からの倍率導出
- `config/` の変更、`_windows` や取り込み処理（`DukascopyTickImporter`）の変更
- 周辺リファクタ・無関係な整形

## 完了条件と検証

- 上記 1〜2 が実装されている
- `.venv/bin/ruff check .` がクリーン
- `.venv/bin/pytest tests/unit tests/replay tests/failure` が全て通る（broker は自動 skip、integration は対象外）
- 変更ファイル一覧、実行した検証コマンドと結果、未確認項目を返す
- **commit / push / PR 作成はしない**（Claude が担当）

## 未確定事項

なし。
