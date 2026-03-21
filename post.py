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
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY")
THREADS_USER_ID       = os.environ.get("THREADS_USER_ID")
THREADS_ACCESS_TOKEN  = os.environ.get("THREADS_ACCESS_TOKEN")
THREADS_BASE          = "https://graph.threads.net/v1.0"
USED_FILE             = "used_today.json"
CASES_FILE            = "output/fundraising.json"

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
        return THREADS_ACCESS_TOKEN
    except Exception as e:
        print(f"⚠️  Token 刷新錯誤：{e}")
        return THREADS_ACCESS_TOKEN

# ══════════════════════════════════════════════════════
# Step 2：選取今日個案
# ══════════════════════════════════════════════════════

def load_used_today() -> list:
    if not os.path.exists(USED_FILE): return []
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != str(date.today()): return []
        return data.get("used_ids", [])
    except: return []

def save_used_today(used_ids: list):
    with open(USED_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": str(date.today()), "used_ids": used_ids}, f, ensure_ascii=False, indent=4)

def pick_case() -> dict:
    with open(CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("data", [])
    used_ids = load_used_today()
    available = [c for c in cases if str(c.get("id")) not in [str(i) for i in used_ids]]
    
    if not available:
        available = cases
        used_ids = []

    chosen = random.choice([c for c in available if c.get("ending_soon")] or available)
    used_ids.append(chosen.get("id"))
    save_used_today(used_ids)
    return chosen

# ══════════════════════════════════════════════════════
# Step 3：Gemini 生成文案 (修正模型名稱)
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
1. 字數 200-300 字，段落清晰。
2. 語氣真誠溫暖。
3. 結尾附上連結與 #公益 #台灣 等 3-5 個 hashtag。
4. 只輸出貼文內容。
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
# Step 4：Pollinations 生成插圖
# ══════════════════════════════════════════════════════

def get_image_url(case: dict) -> str:
    keywords = requests.utils.quote(case.get("title", "")[:15])
    prompt = requests.utils.quote("warm watercolor illustration, kindness and hope, soft colors, gentle")
    return f"https://image.pollinations.ai/prompt/{prompt},{keywords}?width=1080&height=1080&nologo=true&seed={random.randint(1,999)}"

# ══════════════════════════════════════════════════════
# Step 5：Threads 發文 (包含圖片容錯機制)
# ══════════════════════════════════════════════════════

def post_to_threads(text: str, image_url: str, token: str):
    # --- 圖片可用性檢查 ---
    if image_url:
        print(f"🖼️  正在驗證圖片是否可抓取...")
        try:
            # 預先下載圖片，確保 URI 有效且 Pollinations 已生成完成
            img_check = requests.get(image_url, timeout=20)
            if img_check.status_code != 200:
                print("⚠️  圖片抓取失敗，降級為純文字發文")
                image_url = None
        except:
            image_url = None

    url = f"{THREADS_BASE}/{THREADS_USER_ID}/threads"
    params = {"access_token": token, "text": text}
    
    if image_url:
        params["media_type"] = "IMAGE"
        params["image_url"]  = image_url
    else:
        params["media_type"] = "TEXT"

    resp = requests.post(url, params=params)
    if resp.status_code != 200:
        print(f"❌ 容器建立失敗: {resp.text}")
        return None
    
    creation_id = resp.json()["id"]
    print("⏳ 等待 30 秒讓 Threads 處理...")
    time.sleep(30)

    publish_url = f"{THREADS_BASE}/{THREADS_USER_ID}/threads_publish"
    resp_pub = requests.post(publish_url, params={"creation_id": creation_id, "access_token": token})
    return resp_pub.json().get("id") if resp_pub.status_code == 200 else None

# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

def main():
    print("="*50)
    print(f"🌉 AI Kindness Bridge 啟動 | {datetime.now()}")
    
    token = refresh_token()
    case  = pick_case()
    print(f"📂 選定個案: {case.get('title')}")

    style = random.choice(list(STYLES.keys()))
    post_text = generate_post(case, style)
    
    if "失敗" in post_text:
        print("🛑 停止任務: Gemini 無法生成文案")
        return

    print(f"\n[生成文案]\n{post_text}\n")
    
    image_url = get_image_url(case)
    post_id = post_to_threads(post_text, image_url, token)

    if post_id:
        print(f"✅ 發文成功! Post ID: {post_id}")
    else:
        print("❌ 發文失敗")
    print("="*50)

if __name__ == "__main__":
    main()