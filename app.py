"""楽天ROOM 半自動投稿システム

毎日、楽天市場APIから「売れる確率が高い商品」をスコアリングして選び、
AIで紹介文を生成し、コピペするだけで投稿できるカタログ(CATALOG.md)を更新する。

※ 楽天ROOMへの投稿そのものを自動化(ボット投稿)するとROOM利用規約違反で
   アカウント停止・成果報酬没収のリスクがあるため、最後の投稿操作のみ手動とする。
"""

import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))

def _env(name):
    # コピペ時に混入しがちな空白・改行・引用符はAPIエラーの原因になるため除去
    return (os.environ.get(name) or "").strip().strip('"').strip("'") or None


RAKUTEN_APP_ID = _env("RAKUTEN_APP_ID")  # 新形式: ハイフン区切りのUUID
RAKUTEN_ACCESS_KEY = _env("RAKUTEN_ACCESS_KEY")  # 新APIで必須: pk_ で始まるキー
RAKUTEN_AFFILIATE_ID = _env("RAKUTEN_AFFILIATE_ID")  # 任意(あると外部リンクも成果対象)
# 新APIはアプリ登録時のURLとOrigin/Refererの一致を検証する
RAKUTEN_APP_URL = _env("RAKUTEN_APP_URL") or (
    f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}"
    if os.environ.get("GITHUB_REPOSITORY")
    else "https://example.com"
)
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL") or "gemini-2.5-flash"
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")  # 任意(カタログ更新通知)

CONFIG_PATH = "config.json"
HISTORY_PATH = "posted_items.json"
CATALOG_PATH = "CATALOG.md"

# 2026年の楽天APIインフラ刷新後の新エンドポイント(旧app.rakuten.co.jpは廃止済み)
ICHIBA_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"


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


# 月ごとの「今の気分」のトレンドキーワード(時期で可変)。
# ターゲット(アメカジ・古着・ストリート・アウトドア感度のメンズ層)に合わせた季節小物を軽く1つ足す用途。
SEASONAL_KEYWORDS = {
    1: ["ダウンジャケット メンズ", "ニットキャップ メンズ", "厚手 ネルシャツ メンズ"],
    2: ["フリース メンズ アウトドア", "ダウンベスト メンズ", "ニット メンズ"],
    3: ["コーチジャケット メンズ", "スウェットパーカー メンズ", "デニムジャケット メンズ"],
    4: ["マウンテンパーカー 薄手 メンズ", "オックスフォードシャツ メンズ", "スウェット メンズ"],
    5: ["オープンカラーシャツ メンズ", "アウトドア サンダル メンズ", "半袖シャツ アメカジ"],
    6: ["半袖 開襟シャツ メンズ", "バケットハット メンズ", "アウトドア サンダル メンズ"],
    7: ["アロハシャツ メンズ", "ショートパンツ メンズ アメカジ", "サコッシュ アウトドア"],
    8: ["開襟シャツ メンズ", "サンダル アウトドア メンズ", "バケットハット メンズ"],
    9: ["スウェット メンズ 裏毛", "コーデュロイ パンツ メンズ", "ネルシャツ メンズ"],
    10: ["フランネルシャツ メンズ", "ダウンベスト メンズ", "コーデュロイ メンズ"],
    11: ["ダウンベスト メンズ", "ニット メンズ ケーブル", "ワークブーツ メンズ"],
    12: ["ダウンジャケット メンズ", "ニットキャップ メンズ", "レザーグローブ メンズ"],
}


def seasonal_keywords():
    return SEASONAL_KEYWORDS.get(datetime.now(JST).month, [])


def _pick(items, n, rng):
    """日付シードのシャッフルからn件。同日は安定・日替わりで組み合わせが変わる。"""
    n = max(0, min(n, len(items)))
    if n == 0:
        return []
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled[:n]


