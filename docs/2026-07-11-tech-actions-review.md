# 楽天ROOM自動化 GitHubリポジトリ・Actions技術課題調査

- 実施日: 2026-07-11 ／ 実施: NOREN 開発部（タスク 39bf3e16）
- 対象: `foreglcr-svg/rakuten-room-automation`（app.py・.github/workflows/daily-catalog.yml）

## 1. 現状（良い点）

- 稼働中。毎晩 GitHub Actions が楽天市場APIで検索→スコア選定→Gemini紹介文生成→CATALOG.md更新→Issue通知。
- **設計が堅実**: ボット投稿を避け最終手動、履歴（posted_items.json 直近1000件）で重複除外、薬機法NG語辞書、フォールバックテンプレ（AI失敗でもカタログを必ず出す）、Geminiモデルは `GEMINI_MODEL` 環境変数で上書き可能。

## 2. 技術課題・改善余地

| # | 優先 | 課題 | 提案 |
|---|---|---|---|
| 1 | 中 | 依存の固定状況（requirements.txt）とCI監査の有無 | バージョン `==` 固定＋pip-audit を Actions に追加 |
| 2 | 中 | 楽天API/Gemini呼び出しの失敗時リトライ・バックオフ | 一時エラー（429/5xx）に指数バックオフ（mekiki の http 層が参考） |
| 3 | 中 | posted_items.json をコミットで肥大化・競合の恐れ | 直近1000件トリムは実装済み◎。Actionsの同時実行防止（concurrency）を明記 |
| 4 | 低 | Secrets のフォールバック（GEMINI/GOOGLE/OPENAI）が緩い | 使うキーを1つに固定し、未設定時は明示エラー |
| 5 | 低 | 実行時刻の遅延（GitHub cronは30〜90分遅れる仕様） | READMEに明記済み。実害小 |
| 6 | 低 | Actions のログにキー・トークンが出ない確認 | `_env` はstrip整形のみで出力なし◎。ログ出力箇所の再点検 |

## 3. セキュリティ観点（情シス部タスク fac41c1a と連携）

- APIキー類は GitHub Secrets 経由（app.py はenv参照のみ・実値なし）。✅
- 楽天新APIの Origin/Referer 検証に対応（`RAKUTEN_APP_URL`）。✅

## 4. 結論

**大きな技術的欠陥はなく、稼働品質は良好**。改善は「依存固定＋CI監査」「API呼び出しのリトライ」の2点が費用対効果が高い。いずれも🟡の小改善で、社長決裁不要で順次対応可能。
