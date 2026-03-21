"""
AI 善意橋樑 — post.py
流程：選個案 → Gemini 生文案 → Pollinations 生圖 → Threads 發文
"""

import os
import json
import random
import time
import requests
from datetime import datetime, date
from dotenv import load_dotenv
from google import genai

load_dotenv()

# ── 環境變數 ────────────────────────────────────────────
GEMINI_API_KEY        = os.environ["GEMINI_API_KEY"]
THREADS_USER_ID       = os.environ["THREADS_USER_ID"]
THREADS_ACCESS_TOKEN  = os.environ["THREADS_ACCESS_TOKEN"]
THREADS_APP_ID        = os.environ["THREADS_APP_ID"]
THREADS_APP_SECRET    = os.environ["THREADS_APP_SECRET"]

THREADS_BASE  = "https://graph.threads.net/v1.0"
USED_FILE     = "used_today.json"
CASES_FILE    = "output/fundraising.json"


# ══════════════════════════════════════════════════════
# Step 1：Token 刷新
# ══════════════════════════════════════════════════════

def refresh_token() -> str:
    try:
        resp = requests.get(
            f"{THREADS_BASE}/refresh_access_token",
            params={
                "grant_type":   "th_refresh_token",
                "access_token": THREADS_ACCESS_TOKEN,
            },
            timeout=10
        )
        if resp.status_code == 200:
            new_token = resp.json().get("access_token", THREADS_ACCESS_TOKEN)
            print("🔑 Token 刷新成功")
            return new_token
        else:
            print(f"⚠️  Token 刷新失敗，使用原 Token：{resp.text}")
            return THREADS_ACCESS_TOKEN
    except Exception as e:
        print(f"⚠️  Token 刷新錯誤：{e}")
        return THREADS_ACCESS_TOKEN


# ══════════════════════════════════════════════════════
# Step 2：選取今日個案
# ══════════════════════════════════════════════════════

def load_used_today() -> list:
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != str(date.today()):
            return []
        return data.get("used_ids", [])
    except Exception:
        return []


def save_used_today(used_ids: list):
    with open(USED_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "date":     str(date.today()),
            "used_ids": used_ids
        }, f, ensure_ascii=False)