def todays_keywords(config):
    """毎日フレッシュな組み合わせのキーワードを作り、季節の旬を軽く1つ足す。

    固定の組が周期的に戻ると飽きるため、キーワードは日付シードでシャッフルして
    毎日違う切り口に。さらに今月のトレンド(seasonal_per_day件)を上乗せする。
    """
    rng = _today_rng()
    picked = _pick(config["keywords"], config.get("keywords_per_day", 3), rng)
    picked += _pick(seasonal_keywords(), config.get("seasonal_per_day", 0), rng)
    # 順序を保ったまま重複を除去
    seen, out = set(), []
    for kw in picked:
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


# 大まかなカテゴリ判定用ルール(先にマッチしたものを採用)。
# 楽天のジャンルIDはバッグが複数IDに割れるなど粒度が細かすぎるため、
# 商品名/キーワードから大分類を推定してカタログ内の偏り(例: トート3連発)を防ぐ。
CATEGORY_RULES = [
    ("bag", ["バッグ", "トート", "リュック", "サコッシュ", "ショルダー", "ボディバッグ", "かばん", "鞄"]),
    ("shoes", ["スニーカー", "ブーツ", "ローファー", "サンダル", "シューズ", "革靴", "モカシン"]),
    ("hat", ["キャップ", "ハット", "帽子", "ベレー", "ニットキャップ", "ビーニー"]),
    ("accessory", ["アクセサリー", "ピアス", "イヤリング", "ネックレス", "ペンダント", "リング", "指輪", "ブレスレット", "腕時計", "シルバー", "バンダナ", "サングラス", "眼鏡", "メガネ", "財布", "ウォレット", "ベルト"]),
    ("outerwear", ["ジャケット", "パーカー", "パーカ", "ベスト", "ブルゾン", "コート", "アウター", "マウンテンパーカー", "コーチジャケット", "フリース", "ダウンジャケット", "ダウンベスト", "ブレザー"]),
    ("tops", ["シャツ", "tシャツ", "ｔシャツ", "スウェット", "ニット", "カットソー", "ネル", "カーディガン", "トレーナー", "ポロ"]),
    ("bottoms", ["パンツ", "デニム", "ジーンズ", "チノ", "カーゴ", "スラックス", "ショートパンツ", "ショーツ", "スカート"]),
    ("gear", ["ボトル", "ナルゲン", "ランタン", "テント", "チェア", "ギア"]),
]


def category_of(data):
    """商品名(なければ発掘キーワード)から大まかなカテゴリを推定する。"""
    text = (data.get("itemName", "") + " " + data.get("_keyword", "")).lower()
    for cat, words in CATEGORY_RULES:
        if any(w.lower() in text for w in words):
            return cat
    return "other"


def _api_headers():
    from urllib.parse import urlsplit

    parts = urlsplit(RAKUTEN_APP_URL)
    return {
        "Origin": f"{parts.scheme}://{parts.netloc}",
        "Referer": RAKUTEN_APP_URL,
    }


def search_items(keyword, sort, page=1, use_affiliate=True):
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "keyword": keyword,
        "hits": 30,
        "page": page,
        "sort": sort,
        "format": "json",
    }
    if RAKUTEN_AFFILIATE_ID and use_affiliate:
        params["affiliateId"] = RAKUTEN_AFFILIATE_ID
    try:
        res = requests.get(ICHIBA_SEARCH_URL, params=params, headers=_api_headers(), timeout=30)
        if res.status_code in (400, 401, 403):
            try:
                body = res.json()
                detail = body.get("error_description") or body.get("error") or str(body)[:200]
            except ValueError:
                detail = res.text[:200]
            if res.status_code == 400 and use_affiliate and RAKUTEN_AFFILIATE_ID:
                # affiliateIdの形式不正でも全体が止まらないよう、IDなしで再試行
                print(f"  [warn] 楽天API 400 ({detail}) — affiliateIdを外して再試行します")
                time.sleep(1)
                return search_items(keyword, sort, page=page, use_affiliate=False)
            print(f"  [warn] 楽天API {res.status_code} keyword={keyword!r}: {detail}")
            return []
        res.raise_for_status()
        return res.json().get("Items", [])
    except requests.RequestException as e:
        print(f"  [warn] 楽天API取得失敗 keyword={keyword!r} sort={sort}: {e}")
        return []
    finally:
        time.sleep(1)  # 楽天APIのレートリミット(1req/秒)を厳守


