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
HTML_PATH = "docs/index.html"  # GitHub Pages公開用(1タップコピー+アプリ起動)

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


# 月ごとの季節キーワード(時期で可変)。セレクトショップの棚のように、季節の"暮らしのもの"を軽く1つ足す用途。
SEASONAL_KEYWORDS = {
    1: ["真鍮 ブックマーカー", "湯たんぽ おしゃれ", "レターセット 和紙"],
    2: ["チョコレート ギフト おしゃれ", "レザーグローブ メンズ", "お香 白檀"],
    3: ["一輪挿し 花瓶 ガラス", "文庫本カバー レザー", "リネン ストール"],
    4: ["ピクニック ブランケット", "帆布 トートバッグ", "レイバン サングラス"],
    5: ["アイスコーヒー ドリッパー", "リネンシャツ 無地", "ガラス タンブラー おしゃれ"],
    6: ["ガラス 風鈴 おしゃれ", "アロマ ルームミスト", "アイスコーヒー ドリッパー"],
    7: ["冷茶 グラス ガラス", "アイスコーヒー 道具", "リネンシャツ 無地"],
    8: ["アイスコーヒー ドリッパー", "ガラス タンブラー おしゃれ", "サンダル レザー"],
    9: ["ブックカバー レザー", "コーヒー ドリップポット", "リネン ストール"],
    10: ["キャンドル 秋 香り", "レコードプレーヤー アナログ", "ニット帽 おしゃれ"],
    11: ["ウール ブランケット", "お香 白檀", "万年筆 インク"],
    12: ["クリスマス オーナメント 北欧", "レザーグローブ メンズ", "ワイングラス おしゃれ"],
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
    """毎日フレッシュな組み合わせのキーワードを作る。

    ガジェット/小物が毎日必ず候補に入るよう、must_cover_categories の各カテゴリから
    最低1キーワードを確保してから、残りを日付シードのシャッフルで埋める。
    さらに今月のトレンド(seasonal_per_day件)を上乗せする。
    """
    rng = _today_rng()
    keywords = list(config["keywords"])
    n = config.get("keywords_per_day", 5)

    picked = []
    # 必ずカバーしたいカテゴリ(ガジェット・小物)から1本ずつ確保
    for cat in config.get("must_cover_categories", []):
        cands = [k for k in keywords if category_of({"itemName": k}) == cat and k not in picked]
        if cands:
            picked.append(rng.choice(cands))
    # 残り枠を、まだ選ばれていないキーワードのシャッフルで埋める
    rest = [k for k in keywords if k not in picked]
    rng.shuffle(rest)
    picked += rest[: max(0, n - len(picked))]
    # 季節キーワードを上乗せ
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
    ("bag", ["バッグ", "トート", "リュック", "バックパック", "デイパック", "ザック", "サコッシュ", "ショルダー", "ボディバッグ", "かばん", "鞄"]),
    ("shoes", ["スニーカー", "ブーツ", "ローファー", "サンダル", "シューズ", "革靴", "モカシン"]),
    ("hat", ["キャップ", "ハット", "帽子", "ベレー", "ニットキャップ", "ビーニー"]),
    ("accessory", ["アクセサリー", "ピアス", "イヤリング", "ネックレス", "ペンダント", "リング", "指輪", "ブレスレット", "腕時計", "シルバー", "バンダナ", "サングラス", "眼鏡", "メガネ", "財布", "ウォレット", "ベルト", "キーケース", "キーホルダー", "カラビナ", "靴下", "ソックス", "マネークリップ", "カードケース", "ハンカチ"]),
    # 暮らし/カルチャー系の器・花器・香り・コーヒー道具・アート雑貨(セレクトショップの棚のような枠)
    ("interior", ["マグカップ", "食器", "うつわ", "器", "皿", "プレート", "鉢", "ボウル", "花瓶", "一輪挿し", "フラワーベース", "花器", "陶器", "磁器", "porcelain", "波佐見焼", "益子焼", "信楽焼", "ホーロー", "琺瑯", "グラス", "タンブラー", "ポスター", "アートフレーム", "オブジェ", "インテリア雑貨", "ダルトン", "dulton", "プエブコ", "puebco", "キャンドル", "お香", "インセンス", "アロマ", "ディフューザー", "フレグランス", "香水", "ルームミスト", "ドリッパー", "コーヒーミル", "ケメックス", "急須", "ケトル", "コーヒーサーバー", "フレンチプレス"]),
    ("stationery", ["ノート", "手帳", "万年筆", "文房具", "文具", "ボールペン", "トラベラーズ", "traveler", "ペンケース", "筆記具", "レターセット", "マスキングテープ", "付箋", "lamy", "ラミー", "ハイタイド", "hightide", "インク"]),
    ("outerwear", ["ジャケット", "パーカー", "パーカ", "ベスト", "ブルゾン", "コート", "アウター", "マウンテンパーカー", "コーチジャケット", "フリース", "ダウンジャケット", "ダウンベスト", "ブレザー"]),
    ("tops", ["シャツ", "tシャツ", "ｔシャツ", "スウェット", "ニット", "カットソー", "ネル", "カーディガン", "トレーナー", "ポロ"]),
    ("bottoms", ["パンツ", "デニム", "ジーンズ", "チノ", "カーゴ", "スラックス", "ショートパンツ", "ショーツ", "スカート"]),
    ("gadget", ["スピーカー", "イヤホン", "ヘッドホン", "カメラ", "チェキ", "instax", "モバイルバッテリー", "ポータブル電源", "レコードプレーヤー", "ラジオ", "プロジェクター", "zippo", "ジッポ", "マルチツール", "ライター", "キーボード"]),
    ("gear", ["ボトル", "ナルゲン", "ランタン", "テント", "チェア", "ギア", "バーナー", "クッカー"]),
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

    # レビュー数偏重だと安いノーブランド量産品ばかり上位に来るため、
    # 満足度(レビュー平均)を最重視し、レビュー数と料率の比重を下げる。
    # 「一定数売れている」ことの確認程度にレビュー数を使う。
    rate = float(data.get("affiliateRate", 1) or 1)
    score = 0.0
    score += (review_avg / 5.0) * 45                                # 満足度(質)を最重視
    score += min(math.log10(review_count + 1) / 2.5, 1.0) * 25      # 売れている裏付け(頭打ち早め)
    score += min(rate / 8.0, 1.0) * 12                              # 料率は控えめに
    if data.get("postageFlag") == 0:
        score += 8
    if int(data.get("pointRate", 1) or 1) > 1:
        score += 5
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
    max_per_keyword = config.get("max_per_keyword", 2)
    max_per_category = config.get("max_per_category", 2)
    min_categories = config.get("min_categories", {})  # 例: {gadget:1, accessory:1}
    max_per_group = config.get("max_per_group", {})     # 例: {clothing: 2}
    # カテゴリ→グループの逆引き(例: tops→clothing)
    cat_to_group = {}
    for grp, cats in config.get("category_groups", {}).items():
        for c in cats:
            cat_to_group[c] = grp

    selected = []
    used_shops, used_keywords, used_categories, group_counts = set(), {}, {}, {}

    def can_take(d, relax=()):
        """relaxに含めた制約だけ緩める。キーワード/カテゴリ上限は常に厳守(重複防止の要)。"""
        shop, kw, cat = _shop_of(d), d.get("_keyword"), category_of(d)
        # ↓ここは絶対に緩めない: 同一キーワード/同一カテゴリの詰め込みを防ぐ
        if used_keywords.get(kw, 0) >= max_per_keyword:
            return False
        if cat != "other" and used_categories.get(cat, 0) >= max_per_category:
            return False
        if "shop" not in relax and shop in used_shops:
            return False
        if "group" not in relax:
            grp = cat_to_group.get(cat)
            if grp and group_counts.get(grp, 0) >= max_per_group.get(grp, 99):
                return False
        return True

    def take(entry):
        s, d = entry
        cat = category_of(d)
        used_shops.add(_shop_of(d))
        used_keywords[d.get("_keyword")] = used_keywords.get(d.get("_keyword"), 0) + 1
        used_categories[cat] = used_categories.get(cat, 0) + 1
        grp = cat_to_group.get(cat)
        if grp:
            group_counts[grp] = group_counts.get(grp, 0) + 1
        selected.append(entry)
        pool.remove(entry)

    def weighted_pick(entries):
        return entries[rng.choices(range(len(entries)), weights=[s for s, _ in entries], k=1)[0]]

    # ① ガジェット・小物などの最低保証カテゴリを先に確保
    for cat, need in min_categories.items():
        for _ in range(need):
            if len(selected) >= n:
                break
            cands = [e for e in pool if category_of(e[1]) == cat and can_take(e[1])]
            if not cands:
                break
            take(weighted_pick(cands))

    # 残り枠を段階的に制約を緩めて埋める。ただしキーワード/カテゴリ上限は常に厳守するので、
    # 同じ商品カテゴリが3つ以上・同一キーワードから2つ以上並ぶことは決して起きない。
    # ②全制約を守って → ③衣類グループ上限を緩める → ④店の重複も許容。
    # それでも足りなければ5個未満で返す(同種を詰め込むより良い)。
    for relax in ((), ("group",), ("group", "shop")):
        while pool and len(selected) < n:
            takeable = [e for e in pool if can_take(e[1], relax)]
            if not takeable:
                break
            take(weighted_pick(takeable))
    return selected


# フォールバック文が同じ文面で連発しないよう、複数パターンを商品ごとに散らす。
# モノ(器・文具・香り)にも服にも合う、審美的で落ち着いたトーンにする。
FALLBACK_TEMPLATES = [
    ("これは、長く付き合えるやつ。",
     "作りの丁寧さと佇まいの良さがちゃんとある。派手さはないけれど、毎日手に取るほどに愛着がわいてくる。暮らしにそっと馴染む一つ。",
     "気になったら、詳細を覗いてみてください。"),
    ("暮らしに、ひとつ良いものを。",
     "素材の質感やディテールに作り手のこだわりが感じられる。シンプルだからこそ長く使えて、置いておくだけで空気がすこし整う。",
     "お気に入りの棚に加えてみては。"),
    ("さりげないのに、目に留まる。",
     "無駄のないデザインと確かな質感が魅力。主張しすぎず、それでいて存在感がある。こういうものは、飽きずに使い続けられる。",
     "次の買い回り候補にぜひ。"),
    ("良いものは、静かに語る。",
     "流行りに左右されない普遍的な佇まい。使い込むほどに味が出て、自分だけの表情に育っていく。手元に置く喜びがある。",
     "この機会にチェックしてみてください。"),
]


def fallback_caption(data, config):
    """AI生成に失敗した時の予備文。アメカジ調で、かつ商品ごとに文面を散らす。"""
    name = data["itemName"][:50]
    tags = " ".join(config.get("hashtags_base", []))
    # 商品コードから安定的にパターンを選ぶ(同じ商品は常に同じ、別商品は均等に散らばる)
    import zlib

    idx = zlib.crc32(data.get("itemCode", name).encode("utf-8")) % len(FALLBACK_TEMPLATES)
    hook, body, cta = FALLBACK_TEMPLATES[idx]
    return (
        f"{hook}\n\n{name}\n\n{body}"
        f"レビュー{data.get('reviewCount', 0)}件・平均★{data.get('reviewAverage', '-')}と評価も堅い。\n\n"
        f"{cta}\n\n{tags} #cityboy"
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
    prompt = f"""あなたは代官山 蔦屋書店やHATCHのような場所で"本当に良いもの"を選ぶバイヤー/キュレーターです。VOUのようなアート/カルチャーの目線も持ち合わせています。審美眼のある大人の読者に向けて、楽天ROOM用の紹介文を1つ作成してください。

【絶対条件】
- 全体で300〜400文字(厳守)。
- 1行目: 静かに惹きつけるキャッチコピー(絵文字は0〜1個、控えめに)。
- 素材・つくり・デザイン・作り手や背景・暮らしにどう馴染むか、のいずれかに触れ、モノを見る目のある人の言葉で語る(例: 質感、佇まい、経年変化、使うシーン、合わせ方)。
- レビュー{data.get('reviewCount', 0)}件・平均★{data.get('reviewAverage', '-')}という実績を、押し付けずさりげなく織り込む。
- いまは{month}月。季節感や暮らしの一言を入れる(無理なら省略可)。
- トーンは落ち着いた大人。静かに"良さ"が伝わる文章に。
- 最後に行動喚起を1行(例: お気に入りの棚に加えてみては 等)。
- 文末にハッシュタグを4〜5個。{ ' '.join(config.get('hashtags_base', [])) } から1〜2個+商品に合うタグ(#暮らしを楽しむ #お気に入りのもの #うつわ #文房具 #コーヒーのある暮らし #古着 等から適切に)。

【禁止事項(厳守)】
- マークダウン記法(**太字**、*斜体*、#見出し、>引用、-箇条書き 等の記号装飾)は一切使わない。ROOMでは記号がそのまま表示されてしまう。
- 病名・症状名(熱中症・花粉症・肩こり・腰痛・冷え性 等)や、薬機法に触れる効能の断定(治る・予防・改善・殺菌で病気を防ぐ 等)は使わない。体感は「心地よい」「快適」程度の主観表現にとどめる。
- 誇大表現(「絶対」「最安」「必ず」等)や量産品的な煽り(「買わなきゃ損」等)は使わない。
- AIっぽい誇張・定型句や、女性誌的に甘すぎる表現・絵文字の乱用を避け、静かで品のある大人の口調にする(「なんと」「まさに」「〜の救世主」「見つけちゃいました」の連発を避ける)。

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
        # 「開く」リンクは生の商品URLを優先。アフィリのリダイレクト付きURL(hb.afl)は
        # iOSで楽天アプリのUniversal Linkを発火させずブラウザで開いてしまうため。
        # ROOM再投稿では投稿時にROOM側がアフィリを付与するので、自分のタップにアフィリは不要。
        url = data.get("itemUrl") or data.get("affiliateUrl", "")
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


def _esc(text):
    """HTMLエスケープ(属性・本文兼用)"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_html(entries):
    """スマホで1タップコピー+アプリ起動できるHTMLカタログ(GitHub Pages用)を生成。

    - 各紹介文に「コピー」ボタン(クリック内で同期的にクリップボードへ)
    - 「商品ページを開く」は生の商品URL(楽天アプリを起動させるため)
    - メールにはこのページのURL1本だけ載せ、往復をなくす
    """
    now = datetime.now(JST)
    cards = []
    for i, (score, data, caption) in enumerate(entries, 1):
        url = data.get("itemUrl") or data.get("affiliateUrl", "")
        image = data["mediumImageUrls"][0]["imageUrl"] if data.get("mediumImageUrls") else ""
        postage = "送料込み" if data.get("postageFlag") == 0 else "送料別"
        cards.append(f"""
    <article class="card">
      <div class="num">{i}</div>
      {f'<img loading="lazy" src="{_esc(image)}" alt="">' if image else ''}
      <h2>{_esc(data['itemName'])}</h2>
      <p class="meta">💰 {data['itemPrice']:,}円({postage}) ・ ⭐ {data.get('reviewCount', 0)}件 平均{data.get('reviewAverage', '-')}</p>
      <p class="step">① 紹介文をコピー</p>
      <pre class="cap" id="cap{i}">{_esc(caption)}</pre>
      <button class="copy" type="button" data-target="cap{i}">📋 紹介文をコピー</button>
      <p class="step">② 商品ページをアプリで開く → ③ 共有から「ROOMに投稿」→ 貼り付け</p>
      <a class="open" href="{_esc(url)}" target="_blank" rel="noopener">🛒 商品ページを開く</a>
    </article>""")

    html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>📲 {now:%-m/%-d} 楽天ROOMカタログ</title>
<style>
  :root {{ --bg:#f7f6f3; --card:#fff; --fg:#1a1a1a; --sub:#666; --line:#e5e3de; --accent:#bf0000; --btn:#111; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#141414; --card:#1e1e1e; --fg:#f0f0f0; --sub:#9a9a9a; --line:#333; --accent:#ff6b6b; --btn:#e8e8e8; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    line-height:1.7; -webkit-text-size-adjust:100%; }}
  header {{ padding:20px 16px 8px; }}
  header h1 {{ font-size:19px; margin:0 0 6px; }}
  header p {{ font-size:13px; color:var(--sub); margin:4px 0; }}
  .note {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:10px 12px; font-size:12.5px; color:var(--sub); margin:10px 16px; }}
  main {{ padding:8px 12px 40px; }}
  .card {{ position:relative; background:var(--card); border:1px solid var(--line);
    border-radius:14px; padding:16px; margin:0 0 18px; }}
  .num {{ position:absolute; top:-10px; left:-6px; background:var(--accent); color:#fff;
    width:26px; height:26px; border-radius:50%; display:flex; align-items:center;
    justify-content:center; font-size:14px; font-weight:700; }}
  .card img {{ width:96px; height:96px; object-fit:contain; float:right; margin:0 0 8px 10px;
    background:#fff; border-radius:8px; }}
  .card h2 {{ font-size:15px; font-weight:600; margin:4px 0 8px; }}
  .meta {{ font-size:12.5px; color:var(--sub); margin:0 0 12px; clear:both; }}
  .step {{ font-size:13px; font-weight:600; margin:14px 0 6px; }}
  .cap {{ white-space:pre-wrap; word-break:break-word; font-size:14px;
    background:var(--bg); border:1px solid var(--line); border-radius:10px;
    padding:12px; margin:0 0 10px; font-family:inherit; }}
  button.copy, a.open {{ display:block; width:100%; text-align:center; text-decoration:none;
    padding:14px; border-radius:10px; font-size:15px; font-weight:700; border:none;
    cursor:pointer; margin:0 0 6px; }}
  button.copy {{ background:var(--btn); color:var(--bg); }}
  button.copy.done {{ background:#2e7d32; color:#fff; }}
  a.open {{ background:transparent; color:var(--accent); border:2px solid var(--accent); }}
  footer {{ text-align:center; color:var(--sub); font-size:12px; padding:0 16px 30px; }}
</style>
</head>
<body>
<header>
  <h1>📲 {now:%Y/%m/%d} 楽天ROOMカタログ</h1>
  <p>① コピー → ② 商品ページを開く → ③ 共有から「ROOMに投稿」して貼り付け</p>
  <p>💡 投稿は20〜22時(ROOMのゴールデンタイム)がおすすめ</p>
</header>
<div class="note">
  ⚠️ このページは <b>Safari / Chrome</b> で開いてください。LINEやInstagram等のアプリ内ブラウザから開くと、
  「商品ページを開く」で楽天アプリが起動せず、ROOM投稿アイコンが出ないことがあります。
</div>
<main>
{''.join(cards)}
</main>
<footer>自動生成: {now:%Y-%m-%d %H:%M} JST</footer>
<script>
  document.querySelectorAll('button.copy').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var el = document.getElementById(btn.dataset.target);
      var text = el ? el.textContent : '';
      var done = function () {{
        var label = btn.textContent;
        btn.textContent = '✓ コピーしました';
        btn.classList.add('done');
        setTimeout(function () {{ btn.textContent = label; btn.classList.remove('done'); }}, 1600);
      }};
      // クリックのユーザー操作内で同期的に実行(iOS Safariでジェスチャが切れないように)
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(text).then(done, function () {{ fallback(text, done); }});
        }} else {{ fallback(text, done); }}
      }} catch (e) {{ fallback(text, done); }}
    }});
  }});
  function fallback(text, done) {{
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'absolute'; ta.style.left = '-9999px';
    document.body.appendChild(ta);
    var range = document.createRange(); range.selectNodeContents(ta);
    var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    ta.setSelectionRange(0, text.length);
    try {{ document.execCommand('copy'); done(); }} catch (e) {{}}
    document.body.removeChild(ta);
  }}
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(HTML_PATH), exist_ok=True)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


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
    build_html(entries)
    save_history(history + [d["itemCode"] for _, d, _ in entries])
    notify_discord(len(entries))
    print(f"完了: {CATALOG_PATH} と {HTML_PATH} に{len(entries)}商品を出力しました。")


if __name__ == "__main__":
    main()
