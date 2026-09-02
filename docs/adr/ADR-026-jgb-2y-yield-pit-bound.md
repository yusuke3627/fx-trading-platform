# ADR-026: JGB 2年金利（jp_jgb_2y_yield）の PIT 方針

**Status:** Accepted (2026-09-01)

## Decision

財務省「国債金利情報」CSV から JGB 2年複利利回りを `jp_jgb_2y_yield` として
macro_observations に収集する。ADR-015 の `PIT_UNVERIFIED` 規範（backfill に
release 時刻の known_at を与えない）に対し、この系列に限り **backfill にも
「公表タイミングの保守的 bound」を known_at として与える**。

- **bound = その基準日の「次の基準日」の 15:00 JST**。公表は基準日の翌営業日
  09:30 頃（公式 FAQ https://www.mof.go.jp/faq/jgbs/04hf.htm 、実測でも
  jgbcm.csv の Last-Modified = 2026-09-01T09:30:43+09:00 が 8/31 分までの内容と
  一致）。祝日カレンダーを持たずに翌営業日を得るため、CSV の基準日列そのもの
  （= JGB 営業日カレンダー）から次の基準日を採り、09:30「頃」への余裕として
  15:00 に置く。
- **次の基準日が未出現の最新行は emit しない**。known_at が決定論的になり、
  再実行・独立 DB の複数ホスト（Mac / VPS）で同一 vintage になる。
- **次の基準日との間隔が 14 暦日を超える行も emit しない**。連続する基準日の
  実在し得る最大間隔は 11 暦日（2019 年 GW の 10 連休）で、それを超える間隔は
  ファイル境界の空白（全期間ファイルの月次更新が当月ファイルの切り替わりより
  遅れた状態）。空白の両端を営業日の並びとして扱うと、前月末の値に翌月の
  known_at が付き、空白が埋まった後の再収集で同値の重複 vintage ができる。
  該当行は真の翌営業日が現れる月次更新後のランで emit される。
- 分類は `PIT_UNVERIFIED` のまま（ソースは最新版の履歴のみ配信し、真の
  vintage アーカイブは無い）。

## 根拠

ADR-015 が backfill への release 時刻 known_at を禁じたのは、対象8系列が
改定される統計で、「当時見えていた値」を後日の CSV から復元できないため。
JGB 複利利回りはこの前提が当てはまらない:

1. **公表時刻が公式に有界**（翌営業日 09:30 頃）。bound はそれより必ず遅い
   ので、known_at <= t で読む replay に look-ahead が入らない。
2. **値は市場実勢からの機械的算出で、遡及訂正は実務上ない**。仮に訂正されて
   も、collector は同じ known_at を計算して vintage キーの ON CONFLICT で
   落ち、**初出値が保持される**。訂正の真の公表時刻は知り得ないので、初出値
   保持が PIT として最も保守的。
3. 同型の先行例として、介入 daily（`intervention/mof.py` の `daily_known_at`）
   が「四半期末 + 62日 19:00 JST の保守的 bound」を backfill に与えている。

この扱いを他の PIT_UNVERIFIED 系列へ一般化はしない。適用条件は「公表時刻が
公式に有界」かつ「歴史ファイルが初出値のまま維持される」ことで、満たす系列を
追加するときは本 ADR を参照した上で個別に判断する。

## 帰結・制約

- 値の DB 反映は公表から最大1営業日強遅れる（次の基準日の出現待ち + 月末行は
  全期間ファイルの月次更新待ちで2〜3日）。live の feature 消費者は現状なく、
  B′ スタディ（`rate_differential_study`）は履歴を読むので影響しない。
  live 昇格時はこの遅延を前提に設計すること。
- 日足バー t の close（17:00 ET）で見える JP2Y は通常 t−1 の値。US2Y
  （ALFRED、vintage 日 18:00 ET）も同じく t−1 で、金利差は両系列対称に
  1営業日ラグする。
- strict OOS 評価でこの系列の収集開始前の期間を除外する必要はない（bound が
  公表実時刻の上界であることが上記 1–2 で裏付けられるため）。ADR-015 の
  8系列には引き続き除外規範を適用する。

## ソースの事実（2026-09-01 実測）

- 当月分: `https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv`
  （実測は当月のみ・約2KB）
- 全期間: `https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv`
  （S49.9.24〜、約1.1MB、月次更新で約1ヶ月遅れ。年別ファイルは404）
- Shift_JIS。日付は和暦短縮形（`S49.9.24` / `H1.1.9` / `R8.8.31`）。
  ヘッダ `基準日,1年,2年,…,40年`、欠測は `-`、末尾に注記行。