def _today_rng():
    """日付シードの乱数。同じ日に再実行しても結果は安定、日が変われば変わる。"""
    return random.Random(int(datetime.now(JST).strftime("%Y%m%d")))


# 売れ筋順だけだと毎回同じ定番品が出るため、複数のソート軸を日替わりで混ぜる
SORT_POOL = ["-reviewCount", "-affiliateRate", "standard", "-updateTimestamp", "+itemPrice"]


def fetch_candidates(config):
    """キーワード×(日替わりのソート軸・ページ)で広く取得し、商品コードで重複排除。

    毎回同じ商品ばかり出るのを防ぐため、ソート順とページを日替わりで変えて
    「売れ筋の定番」だけでなく、新着・中堅価格帯・別の人気商品まで候補に入れる。
    """
    rng = _today_rng()
    candidates = {}
    for kw in todays_keywords(config):
        print(f"検索中: {kw}")
        # 売れ筋(質の担保)は必ず含めつつ、残りは日替わりで別の軸を混ぜる
        sorts = ["-reviewCount"] + rng.sample(SORT_POOL[1:], 2)
        for sort in sorts:
            page = rng.randint(1, 3)  # 1ページ目の超定番だけでなく2〜3ページ目も掘る
            for item in search_items(kw, sort, page=page):
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


def _shop_of(data):
    return data["itemCode"].split(":")[0]


def select_items(candidates, config, history):
    """質の高い候補プールから、スコアを重みにした抽選で多様に選ぶ。

    単純な上位N件だと毎回同じ定番品になるため、
    ① スコア上位プールに絞って質を担保したうえで、
    ② スコアを重みにした重み付きランダムで選び、
    ③ 同一店舗は1つまで・同一キーワード/同一ジャンルは上限ありに制限して
    ジャンルや店の偏り(例: 傘ばかり)をなくす。
    """
    posted = set(history)
    excludes = config.get("exclude_keywords", [])
    n = config["items_per_day"]
    scored = []
    for data in candidates:
        if data["itemCode"] in posted:
            continue
        # feedに合わない商品(医療用ウィッグ等)は商品名で除外
        name = data.get("itemName", "")
        if any(x in name for x in excludes):
            continue
        s = score_item(data, config)
        if s is not None:
            scored.append((s, data))
    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    # 質を担保するため上位プールに限定(でも上位N件より広く取って抽選の幅を持たせる)
    pool = scored[: max(n * 6, 30)]

    rng = _today_rng()
    selected, rejected = [], []
    used_shops, used_keywords, used_categories = set(), {}, {}
    max_per_keyword = config.get("max_per_keyword", 2)  # 1キーワードから最大2つまで
    max_per_category = config.get("max_per_category", 2)  # 大分類(バッグ/靴等)ごと最大2つまで

    while pool and len(selected) < n:
        weights = [s for s, _ in pool]
        idx = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        s, d = pool.pop(idx)
        shop, kw, cat = _shop_of(d), d.get("_keyword"), category_of(d)
        if (
            shop in used_shops
            or used_keywords.get(kw, 0) >= max_per_keyword
            or (cat != "other" and used_categories.get(cat, 0) >= max_per_category)
        ):
            rejected.append((s, d))
            continue
        used_shops.add(shop)
        used_keywords[kw] = used_keywords.get(kw, 0) + 1
        used_categories[cat] = used_categories.get(cat, 0) + 1
        selected.append((s, d))

    # 多様性制約で枠が埋まらなければ、弾いた候補からスコア順に補充
    for s, d in sorted(rejected, key=lambda x: x[0], reverse=True):
        if len(selected) >= n:
            break
        selected.append((s, d))
    return selected


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


