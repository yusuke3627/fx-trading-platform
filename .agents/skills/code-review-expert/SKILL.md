---
name: code-review-expert
description: git 差分や PR を正しさ・認可・性能・保守性・テストの観点でレビューし、P0–P3 の根拠付き指摘を返す。レビューのみなら編集せず、実装中のレビューでは依頼範囲の問題を修正する。
---

# Code Review Expert

ユーザーの指定範囲と AGENTS.md の規約を基準にする。
参照資料の一般例より、AGENTS.md の不変条件・境界検証・重要度・出力形式を優先する。レビューだけならコード変更・commit・投稿を行わない。
実装ワークフロー中は依頼範囲内の P0–P2 を修正し、関連チェックを通す。
仕様判断が必要な点は相談し、無関係なリファクタや削除計画を成果物に混ぜない。

## レビュー

1. git status --short、unstaged・staged の差分、指定 commit 範囲を確認する。
   PR は <base>...HEAD 全体を見る。git diff が空というだけで変更なしと結論しない。
2. 変更箇所の入口・呼び出し元・認可・保存先・テストを追い、具体的な条件で不具合を確認する。
3. 大きな差分は機能単位で分け、確認範囲を記録する。行数だけで未読部分を切り捨てない。
4. 下表から該当する参照だけを読み、正しさと影響が確認できる問題を優先する。

| 場面 | 参照 |
|---|---|
| 責務・依存方向の問題 | [SOLID](references/solid-checklist.md) |
| 認証・認可・外部入力・データ境界 | [セキュリティ](references/security-checklist.md) |
| 例外・性能・境界値 | [品質](references/code-quality-checklist.md) |
| 削除・改名が含まれる | [削除の確認](references/removal-plan.md) |
| LLM の出力やプロンプトを扱う | [LLM 境界](references/llm-trust-boundary.md) |
| 設定・API・手順が変わる | [ドキュメント](references/doc-staleness.md) |
| 修正範囲の判断に迷う | [修正分類](references/fix-classification.md) |

## 指摘

P0 Critical / P1 High / P2 Medium / P3 Low の重要度順で、ファイルと行、発生条件、
具体的な影響、最小の修正案を示す。プロジェクトの重大度定義があれば優先する。
AUTO-FIX / ASK は局所修正か仕様判断が必要かの分類であり、変更許可そのものではない。
脆弱性は到達経路と影響を説明し、シークレットの値は示さない。

空の重要度見出しや固定の確認質問は不要。指摘が無ければ確認した観点と未検証範囲を短く添える。
テストを実行していなければ成功と表現しない。ユーザーが求める形式や Codex のレビュー出力形式を優先する。
