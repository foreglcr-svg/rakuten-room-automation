# 楽天ROOM 半自動投稿システム

毎晩、**売れる確率が高い商品を自動リサーチ → AIが紹介文を生成 → コピペ用カタログを自動更新**するシステムです。
あなたがやることは「[CATALOG.md](./CATALOG.md) を開いてコピペ投稿する」だけ(1商品30秒)。

## ⚠️ なぜ「投稿の最後のワンタップ」だけ手動なのか

楽天ROOMには投稿用の公開APIが存在せず、ボット・スクリプトによる自動投稿は
**ROOM利用規約違反**です。発覚するとアカウント停止・成果報酬の没収につながるため、
本システムは規約の範囲内で「投稿以外のすべて」を自動化しています。

## 仕組み

```
毎晩20時頃(GitHub Actions)
  └─ 楽天市場APIで日替わりキーワード検索(売れ筋順 × 高料率順)
  └─ 「買われやすさスコア」で商品を選定
       レビュー件数(売れている証拠) / レビュー平均 / 料率 / 送料込み / ポイント倍率
  └─ AIが購買心理を組み込んだ紹介文を生成(社会的証明・ベネフィット・行動喚起)
  └─ CATALOG.md を更新してコミット(過去に紹介した商品は自動で除外)
  └─ カタログ入りのIssueを作成 → メール通知が届く(メール内でコピペ完結)
  └─ (任意) Discordに完成通知
```

## セットアップ

1. **楽天アプリIDの取得**: [Rakuten Developers](https://webservice.rakuten.co.jp/) でアプリ登録 → `applicationId` を取得
2. **GitHubリポジトリの Settings → Secrets and variables → Actions** に以下を登録

   | Secret | 必須 | 内容 |
   |---|---|---|
   | `RAKUTEN_APP_ID` | ✅ | アプリケーションID(2026年新形式: ハイフン区切りのUUID) |
   | `RAKUTEN_ACCESS_KEY` | ✅ | アクセスキー(`pk_` で始まる。2026年の新APIで必須) |
   | `GEMINI_API_KEY` | ✅ | Gemini APIキー(紹介文生成用、[Google AI Studio](https://aistudio.google.com/apikey)で無料発行) |
   | `RAKUTEN_AFFILIATE_ID` | 任意 | 楽天アフィリエイトID(カタログ内リンクも成果対象になる) |
   | `RAKUTEN_APP_URL` | 任意 | 楽天にアプリ登録した際のURL(未設定時はこのリポジトリのURL)。Origin/Referer検証に使用 |
   | `DISCORD_WEBHOOK_URL` | 任意 | カタログ完成をDiscordに通知 |

3. **main ブランチにマージ**すると毎晩20時(JST)起動で自動実行されます(GitHubの仕様で30〜90分遅れることがあります)。
   手動実行は Actions タブ → Daily ROOM Catalog → Run workflow。

## カスタマイズ([config.json](./config.json))

- `keywords`: 検索キーワード。**自分のROOMのジャンルに合わせて編集してください**(日替わりローテーション)
- `items_per_day`: 1日に紹介する商品数(ROOMのアルゴリズム的に1日3〜5投稿が最適)
- `min_price` / `max_price`: 価格帯(衝動買いされやすい1,000〜15,000円がデフォルト)
- `min_review_count` / `min_review_average`: 品質フィルター

## 📈 「最も買われる」ための運用ルール

システムが商品選定と紹介文を最適化しても、最後はROOMアカウント自体の力で差がつきます。

1. **投稿は20〜22時**(ROOMの閲覧ピーク)。カタログは朝完成しているので夜にコピペ投稿
2. **毎日続ける**: ROOMのフィードは更新頻度の高いユーザーを優遇
3. **オリジナル写真を1枚目に**: 公式画像のみの投稿よりクリック率が大幅に上がる(可能な商品だけでOK)
4. **ジャンルを絞る**: 「何でも屋」より「古着とケア用品の専門家」がフォローされ、買われる
5. **コレ!・フォロー返し**: 投稿後10分、同ジャンルの人気ユーザーに「いいね」を回ると露出が増える
6. **プロフィール整備**: 「○○好きが本当に使って良かった物だけ」等、信頼される一言を

## ローカルでの実行

```bash
pip install -r requirements.txt
export RAKUTEN_APP_ID=xxxx GEMINI_API_KEY=AIzaxxxx
python app.py
# → CATALOG.md が生成される
```
