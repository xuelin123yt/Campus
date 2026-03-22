"""
AI 善意橋樑 — post.py
流程：選個案 → Gemini 生文案 → HuggingFace 生圖 → Cloudinary 上傳 → Threads 發文
"""

import os
import json
import random
import time
import base64
import hashlib
import requests
from io import BytesIO
from datetime import datetime, date
from dotenv import load_dotenv
from google import genai
from huggingface_hub import InferenceClient

load_dotenv()

# ── 環境變數 ────────────────────────────────────────────
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY")
THREADS_USER_ID      = os.environ.get("THREADS_USER_ID")
THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN")
HF_TOKEN             = os.environ.get("HF_TOKEN")
CLOUDINARY_CLOUD     = os.environ.get("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_KEY       = os.environ.get("CLOUDINARY_API_KEY")
CLOUDINARY_SECRET    = os.environ.get("CLOUDINARY_API_SECRET")

THREADS_BASE = "https://graph.threads.net/v1.0"
USED_FILE    = "used_today.json"
CASES_FILE   = "output/fundraising.json"


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
        print(f"⚠️  Token 刷新失敗，使用原 Token：{resp.text}")
        return THREADS_ACCESS_TOKEN
    except Exception as e:
        print(f"⚠️  Token 刷新錯誤：{e}")
        return THREADS_ACCESS_TOKEN


# ══════════════════════════════════════════════════════
# Step 2：選取今日個案（同天不重複）
# ══════════════════════════════════════════════════════

def load_used_today() -> list:
    if not os.path.exists(USED_FILE):
        return []
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
        }, f, ensure_ascii=False, indent=4)


def pick_case() -> dict:
    with open(CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    cases    = data.get("data", [])
    used_ids = load_used_today()

    available = [c for c in cases if str(c.get("id")) not in [str(i) for i in used_ids]]

    if not available:
        print("⚠️  今天所有個案都用過了，重置重新開始")
        available = cases
        used_ids  = []

    ending_soon = [c for c in available if c.get("ending_soon")]
    chosen      = random.choice(ending_soon if ending_soon else available)

    used_ids.append(chosen.get("id"))
    save_used_today(used_ids)
    return chosen


# ══════════════════════════════════════════════════════
# Step 3：Gemini 生成文案
# ══════════════════════════════════════════════════════

STYLES = {
    "反問式": "以一個令人深思的反問句開頭，例如「你知道嗎？」或「如果是你，你會怎麼做？」。",
    "感動式": "直接從一個真實的生活場景切入，情緒層層遞進。",
    "科普式": "先提出一個台灣社會現況的數據或事實，再帶入個案。",
}

def generate_post(case: dict, style_name: str) -> str:
    # 🌟 2026 最新 SDK 必須使用 Client 物件
    client = genai.Client(api_key=GEMINI_API_KEY)

    # 🌟 使用你剛才測試成功的模型名稱
    model_name = "gemini-robotics-er-1.5-preview"

    # --- 以下完全保留你原本的 Prompt 寫法 ---
    prompt = f"""
請根據以下公益個案，用「{style_name}」風格撰寫一篇 Threads 貼文。
風格說明：{STYLES[style_name]}
【個案標題】{case.get('title', '')}
【組織名稱】{case.get('npo_name', '')}
【個案描述】{case.get('description', '')}
【捐款連結】{case.get('link', '')}

規則：
1. 全文（包含 hashtag 和連結）總字元數必須在 400 字元以內，這是最重要的限制。
2. 內容要完整，有開頭、中段、結尾，不可以寫到一半就停。
3. 語氣真誠溫暖。
4. 結尾附上連結與 #公益 #台灣 等 2-3 個 hashtag。
5. 只輸出貼文內容，不要加任何說明文字。
"""
    # ---------------------------------------

    try:
        print(f"🚀 使用成功路徑：{model_name} (風格: {style_name})")

        # 🌟 對接最新 SDK 的 generate_content 呼叫方式
        response = client.models.generate_content(
            model=model_name,
            contents=prompt
        )

        if response and response.text:
            return response.text.strip()
        else:
            return "文案生成失敗：AI 回傳內容為空"

    except Exception as e:
        print(f"❌ 嘗試生成文案時發生錯誤: {e}")
        # 備援機制：如果 robotics 模型暫時無法使用，嘗試自動切換到 2.0-flash
        try:
            print("🔄 正在嘗試備援模型 gemini-2.0-flash...")
            backup_res = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt
            )
            return backup_res.text.strip()
        except:
            return "文案生成暫時失敗，請檢查 API 配額。"


# ══════════════════════════════════════════════════════
# Step 4：HuggingFace 生圖 + Cloudinary 上傳
# ══════════════════════════════════════════════════════

# 全部改成象徵性場景，不畫人物，避免詭異效果
IMAGE_PROMPTS = {
    "募款計畫": "warm watercolor illustration, glowing lantern floating over misty water, soft golden light, hope and compassion, no people, no text",
    "集食送愛": "warm watercolor illustration, steaming bowl of soup on wooden table, fresh vegetables, cozy home atmosphere, soft warm colors, no people, no text",
    "溫馨影片": "warm watercolor illustration, two pairs of hands gently holding each other, soft pink and gold tones, love and care, close up, no text",
    "default":  "warm watercolor illustration, blooming wildflowers in sunlight, soft pastel colors, gentle breeze, peaceful and hopeful, no people, no text",
}


