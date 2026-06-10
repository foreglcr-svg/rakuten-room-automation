import os
import requests
from openai import OpenAI

# 1. 環境変数のセキュアな取得（RenderのEnvironment Variablesから注入）
RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def fetch_rakuten_items():
    """楽天市場APIから、自社ドメイン（古着・ケア用品など）に親和性の高い高料率商品を検索"""
    url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "keyword": "ヴィンテージ 古着 ケア",  # 必要に応じてキーワードを変更可能
        "hits": 5,                           # 1回のリサーチで取得する件数
        "sort": "-affiliateRate",            # アフィリエイト料率が高い順
        "format": "json"
    }
    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"Error fetching Rakuten API: {response.status_code}")
        return []
    return response.json().get("Items", [])

def generate_room_caption(item_name, item_caption):
    """楽天ROOM（購買意欲の高い層）の心理に刺さる訴求文をAIに生成させる"""
    client = OpenAI(api_key=OPENAI_API_KEY)
    
    prompt = f"""
    以下の楽天市場の商品情報を元に、楽天ROOMで思わず『コレ！』したくなる魅力的な紹介文を作成してください。
    
    【制約条件】
    ・文字数は絶対に300文字〜400文字の間に収めること（厳守）。
    ・冒頭にキャッチーな絵文字とフックになる1行を入れる。
    ・商品のメリット（なぜこれを買うべきか、どう生活が変わるか）を具体的に強調。
    ・文末に関連するハッシュタグ（例: #古着女子 #ヴィンテージ #おうち時間 など）を3〜5個つける。
    
    【商品名】: {item_name}
    【商品説明】: {item_caption}
    """
    
    completion = client.chat.completions.create(
        model="gpt-4o-mini", # 高速かつ低コストな最適解モデル
        messages=[{"role": "user", "content": prompt}]
    )
    
    caption = completion.choices[0].message.content
    
    # 物理リミッター（楽天ROOMの500文字制限を絶対に超えないための防衛ライン）
    if len(caption) > 480:
        caption = caption[:475] + "..."
        
    return caption

def main():
    if not RAKUTEN_APP_ID or not OPENAI_API_KEY:
        print("Error: 必須な環境変数が設定されていません。")
        return

    items = fetch_rakuten_items()
    if not items:
        print("対象商品が見つかりませんでした。")
        return

    # カタログファイルの初期化（README.mdを毎回上書きアップデートする）
    markdown_content = "# 毎日更新：楽天ROOMコピペ用カタログ\n\n"
    markdown_content += "ここに並んでいるテキストとリンクをスマホでコピーして、そのまま楽天ROOMへ投稿してください。\n\n---\n\n"
    
    for item in items:
        data = item["Item"]
        name = data["itemName"]
        affiliate_url = data["affiliateUrl"]
        image_url = data["mediumImageUrls"][0]["imageUrl"] if data["mediumImageUrls"] else ""
        raw_caption = data["itemCaption"]
        
        # AIによるテキスト生成
        print(f"Processing: {name[:20]}...")
        room_text = generate_room_caption(name, raw_caption)
        
        # コピペを極限まで楽にするUI構造の構築
        markdown_content += f"## 📦 {name}\n"
        markdown_content += f"![商品画像]({image_url})\n\n"
        markdown_content += f"### [👉 このリンクを開いてROOMで『コレ！』する]({affiliate_url})\n\n"
        markdown_content += f"#### ↓【トリプルクリックで全選択してコピー】\n```text\n{room_text}\n```\n"
        markdown_content += "\n---\n"
        
    # 生成された内容をREADME.mdとして保存する（これがGitHub上でそのままWebカタログになる）
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print("README.md の更新が正常に完了しました。")

if __name__ == "__main__":
    main()