def pick_case() -> dict:
    with open(CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    cases    = data.get("data", [])
    used_ids = load_used_today()

    available = [c for c in cases if c.get("id") not in used_ids]

    if not available:
        print("⚠️  今天所有個案都用過了，重置重新開始")
        available = cases

    ending_soon = [c for c in available if c.get("ending_soon")]
    chosen = random.choice(ending_soon if ending_soon else available)

    used_ids.append(chosen.get("id"))
    save_used_today(used_ids)

    return chosen


# ══════════════════════════════════════════════════════
# Step 3：Gemini 生成文案
# ══════════════════════════════════════════════════════

STYLES = {
    "反問式": "以一個令人深思的反問句開頭，例如「你知道嗎？」或「如果是你，你會怎麼做？」，再帶入故事",
    "感動式": "直接從一個真實的生活場景切入，用細節描寫打動人心，情緒層層遞進",
    "科普式": "先提出一個台灣社會現況的數據或事實，再帶入這個個案說明問題的真實性",
}


def generate_post(case: dict, style_name: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)

    today      = datetime.now().strftime("%Y年%m月%d日")
    style_desc = STYLES[style_name]

    prompt = f"""
今天是 {today}。
請根據以下公益個案，用「{style_name}」風格撰寫一篇 Threads 貼文。

【個案標題】{case.get('title', '')}
【組織名稱】{case.get('npo_name', '')}
【類別】{case.get('category', '')}
【個案描述】{case.get('description', '')}
【捐款連結】{case.get('link', '')}

撰寫風格說明：
{style_desc}

撰寫規則：
1. 字數：200～300 字之間
2. 結構：開頭引發共鳴 → 中段說明需求 → 結尾明確行動呼籲
3. 語氣：溫暖真誠，像朋友在說話
4. 格式：Threads 段落換行，每段不超過 3 行
5. 結尾附上捐款連結
6. 最後加上 3～5 個 hashtag，包含 #公益 #台灣
7. 只輸出貼文內容，不要加任何說明文字
"""

    response = client.models.generate_content(
        model="gemini-2.0-flash-lite",
        contents=prompt
    )
    return response.text.strip()


# ══════════════════════════════════════════════════════
# Step 4：Pollinations 生成插圖
# ══════════════════════════════════════════════════════

IMAGE_PROMPTS = {
    "募款計畫": "warm watercolor illustration, people helping each other, soft warm colors, gentle, hopeful, taiwanese charity",
    "集食送愛": "warm watercolor illustration, food sharing community, elderly and children, soft colors, heartwarming",
    "溫馨影片": "warm watercolor illustration, family love, mother and child, soft pastel colors, gentle light",
    "default":  "warm watercolor illustration, kindness and hope, soft colors, gentle, community helping",
}


def get_image_url(case: dict) -> str:
    category       = case.get("category", "default")
    prompt         = IMAGE_PROMPTS.get(category, IMAGE_PROMPTS["default"])
    title_keywords = case.get("title", "")[:20]
    full_prompt    = f"{prompt}, {title_keywords}"

    encoded   = requests.utils.quote(full_prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1080&nologo=true"

    try:
        resp = requests.head(image_url, timeout=15)
        if resp.status_code == 200:
            print("🖼️  圖片生成成功")
            return image_url
        else:
            print(f"⚠️  圖片生成失敗（{resp.status_code}），改發純文字")
            return None
    except Exception as e:
        print(f"⚠️  圖片請求錯誤：{e}，改發純文字")
        return None


# ══════════════════════════════════════════════════════
# Step 5：Threads 發文
# ══════════════════════════════════════════════════════

def create_container(text: str, image_url: str, token: str) -> str:
    url    = f"{THREADS_BASE}/{THREADS_USER_ID}/threads"
    params = {
        "access_token": token,
        "text":         text,
    }

    if image_url:
        params["media_type"] = "IMAGE"
        params["image_url"]  = image_url
    else:
        params["media_type"] = "TEXT"

    resp = requests.post(url, params=params, timeout=15)

    if resp.status_code != 200:
        print(f"❌ 建立容器失敗：{resp.status_code} {resp.text}")
        resp.raise_for_status()

    return resp.json()["id"]


def publish_container(creation_id: str, token: str) -> str:
    print("⏳ 等待 30 秒讓容器準備完成...")
    time.sleep(30)

    url  = f"{THREADS_BASE}/{THREADS_USER_ID}/threads_publish"
    resp = requests.post(url, params={
        "creation_id":  creation_id,
        "access_token": token,
    }, timeout=15)

    if resp.status_code != 200:
        print(f"❌ 發布失敗：{resp.status_code} {resp.text}")
        resp.raise_for_status()

    return resp.json()["id"]


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print(f"🌉 AI Kindness Bridge 啟動  {datetime.now()}")
    print("=" * 55)

    print("\n🔑 Step 1：刷新 Token")
    token = refresh_token()

    print("\n📂 Step 2：選取個案")
    case = pick_case()
    print(f"   → {case.get('title', '')}（{case.get('npo_name', '')}）")

    style = random.choice(list(STYLES.keys()))
    print(f"\n✍️  Step 3：Gemini 生成文案（{style}）")
    post_text = generate_post(case, style)
    print(f"\n{'-'*50}")
    print(post_text)
    print(f"{'-'*50}\n")

    print("🖼️  Step 4：Pollinations 生成插圖")
    image_url = get_image_url(case)

    print("\n🚀 Step 5：發文到 Threads")
    creation_id = create_container(post_text, image_url, token)
    print(f"   → 容器建立：{creation_id}")

    post_id = publish_container(creation_id, token)
    print(f"\n✅ 發文成功！Post ID：{post_id}")
    print(f"   → 個案：{case.get('title', '')}")
    print(f"   → 風格：{style}")
    print(f"   → 時間：{datetime.now()}")
    print("=" * 55)


if __name__ == "__main__":
    main()