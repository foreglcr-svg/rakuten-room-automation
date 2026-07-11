# 認証情報・APIキー・GitHub Secrets 管理のセキュリティ診断

- 実施日: 2026-07-11 ／ 実施: NOREN 情シス部（タスク fac41c1a）
- 対象: app.py の環境変数参照・.github/workflows/daily-catalog.yml のSecrets参照

## 1. 秘密の一覧と管理状況

| 秘密 | 用途 | 置き場所 | 評価 |
|---|---|---|---|
| RAKUTEN_APP_ID | 市場API | GitHub Secrets | ✅ env参照のみ・実値なし |
| RAKUTEN_ACCESS_KEY | 新API必須（pk_） | GitHub Secrets | ✅ |
| RAKUTEN_AFFILIATE_ID | 任意 | GitHub Secrets | ✅ |
| GEMINI_API_KEY | 紹介文生成 | GitHub Secrets（GOOGLE/OPENAIへフォールバック） | ⚠️ フォールバックが緩い（下記） |
| DISCORD_WEBHOOK_URL | 通知（任意） | GitHub Secrets | ✅ |

- app.py の `_env()` は取得と整形（引用符除去）のみで、**値をログ出力しない**。✅
- workflow は Secrets を env 注入。リポジトリ・コードに実値は無い。✅

## 2. 指摘と改善提案

| # | 優先 | 指摘 | 提案 |
|---|---|---|---|
| 1 | 中 | Geminiキーが `GEMINI_API_KEY \|\| GOOGLE_API_KEY \|\| OPENAI_API_KEY` の緩いフォールバック。意図しないキーを拾う/デバッグ困難 | 使うキーを1つに固定。未設定時は明示的に失敗させる |
| 2 | 中 | ログ・エラー出力にキーが混じらないかの継続確認 | Gemini/楽天API呼び出しの例外handlingでURL・ヘッダを出力しない（キーはクエリ/ヘッダに載る） |
| 3 | 低 | Secrets のローテーション運用 | 楽天accessKey・Geminiキーの定期ローテーションを運用化。漏洩時の即時無効化手順をREADMEに |
| 4 | 低 | posted_items.json 等の生成物に秘密が混入しないか | 現状混入なし。生成ロジック変更時に再点検 |
| 5 | 低 | Actions の権限（GITHUB_TOKEN）最小化 | workflow の permissions を必要最小（contents:write, issues:write 等）に明示 |

## 3. GitHub Secrets 運用チェックリスト（社長がリポジトリ設定で確認）

1. [ ] Secrets は Repository/Environment secrets に設定（コード・Variablesに平文で置かない）
2. [ ] 不要になった旧キー（旧API形式等）は削除
3. [ ] フォークからのPRでSecretsが渡らない設定（デフォルトで安全だが確認）
4. [ ] workflow の `permissions:` を最小化

## 4. 情シス部の結論

**秘密管理の方式は妥当**（env/Secrets経由・コードに実値なし・ログ非出力）。改善は「Geminiキーのフォールバック撤廃」「API例外でのキー非出力の徹底」「permissions最小化」の3点。いずれも小コストの🟡改善。定期的なキーのローテーションを推奨。
