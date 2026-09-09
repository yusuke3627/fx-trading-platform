# 2024年3〜6月の政策会合の転記根拠

`config/policy_meetings.yaml` の対象期間を2024年3月1日まで広げ、7月より前の定例会合6件を追加する。
金利決定への反対人数、当年の物価見通し改定、声明本文の利上げ予告を既存の採点ルールで扱う。

## 追加した会合

| 会合 | 金利変更 | 金利決定への反対 | 見通し改定 | 声明公表時刻（UTC） | 一次資料 |
| --- | --- | --- | --- | --- | --- |
| BOJ 2024-03-19 | +20bp | 緩和方向2名 | なし | 03:35 | [声明と公表時刻](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240319a.htm) |
| FED 2024-03-20 | 据え置き | なし | 上方 | 18:00 | [声明](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240320a.htm)・[SEP](https://www.federalreserve.gov/monetarypolicy/fomcprojtabl20240320.htm) |
| BOJ 2024-04-26 | 据え置き | なし | 上方 | 03:22 | [声明と公表時刻](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240426a.htm)・[展望の基本的見解](https://www.boj.or.jp/en/mopo/outlook/gor2404a.pdf) |
| FED 2024-05-01 | 据え置き | なし | なし | 18:00 | [声明](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240501a.htm) |
| FED 2024-06-12 | 据え置き | なし | 上方 | 18:00 | [声明](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240612a.htm)・[SEP](https://www.federalreserve.gov/monetarypolicy/fomcprojtabl20240612.htm) |
| BOJ 2024-06-14 | 据え置き | なし | なし | 03:23 | [声明と公表時刻](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240614a.htm) |

3月BOJの+20bpは、[前会合の短期政策金利-0.1%](https://www.boj.or.jp/en/mopo/mpmdeci/state_2024/k240123a.htm)と、新たな誘導範囲0〜0.1%の上限との差を表す。
7月の既存エントリが0.1%から0.25%への差を+15bpとしているため、同じ基準を使う。
枠組みの移行を含む決定であり、市場で実現した翌日物金利が20bp上昇したという意味ではない。
採点は変更幅の符号のみを使う。

3月FEDの当年コアPCE見通し中央値は2.4%から2.6%、6月は2.6%から2.8%へ上方修正された。
4月BOJの2024年度コアCPI見通し中央値は、展望の基本的見解の付表で2.4%から2.8%へ上方修正されている。
6月BOJの国債買入れ減額方針への反対1名は、金利据え置きへの反対に数えない。
追加6件には声明本文での明示的な将来利上げ予告がないため、該当フラグはfalseとする。

## 公表時刻と予定窓

3〜6月BOJの実公表時刻は、各声明末尾の公表日時を採用した。
事前のリスク窓には、[前年公表の2024年会合日程](https://www.boj.or.jp/mopo/mpmsche_minu/m_ref/mref230728a.pdf)に基づく予定を追加し、従来どおり9:00〜15:00 JSTの幅を持たせる。
実公表時刻が分かった後も予定窓は縮めない。

[BOJ 2026年7月31日の声明](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260731a.pdf)の参考欄は、公表時刻を12:11 JSTと記録している。
当該エントリの15:00 JSTという保守置きを12:11へ訂正する。
声明の8対1、反対1名の1.25%提案は既存の入力と一致する。
[FED 2026年7月29日の声明](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm)も、9対3、反対3名の25bp利上げ選好、14:00 EDT公表という既存入力と一致する。

## 保存先への反映

collectorは決定的IDでupsertするため、追加6件を新規保存し、既存のBOJ 2026年7月31日イベントの公表時刻を訂正する。
同じファイルで再実行した場合は新規保存・訂正とも0件となる。

```bash
python -m trading.data.policy.collector --env demo
```

このコマンドは設定されたDBへ書き込む。
開発時の動作確認は専用ローカルDBで実施し、運用DBへの反映は行わない。