# ROOMが投稿時に弾く薬機法・健康関連のNGワードを、意味の近い安全な表現へ置換する。
# (ROOMは病名・症状名や効能の断定を「ご利用できない文字列」として拒否する)
# config.json の "ng_replacements" で追加・上書き可能。
DEFAULT_NG_REPLACEMENTS = {
    "熱中症": "夏の暑さ",
    "花粉症": "花粉",
    "感染症": "気になる季節",
    "肩こり": "肩の疲れ",
    "腰痛": "腰の負担",
    "冷え性": "冷え",
    "便秘": "すっきり",
    "不眠": "寝つき",
    "アトピー": "肌",
    "湿疹": "肌",
    "ニキビ": "肌",
    "治る": "うれしい変化",
    "完治": "うれしい変化",
    "予防します": "対策に",
    "予防できます": "対策に",
}


def sanitize_caption(caption, config):
    """ROOM投稿で弾かれる/AIっぽく見える要素を機械的に除去する安全網。

    ① マークダウン装飾(**太字**, *斜体*, #見出し, > 引用, - 箇条書き)を除去
       — ROOMでは記号がそのまま表示され、いかにもAI生成に見えるため。
    ② 薬機法・健康関連のNGワードを安全な表現に置換 — ROOMの投稿時拒否を防ぐ。
    """
    # ① マークダウン記法の除去
    caption = caption.replace("**", "").replace("__", "")
    # 見出し(# のあとに空白)だけ除去。空白なしの #楽天room はハッシュタグなので残す
    caption = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", caption)
    caption = re.sub(r"(?m)^\s{0,3}>\s?", "", caption)         # 引用
    caption = re.sub(r"(?m)^\s{0,3}[-•]\s+", "", caption)      # 箇条書き
    caption = caption.replace("*", "")                          # 残った装飾アスタリスク

    # ② NGワードの置換(デフォルト + config上書き)
    replacements = {**DEFAULT_NG_REPLACEMENTS, **config.get("ng_replacements", {})}
    for ng, safe in replacements.items():
        caption = caption.replace(ng, safe)

    # 余分な空行を整理
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    return caption


def generate_caption(data, config):
    """購買心理(社会的証明・ベネフィット・行動喚起)を組み込んだ紹介文をAIで生成"""
    month = datetime.now(JST).month
    prompt = f"""あなたはPOPEYE・2nd・GO OUTのような雑誌を愛読する、古着・アメカジ・ヴィンテージ・ストリート・アウトドアに精通したバイヤー気質のインフルエンサーです。シティボーイ/アメカジ感度の高い読者に向けて、楽天ROOM用の紹介文を1つ作成してください。

【絶対条件】
- 全体で300〜400文字(厳守)。
- 1行目: 絵文字つきのキャッチコピー(スクロール中の指を止めるフック)。
- 素材感・ディテール・着回し・モノとしての背景のいずれかに触れ、分かっている人の language で語る(例: 生地、シルエット、色落ち、無骨さ、経年変化、コーデの合わせ方)。
- レビュー{data.get('reviewCount', 0)}件・平均★{data.get('reviewAverage', '-')}という実績を、押し付けずさりげなく織り込む。
- いまは{month}月。季節感やコーデの一言を入れる(無理なら省略可)。
- トーンは大人で落ち着いた、こなれた感じ。過度に可愛い絵文字の乱用や女性誌的な甘い表現は避ける。
- 最後に行動喚起を1行(例: お買い物マラソンの候補にどうぞ 等)。
- 文末にハッシュタグを4〜5個。{ ' '.join(config.get('hashtags_base', [])) } から1〜2個+商品に合うタグ(#古着 #アメカジ #ヴィンテージ #cityboy #街コーデ 等から適切に)。

【禁止事項(厳守)】
- マークダウン記法(**太字**、*斜体*、#見出し、>引用、-箇条書き 等の記号装飾)は一切使わない。ROOMでは記号がそのまま表示されてしまう。
- 病名・症状名(熱中症・花粉症・肩こり・腰痛・冷え性 等)や、薬機法に触れる効能の断定(治る・予防・改善・殺菌で病気を防ぐ 等)は使わない。体感は「涼しく感じる」「快適」程度の主観表現にとどめる。
- 誇大表現(「絶対」「最安」「必ず痩せる」等)は使わない。
- AIっぽい誇張・定型句を避け、知人にすすめるような自然で素直な口調にする(「なんと」「まさに」「〜の救世主」「見つけちゃいました」「〜なんです」の連発を避ける)。

【商品名】{data['itemName']}
【価格】{data['itemPrice']}円(送料{'込み' if data.get('postageFlag') == 0 else '別'})
【商品説明】{(data.get('itemCaption') or '')[:500]}

紹介文の本文だけを、記号装飾なしのプレーンテキストで出力してください。"""

    caption = None
    for attempt in range(3):  # 無料枠のレートリミットに当たっても再試行で拾う
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            res.raise_for_status()
            caption = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if caption:
                break
            raise ValueError("empty caption")
        except Exception as e:
            print(f"  [warn] AI生成失敗(試行{attempt + 1}/3): {e}")
            time.sleep(20 * (attempt + 1))
    if not caption:
        print("  [warn] テンプレ文を使用します")
        caption = fallback_caption(data, config)
    time.sleep(8)  # Gemini無料枠のレートリミット(10req/分)を厳守

    # AI/テンプレ問わず、NGワード・記号装飾を機械的に除去(ROOMの投稿拒否を防ぐ)
    caption = sanitize_caption(caption, config)

    # 楽天ROOMの文字数上限を絶対に超えないための物理リミッター
    if len(caption) > 480:
        caption = caption[:475] + "..."
    return caption


