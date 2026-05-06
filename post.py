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

THREADS_BASE  = "https://graph.threads.net/v1.0"
USED_FILE     = "used_today.json"

# 兩個個案庫，輪流使用
CASES_FILES = {
    "food":        "output/food.json",
    "fundraising": "output/fundraising.json",
}


# ══════════════════════════════════════════════════════
# 工具函式：自動模型切換器
# ══════════════════════════════════════════════════════

def safe_generate(client, prompt, model_order=None):
    if model_order is None:
        model_order = [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        ]
    for model in model_order:
        try:
            print(f"📡 嘗試呼叫：{model}...")
            response = client.models.generate_content(model=model, contents=prompt)
            if response and response.text:
                return response.text.strip(), model
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                print(f"⚠️  {model} 配額已滿 (429)，切換下一個...")
                continue
            else:
                print(f"❌ {model} 非預期錯誤：{err_msg}")
                continue
    return None, None


# ══════════════════════════════════════════════════════
# Step 1：Token 刷新
# ══════════════════════════════════════════════════════

def refresh_token() -> str:
    try:
        resp = requests.get(
            f"{THREADS_BASE}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": THREADS_ACCESS_TOKEN},
            timeout=10
        )
        if resp.status_code == 200:
            new_token = resp.json().get("access_token", THREADS_ACCESS_TOKEN)
            print("🔑 Token 刷新成功")
            return new_token
        return THREADS_ACCESS_TOKEN
    except Exception as e:
        print(f"⚠️  Token 刷新錯誤：{e}")
        return THREADS_ACCESS_TOKEN


# ══════════════════════════════════════════════════════
# Step 2：選取今日個案（兩個 JSON 輪流，優先 ending_soon）
# ══════════════════════════════════════════════════════

def load_used_today() -> dict:
    """回傳 { "date": "...", "used_ids": { "food": [...], "fundraising": [...] } }"""
    if not os.path.exists(USED_FILE):
        return {"date": str(date.today()), "used_ids": {"food": [], "fundraising": []}}
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != str(date.today()):
            return {"date": str(date.today()), "used_ids": {"food": [], "fundraising": []}}
        # 相容舊格式（used_ids 是 list）
        if isinstance(data.get("used_ids"), list):
            return {"date": str(date.today()), "used_ids": {"food": [], "fundraising": []}}
        return data
    except Exception:
        return {"date": str(date.today()), "used_ids": {"food": [], "fundraising": []}}


def save_used_today(used_data: dict):
    with open(USED_FILE, "w", encoding="utf-8") as f:
        json.dump(used_data, f, ensure_ascii=False, indent=4)


