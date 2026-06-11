"""楽天ROOM 半自動投稿システム

毎日、楽天市場APIから「売れる確率が高い商品」をスコアリングして選び、
AIで紹介文を生成し、コピペするだけで投稿できるカタログ(CATALOG.md)を更新する。

※ 楽天ROOMへの投稿そのものを自動化(ボット投稿)するとROOM利用規約違反で
   アカウント停止・成果報酬没収のリスクがあるため、最後の投稿操作のみ手動とする。
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))

def _env(name):
    # コピペ時に混入しがちな空白・改行・引用符はAPIエラーの原因になるため除去
    return (os.environ.get(name) or "").strip().strip('"').strip("'") or None


RAKUTEN_APP_ID = _env("RAKUTEN_APP_ID")
RAKUTEN_AFFILIATE_ID = _env("RAKUTEN_AFFILIATE_ID")  # 任意(あると外部リンクも成果対象)
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL") or "gemini-2.5-flash"
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")  # 任意(カタログ更新通知)

CONFIG_PATH = "config.json"
HISTORY_PATH = "posted_items.json"
CATALOG_PATH = "CATALOG.md"

ICHIBA_SEARCH_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f).get("posted", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_history(posted_codes):
    # 直近1000件だけ保持(無限肥大の防止)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"posted": posted_codes[-1000:]}, f, ensure_ascii=False, indent=2)


def todays_keywords(config):
    """日替わりでキーワードをローテーションし、毎日違う切り口の商品を発掘する"""
    keywords = config["keywords"]
    n = max(1, min(config.get("keywords_per_day", 3), len(keywords)))
    day = datetime.now(JST).timetuple().tm_yday
    start = (day * n) % len(keywords)
    return [keywords[(start + i) % len(keywords)] for i in range(n)]


def search_items(keyword, sort, use_affiliate=True):
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "keyword": keyword,
        "hits": 30,
        "sort": sort,
        "format": "json",
    }
    if RAKUTEN_AFFILIATE_ID and use_affiliate:
        params["affiliateId"] = RAKUTEN_AFFILIATE_ID
    try:
        res = requests.get(ICHIBA_SEARCH_URL, params=params, timeout=30)
        if res.status_code == 400:
            try:
                detail = res.json().get("error_description", "")
            except ValueError:
                detail = ""
            if use_affiliate and RAKUTEN_AFFILIATE_ID:
                # affiliateIdの形式不正でも全体が止まらないよう、IDなしで再試行
                print(f"  [warn] 楽天API 400 ({detail}) — affiliateIdを外して再試行します")
                time.sleep(1)
                return search_items(keyword, sort, use_affiliate=False)
            print(f"  [warn] 楽天API 400 keyword={keyword!r}: {detail}")
            return []
        res.raise_for_status()
        return res.json().get("Items", [])
    except requests.RequestException as e:
        print(f"  [warn] 楽天API取得失敗 keyword={keyword!r} sort={sort}: {e}")
        return []
    finally:
        time.sleep(1)  # 楽天APIのレートリミット(1req/秒)を厳守


def fetch_candidates(config):
    """キーワード×(売れ筋順・高料率順)で広く取得し、商品コードで重複排除"""
    candidates = {}
    for kw in todays_keywords(config):
        print(f"検索中: {kw}")
        for sort in ("-reviewCount", "-affiliateRate"):
            for item in search_items(kw, sort):
                data = item["Item"]
                data["_keyword"] = kw
                candidates.setdefault(data["itemCode"], data)
    return list(candidates.values())


def score_item(data, config):
    """『買われやすさ』を0〜100でスコアリング。条件を満たさない商品はNone。

    - レビュー件数: 実際に売れている証拠(社会的証明)。最重視。
    - レビュー平均: 満足度が低い商品は紹介しても続かない。
    - アフィリエイト料率: 同じ売れやすさなら報酬が高い方を優先。
    - 送料込み・ポイントアップ: 購入の最後のひと押しになる要素。
    """
    price = data.get("itemPrice", 0)
    if not (config["min_price"] <= price <= config["max_price"]):
        return None
    if data.get("availability", 1) != 1:  # 売り切れは除外
        return None
    review_count = data.get("reviewCount", 0)
    review_avg = float(data.get("reviewAverage", 0) or 0)
    if review_count < config["min_review_count"]:
        return None
    if review_avg < config["min_review_average"]:
        return None

    rate = float(data.get("affiliateRate", 1) or 1)
    score = 0.0
    score += min(math.log10(review_count + 1) / 4.0, 1.0) * 40
    score += (review_avg / 5.0) * 25
    score += min(rate / 8.0, 1.0) * 20
    if data.get("postageFlag") == 0:
        score += 8
    if int(data.get("pointRate", 1) or 1) > 1:
        score += 7
    return score


def select_items(candidates, config, history):
    posted = set(history)
    scored = []
    for data in candidates:
        if data["itemCode"] in posted:
            continue
        s = score_item(data, config)
        if s is not None:
            scored.append((s, data))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s, d) for s, d in scored[: config["items_per_day"]]]


def fallback_caption(data, config):
    """AI生成に失敗しても必ずカタログを出すためのテンプレート文"""
    name = data["itemName"][:60]
    tags = " ".join(config.get("hashtags_base", []))
    return (
        f"🛒 レビュー{data.get('reviewCount', 0)}件・★{data.get('reviewAverage', '-')}の人気アイテム!\n\n"
        f"{name}\n\n"
        f"実際に買った人の評価が高いので安心しておすすめできます◎\n"
        f"気になった人は今のうちにチェックしてみてください✨\n\n{tags}"
    )


def generate_caption(data, config):
    """購買心理(社会的証明・ベネフィット・行動喚起)を組み込んだ紹介文をAIで生成"""
    month = datetime.now(JST).month
    prompt = f"""あなたは楽天ROOMで月間売上トップクラスの人気インフルエンサーです。