def build_catalog(entries):
    now = datetime.now(JST)
    lines = [
        f"# 📲 {now:%Y/%m/%d} 楽天ROOM投稿用カタログ",
        "",
        "**使い方(1商品30秒): ① 紹介文を長押しコピー → ② リンクを開く → ③ 商品ページの「ROOMに投稿する」をタップして貼り付け**",
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
            f"- 💰 **{data['itemPrice']:,}円**(送料{'込み' if data.get('postageFlag') == 0 else '別'})"
            f" / ⭐ {data.get('reviewCount', 0)}件・平均{data.get('reviewAverage', '-')}"
            f" / 📈 料率{data.get('affiliateRate', '-')}%・スコア{score:.0f}",
            "",
            "#### ① 下の紹介文を長押しコピー",
            "```text",
            caption,
            "```",
            "",
            f"### [② ここをタップ → 商品ページの「ROOMに投稿する」→ ③ 貼り付けて投稿]({url})",
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
    if not RAKUTEN_APP_ID or not RAKUTEN_ACCESS_KEY or not GEMINI_API_KEY:
        print("Error: RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY / GEMINI_API_KEY を環境変数に設定してください。")
        sys.exit(1)

    # 値そのものは出さず、形式だけを診断ログに出す(Secret登録ミスの切り分け用)
    print(f"[check] RAKUTEN_APP_ID: {len(RAKUTEN_APP_ID)}文字・ハイフン{RAKUTEN_APP_ID.count('-')}個(新形式はハイフン4個のUUID)")
    print(f"[check] RAKUTEN_ACCESS_KEY: pk_開始={'はい' if RAKUTEN_ACCESS_KEY.startswith('pk_') else 'いいえ(正しくはpk_で始まる)'}")
    if RAKUTEN_AFFILIATE_ID:
        print(f"[check] RAKUTEN_AFFILIATE_ID: {len(RAKUTEN_AFFILIATE_ID)}文字・ピリオド{RAKUTEN_AFFILIATE_ID.count('.')}個(正しくはピリオド3個)")
    print(f"[check] Origin/Referer: {RAKUTEN_APP_URL}(楽天のアプリ登録URLと一致している必要あり)")
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