def generate_and_upload_image(case: dict, post_text: str = "") -> str:
    """Gemini 分析文案產生 Prompt → HuggingFace 生圖 → Cloudinary 上傳"""

    # ── Step 4-0：用 Gemini 分析文案產生圖片 Prompt ───
    try:
        print("🧠 Gemini 分析文案，生成圖片 Prompt...")
        img_client = genai.Client(api_key=GEMINI_API_KEY)
        img_prompt_response = img_client.models.generate_content(
            model="gemini-robotics-er-1.5-preview",
            contents=f"""
以下是一篇公益 Threads 貼文，請根據貼文的核心情感與場景，
用英文生成一段適合 AI 繪圖的 Prompt（15-25 個英文單字）。

要求：
- 風格：warm watercolor illustration
- 情緒：溫暖、希望、關懷
- 畫自然景物、燈光、花卉、手、物品等象徵性場景，絕對不要畫人物或臉部
- 不要出現文字、logo、浮水印
- 只輸出 Prompt 本身，不要加任何說明

貼文內容：
{post_text[:400]}
"""
        )
        base_prompt = img_prompt_response.text.strip()
        print(f"   → 圖片 Prompt：{base_prompt[:80]}...")
    except Exception as e:
        print(f"⚠️  Gemini 分析失敗，使用預設 Prompt：{e}")
        category    = case.get("category", "default")
        base_prompt = IMAGE_PROMPTS.get(category, IMAGE_PROMPTS["default"])

    full_prompt = f"{base_prompt}, no people, no faces, no text, no watermark, soft warm colors"

    # ── Step 4-1：HuggingFace 生圖 ────────────────────
    try:
        print("🎨 HuggingFace 生圖中（FLUX.1-schnell）...")
        hf_client = InferenceClient(api_key=HF_TOKEN)
        image = hf_client.text_to_image(
            prompt=full_prompt,
            model="black-forest-labs/FLUX.1-schnell",
            negative_prompt="people, person, face, human, body, ugly, blurry, dark, scary, violent, text, watermark, logo, signature",
        )
        print("✅ 圖片生成成功")
    except Exception as e:
        print(f"⚠️  HuggingFace 生圖失敗：{e}，改發純文字")
        return None

    # ── Step 4-2：PIL Image 轉 base64 ─────────────────
    try:
        buffer  = BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        img_b64 = base64.b64encode(buffer.getvalue()).decode()
    except Exception as e:
        print(f"⚠️  圖片轉換失敗：{e}")
        return None

    # ── Step 4-3：上傳到 Cloudinary ───────────────────
    try:
        print("📤 上傳圖片到 Cloudinary...")
        timestamp = str(int(time.time()))
        public_id = f"kindness_{date.today()}_{random.randint(1000,9999)}"
        sign_str  = f"public_id={public_id}&timestamp={timestamp}{CLOUDINARY_SECRET}"
        signature = hashlib.sha256(sign_str.encode()).hexdigest()

        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/image/upload",
            data={
                "file":       f"data:image/jpeg;base64,{img_b64}",
                "public_id":  public_id,
                "timestamp":  timestamp,
                "api_key":    CLOUDINARY_KEY,
                "signature":  signature,
            },
            timeout=60
        )
        if resp.status_code == 200:
            url = resp.json()["secure_url"]
            print(f"✅ 圖片已上傳：{url[:60]}...")
            return url
        else:
            print(f"⚠️  Cloudinary 上傳失敗：{resp.text}")
            return None
    except Exception as e:
        print(f"⚠️  Cloudinary 上傳錯誤：{e}")
        return None


# ══════════════════════════════════════════════════════
# Step 5：Threads 發文
# ══════════════════════════════════════════════════════

def create_container(text: str, image_url: str, token: str) -> str:
    url    = f"{THREADS_BASE}/{THREADS_USER_ID}/threads"
    params = {"access_token": token, "text": text}

    if image_url:
        params["media_type"] = "IMAGE"
        params["image_url"]  = image_url
    else:
        params["media_type"] = "TEXT"

    resp = requests.post(url, params=params, timeout=15)

    if resp.status_code != 200:
        print(f"❌ 容器建立失敗：{resp.status_code} {resp.text}")
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

    style     = random.choice(list(STYLES.keys()))
    post_text = generate_post(case, style)

    if "失敗" in post_text:
        print("🛑 停止任務：文案生成失敗")
        return

    print(f"\n{'-'*50}")
    print(post_text)
    print(f"字元數：{len(post_text)}")
    print(f"{'-'*50}\n")

    print("🖼️  Step 4：生成插圖並上傳")
    image_url = generate_and_upload_image(case, post_text)

    print("\n🚀 Step 5：發文到 Threads")
    creation_id = create_container(post_text, image_url, token)
    print(f"   → 容器建立：{creation_id}")

    post_id = publish_container(creation_id, token)
    print(f"\n✅ 發文成功！Post ID：{post_id}")
    print(f"   → 個案：{case.get('title', '')}")
    print(f"   → 風格：{style}")
    print(f"   → 圖片：{'有' if image_url else '純文字'}")
    print(f"   → 時間：{datetime.now()}")
    print("=" * 55)


if __name__ == "__main__":
    main()