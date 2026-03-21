from google import genai
import os
from dotenv import load_dotenv

# 1. 自動讀取 .env 檔案中的 Key
load_dotenv()

# 2. 初始化 Client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# 3. 直球對決測試模型 (使用你清單中有的 2.0-flash)
try:
    print("📡 正在發送測試請求...")
    response = client.models.generate_content(
        model="gemini-robotics-er-1.5-preview",
        contents="你好，這是一個測試，如果你收到了請回覆『連線成功』。"
    )
    
    # 4. 印出結果
    print("-" * 30)
    print(f"✅ AI 回應：{response.text}")
    print("-" * 30)

except Exception as e:
    print(f"❌ 測試失敗，原因：{e}")