def pick_case() -> dict:
    used_data = load_used_today()
    used_ids  = used_data["used_ids"]

    # 決定這次從哪個庫選（哪個用得比較少就選哪個）
    food_count  = len(used_ids.get("food", []))
    fund_count  = len(used_ids.get("fundraising", []))
    source_key  = "food" if food_count <= fund_count else "fundraising"
    cases_file  = CASES_FILES[source_key]

    # 如果檔案不存在就換另一個
    if not os.path.exists(cases_file):
        source_key = "fundraising" if source_key == "food" else "food"
        cases_file = CASES_FILES[source_key]

    with open(cases_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("data", [])

    already_used = [str(i) for i in used_ids.get(source_key, [])]
    available    = [c for c in cases if str(c.get("id")) not in already_used]

    # 全部用過就重置這個庫
    if not available:
        available = cases
        used_ids[source_key] = []

    # 優先 ending_soon
    ending = [c for c in available if c.get("ending_soon")]
    chosen = random.choice(ending if ending else available)

    used_ids.setdefault(source_key, []).append(chosen.get("id"))
    used_data["used_ids"] = used_ids
    save_used_today(used_data)

    print(f"📂 個案來源：{source_key}（{cases_file}）")
    return chosen


# ══════════════════════════════════════════════════════
# Step 3：Gemini 生成文案（加入 ocr_text）
# ══════════════════════════════════════════════════════

STYLES = {
    "反問式": "以一個令人深思的反問句開頭，例如「你知道嗎？」或「如果是你，你會怎麼做？」。",
    "感動式": "直接從一個真實的生活場景切入，情緒層層遞進。",
    "科普式": "先提出一個台灣社會現況的數據或事實，再帶入個案。",
}


def generate_post(case: dict, style_name: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)

    # 從 ocr_text 取前 400 字作為補充資訊
    ocr_snippet = case.get("ocr_text", "")[:400].strip()
    ocr_section = f"【圖片補充資訊】{ocr_snippet}" if ocr_snippet else ""

    prompt = f"""
你是一位心靈大師，語文造詣跟感動眾人是你的專長。
這篇貼文的主要目的是讓看到你的文章的人想要點下網址去捐款。
可嘗試加入1~2個五官的寫實感受。
首先公益個案只是參考，讓你理解該個案的主題，我們需要創新的文章。
請根據以下公益個案，用「{style_name}」風格撰寫一篇 Threads 貼文。
風格說明：{STYLES[style_name]}

【個案標題】{case.get('title', '')}
【組織名稱】{case.get('npo_name', '')}
【個案描述】{case.get('description', '')}
{ocr_section}
【捐款連結】{case.get('link', '')}

規則：
1. 全文總字元數必須在 400 字元以內。
2. 語氣真誠溫暖。
3. 結尾附上連結與 #公益 #台灣 等 2-3 個 hashtag。
4. 連結格式 捐款連結:【捐款連結】
5. 只輸出貼文內容。
"""
    content, used_model = safe_generate(client, prompt)
    if content:
        print(f"✅ 文案生成成功 (使用: {used_model})")
        return content
    return "文案生成失敗"


# ══════════════════════════════════════════════════════
# Step 4：HuggingFace 生圖 + Cloudinary 上傳
# ══════════════════════════════════════════════════════

IMAGE_PROMPTS = {
    "募款計畫": "warm watercolor, glowing lantern over misty water, soft gold light",
    "集食送愛": "warm watercolor, steaming bowl of soup on wooden table, cozy light",
    "default":  "warm watercolor, blooming wildflowers in sunlight, soft pastel"
}


def generate_and_upload_image(case: dict, post_text: str = "") -> str:
    try:
        print("\n" + "╔" + "═"*50 + "╗")
        print("║ 🎨 Gemini 意象分析：排除圖片衝突元素...          ║")
        print("╚" + "═"*50 + "╝")

        img_client = genai.Client(api_key=GEMINI_API_KEY)
        img_analysis_prompt = f"""
Based on this charity post, create a STILL LIFE art prompt (15-20 words).
STRICT RULES:
- NO HUMANS, NO HANDS, NO FACES, NO WATERMARK.
- Focus on symbolic objects (books, light, plants, warm soup, etc.)
- Style: Dreamy warm watercolor, soft lighting.
Output only the English prompt.

Post: {post_text[:400]}
"""
        base_prompt, used_model = safe_generate(img_client, img_analysis_prompt)
        if not base_prompt:
            raise Exception("Gemini 配額用盡")
        print(f"📝 [Gemini 分析結果 ({used_model})]: {base_prompt}")

    except Exception as e:
        print(f"⚠️  分析失敗使用預設：{e}")
        base_prompt = IMAGE_PROMPTS.get(case.get("category", "default"), IMAGE_PROMPTS["default"])

    full_prompt = (
        f"{base_prompt}, masterpiece, watercolor texture, ethereal lighting, "
        "tranquil, no people, no text"
    )

    try:
        print(f"🎨 FLUX 生圖中...")
        hf_client = InferenceClient(api_key=HF_TOKEN)
        image = hf_client.text_to_image(
            prompt=full_prompt,
            model="black-forest-labs/FLUX.1-schnell",
            negative_prompt="human, person, hand, finger, face, body, silhouette, text, watermark, blurry",
        )

        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        img_b64 = base64.b64encode(buffer.getvalue()).decode()

        print("📤 上傳到 Cloudinary...")
        timestamp = str(int(time.time()))
        public_id = f"kindness_{date.today()}_{random.randint(1000, 9999)}"
        sign_str  = f"public_id={public_id}&timestamp={timestamp}{CLOUDINARY_SECRET}"
        signature = hashlib.sha256(sign_str.encode()).hexdigest()

        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/image/upload",
            data={
                "file":      f"data:image/jpeg;base64,{img_b64}",
                "public_id": public_id,
                "timestamp": timestamp,
                "api_key":   CLOUDINARY_KEY,
                "signature": signature,
            },
            timeout=60
        )
        return resp.json().get("secure_url") if resp.status_code == 200 else None

    except Exception as e:
        print(f"❌ 生圖或上傳失敗: {e}")
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

    resp = requests.post(url, params=params, timeout=15).json()
    if "id" in resp:
        return resp["id"]
    print(f"❌ 建立容器失敗: {resp}")
    return None


def wait_for_ready(creation_id: str, token: str) -> bool:
    """等待 Threads 處理媒體容器"""
    print(f"⏳ 等待 Threads 處理媒體 (ID: {creation_id})...")
    url = f"{THREADS_BASE}/{creation_id}"
    for i in range(12):
        try:
            resp   = requests.get(url, params={"fields": "status,error_message", "access_token": token}, timeout=10).json()
            status = resp.get("status")
            if status == "FINISHED":
                print("✅ 媒體處理完成")
                return True
            elif status == "ERROR":
                print(f"❌ 媒體處理失敗: {resp.get('error_message')}")
                return False
            print(f"   狀態: {status}... ({i+1}/12)")
        except Exception as e:
            print(f"⚠️  檢查狀態錯誤: {e}")
        time.sleep(5)
    print("❌ 媒體處理超時")
    return False


def publish_container(creation_id: str, token: str) -> str:
    print("🚀 執行發布...")
    url  = f"{THREADS_BASE}/{THREADS_USER_ID}/threads_publish"
    resp = requests.post(url, params={"creation_id": creation_id, "access_token": token}, timeout=15).json()
    return resp.get("id")


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print(f"Bridge 啟動  {datetime.now()}")
    print("=" * 55)

    token = refresh_token()
    case  = pick_case()
    print(f"📂 選取個案：{case.get('title')}")

    style     = random.choice(list(STYLES.keys()))
    post_text = generate_post(case, style)

    if "失敗" in post_text:
        return

    print(f"\n{'-'*50}\n{post_text}\n{'-'*50}\n")

    image_url = generate_and_upload_image(case, post_text)
    if not image_url:
        print("⚠️  圖片生成失敗，改為純文字發布。")

    try:
        c_id = create_container(post_text, image_url, token)
        if c_id:
            if image_url:
                is_ready = wait_for_ready(c_id, token)
                if not is_ready:
                    print("⚠️  媒體未準備好，發文可能失敗。")
            p_id = publish_container(c_id, token)
            if p_id:
                print(f"✅ 發文成功！貼文 ID: {p_id}")
            else:
                print("❌ 發布失敗。")
    except Exception as e:
        print(f"❌ 發文過程出錯: {e}")


if __name__ == "__main__":
    main()