# LLM Output Trust Boundary Checklist

## 概要

コードが LLM（OpenAI、Anthropic、ローカルモデルなど）の出力を消費する場合、その出力は**信頼できないユーザー入力**として扱わなければならない。LLM の応答は非決定的であり、プロンプトインジェクションによって操作され得るほか、不正または悪意のある内容を含む可能性がある。

## フラグすべきアンチパターン

### 直接実行

- **コード実行**: LLM 出力を `eval()`、`exec()`、`system()`、またはシェルコマンドに渡す
  ```ruby
  # DANGEROUS
  result = OpenAI.chat(prompt)
  eval(result)  # Arbitrary code execution
  ```
- **SQL の組み立て**: LLM 出力をクエリに埋め込む
  ```ruby
  # DANGEROUS
  filter = llm_response["sql_where"]
  User.where(filter)  # SQL injection
  ```
- **テンプレートのレンダリング**: LLM 出力をエスケープせずに HTML/ERB としてレンダリングする
  ```ruby
  # DANGEROUS
  raw(llm_response["html"])  # XSS
  ```

### 暗黙の信頼

- **構造化出力を有効とみなす**: スキーマ検証なしで LLM の JSON をパースする
  ```ruby
  # RISKY
  data = JSON.parse(llm_response)
  user.update!(role: data["role"])  # LLM could set role to "admin"
  ```
- **enum 値が未検証**: LLM が返す "status" や "type" をそのまま使う
  ```ruby
  # RISKY
  status = llm_response["status"]
  order.update!(status: status)  # No allowlist check
  ```
- **URL が未検証**: LLM が生成した URL をリダイレクトやフェッチに使う（SSRF）
  ```ruby
  # RISKY
  url = llm_response["source_url"]
  HTTParty.get(url)  # Could hit internal services
  ```

### プロンプト経由のデータ漏洩

- **プロンプト内の機微データ**: ユーザーの PII、認証情報、内部データを外部の LLM API に送信する
- **system プロンプトの露出**: プロンプトインジェクションによって system の指示が取得できる
- **ユーザー間のデータ**: あるユーザーのデータが別のユーザーの LLM コンテキストに含まれる

## 検証チェックリスト

- [ ] LLM 出力を `eval`、`exec`、`system`、またはそれに相当するものに**決して**渡さない
- [ ] LLM 出力を SQL、シェルコマンド、テンプレートに**決して**埋め込まない
- [ ] LLM が生成した JSON/構造化出力をスキーマ検証する（例: `JSON Schema`、`Zod`、`dry-validation`）
- [ ] LLM が返す enum/カテゴリ値を allowlist と照合する
- [ ] LLM が生成した URL は、フェッチ前にドメインの allowlist と照合する
- [ ] ユーザーに表示する LLM 出力は適切にエスケープする（HTML、Markdown）
- [ ] 保存前に LLM 出力へトークン/文字数の制限を適用する
- [ ] プロンプトに認証情報、トークン、シークレットを含めない
- [ ] プロンプト内のユーザーデータは、リクエスト元のユーザーのみにスコープする
- [ ] リトライ/フォールバックのロジックが、不正な LLM 応答を適切に処理する
- [ ] LLM API 呼び出しにレート制限を適用する（コスト + 悪用の防止）

## 重大度の分類

| Pattern | Severity | Reason |
|---------|----------|--------|
| LLM 出力を `eval`/`exec`/`system` に渡す | **P0** | Remote code execution |
| LLM 出力を SQL/シェルに埋め込む | **P0** | Injection |
| LLM 出力を raw HTML としてレンダリング | **P0** | XSS |
| 構造化出力のスキーマ検証なし | **P1** | Data integrity, potential privilege escalation |
| LLM が生成した URL をフェッチ/リダイレクトに使用 | **P1** | SSRF |
| 外部 API へのプロンプトにシークレット/PII を含む | **P1** | Data leakage |
| 出力長の制限なし | **P2** | Storage abuse, DoS |
| LLM 呼び出しのレート制限なし | **P2** | Cost abuse |
| プロンプトコンテキストにユーザー間データが混入 | **P1** | Privacy violation |

## 確認すべき問い

- 「この LLM 出力は何らかの実行コンテキスト（SQL、シェル、eval、テンプレート）で使われていないか？」
- 「LLM がまったく予期しない内容を返したら何が起きるか？」
- 「機微なデータが LLM プロバイダーに送られていないか？」
- 「あるユーザーのプロンプトインジェクションが別のユーザーの結果に影響し得るか？」
- 「LLM が悪意のある内容を返した場合の影響範囲（blast radius）はどれくらいか？」