以下の商品の、楽天ROOM用の紹介文を1つ作成してください。

【絶対条件】
- 全体で300〜400文字(厳守)。
- 1行目: 絵文字つきのキャッチコピー(スクロール中の指を止めるフック)。
- レビュー{data.get('reviewCount', 0)}件・平均★{data.get('reviewAverage', '-')}という実績(社会的証明)を自然に織り込む。
- 「この商品で生活がどう良くなるか」のベネフィットを具体的に1〜2個。
- いまは{month}月。季節感を一言入れる(無理なら省略可)。
- 最後に行動喚起を1行(例: お買い物マラソンの候補にどうぞ 等)。
- 文末にハッシュタグを4〜5個。{ ' '.join(config.get('hashtags_base', [])) } から1〜2個+商品ジャンルに合うタグ。
- 誇大表現(「絶対」「最安」「必ず痩せる」等)と効果効能の断定は禁止。

【商品名】{data['itemName']}
【価格】{data['itemPrice']}円(送料{'込み' if data.get('postageFlag') == 0 else '別'})
【商品説明】{(data.get('itemCaption') or '')[:500]}

紹介文の本文だけを出力してください。"""

    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        res.raise_for_status()
        caption = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not caption:
            raise ValueError("empty caption")
    except Exception as e:
        print(f"  [warn] AI生成失敗、テンプレ文を使用: {e}")
        caption = fallback_caption(data, config)
    finally:
        time.sleep(5)  # Gemini無料枠のレートリミット(10req/分)を厳守

    # 楽天ROOMの文字数上限を絶対に超えないための物理リミッター
    if len(caption) > 480:
        caption = caption[:475] + "..."
    return caption


def build_catalog(entries):
    now = datetime.now(JST)
    lines = [
        f"# 📲 {now:%Y/%m/%d} 楽天ROOM投稿用カタログ",
        "",
        "**使い方(1商品あたり30秒)**",
        "1. 商品リンクを開く → 楽天市場アプリの共有ボタン → 「ROOMに投稿」",
        "2. 下の紹介文ブロックを長押しコピーして貼り付け → 投稿",
        "",
        "> 💡 投稿は **20〜22時(ROOMのゴールデンタイム)** がおすすめ。",
        "",
        "---",
        "",
    ]
    for i, (score, data, caption) in enumerate(entries, 1):
        url = data.get("affiliateUrl") or data.get("itemUrl", "")
        image = data["mediumImageUrls"][0]["imageUrl"] if data.get("mediumImageUrls") else ""
        lines += [
            f"## {i}. {data['itemName']}",
            "",
            f"![商品画像]({image})" if image else "",
            "",
            f"- 💰 **{data['itemPrice']:,}円**(送料{'込み' if data.get('postageFlag') == 0 else '別'})",
            f"- ⭐ レビュー {data.get('reviewCount', 0)}件 / 平均 {data.get('reviewAverage', '-')}",
            f"- 📈 料率 {data.get('affiliateRate', '-')}% / 買われやすさスコア {score:.0f}/100",
            f"- 🔍 発掘キーワード: {data.get('_keyword', '-')}",
            "",
            f"### [👉 商品ページを開いてROOMに投稿する]({url})",
            "",
            "#### ↓ 紹介文(長押しで全選択コピー)",
            "```text",
            caption,
            "```",
            "",
            "---",
            "",
        ]
    lines.append(f"_自動生成: {now:%Y-%m-%d %H:%M} JST_\n")
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def notify_discord(count):
    if not DISCORD_WEBHOOK_URL:
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = f"https://github.com/{repo}/blob/main/CATALOG.md" if repo else "CATALOG.md"
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": f"📲 今日の楽天ROOMカタログ({count}商品)が完成! 20〜22時に投稿しよう → {url}"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [warn] Discord通知失敗: {e}")


def main():
    if not RAKUTEN_APP_ID or not GEMINI_API_KEY:
        print("Error: RAKUTEN_APP_ID と GEMINI_API_KEY を環境変数に設定してください。")
        sys.exit(1)

    # 値そのものは出さず、形式だけを診断ログに出す(Secret登録ミスの切り分け用)
    digits = "数字のみ" if RAKUTEN_APP_ID.isdigit() else "数字以外の文字を含む"
    print(f"[check] RAKUTEN_APP_ID: {len(RAKUTEN_APP_ID)}文字・{digits}(正しくは20桁の数字)")
    if not (RAKUTEN_APP_ID.isdigit() and len(RAKUTEN_APP_ID) == 20):
        print("[check] → 形式が一致しません。https://webservice.rakuten.co.jp/app/list の「アプリID/デベロッパーID」を確認してください。")
    if RAKUTEN_AFFILIATE_ID:
        print(f"[check] RAKUTEN_AFFILIATE_ID: {len(RAKUTEN_AFFILIATE_ID)}文字・ピリオド{RAKUTEN_AFFILIATE_ID.count('.')}個(正しくはピリオド3個)")
    if not RAKUTEN_AFFILIATE_ID:
        print("[warn] RAKUTEN_AFFILIATE_ID 未設定。カタログ内リンクは成果対象外になります(ROOM投稿分の報酬には影響なし)。")

    config = load_config()
    history = load_history()

    candidates = fetch_candidates(config)
    print(f"候補商品: {len(candidates)}件")
    selected = select_items(candidates, config, history)
    if not selected:
        print("条件を満たす新規商品が見つかりませんでした。config.jsonのキーワードや条件を見直してください。")
        sys.exit(1)  # 気づけるようにrunを失敗扱いにする

    entries = []
    for score, data in selected:
        print(f"紹介文生成中 (score={score:.0f}): {data['itemName'][:30]}...")
        caption = generate_caption(data, config)
        entries.append((score, data, caption))

    build_catalog(entries)
    save_history(history + [d["itemCode"] for _, d, _ in entries])
    notify_discord(len(entries))
    print(f"完了: {CATALOG_PATH} に{len(entries)}商品を出力しました。")


if __name__ == "__main__":
    main()
