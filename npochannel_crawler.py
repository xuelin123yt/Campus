"""
AI 善意橋樑 — NPO Channel 爬蟲
支援兩個頁面：公益募款 / 集食送愛（含內頁 Tesseract OCR 圖片文字擷取）
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import sys
import os
import re
from datetime import datetime, date
from io import BytesIO

# OCR 相關
try:
    from PIL import Image
    import pytesseract
    # 自動判斷環境：Windows 需要指定路徑，Linux (GitHub Actions) 不需要
    if os.name == "nt":
        pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

BASE_URL = "https://www.npochannel.net"

PAGES = {
    "1": {"name": "公益募款", "url": f"{BASE_URL}/Fundraising", "type": "fundraising"},
    "2": {"name": "集食送愛", "url": f"{BASE_URL}/Ad2",         "type": "food"},
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ══════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════

def clean(text: str) -> str:
    return text.strip().replace("\xa0", " ").replace("\r\n", " ").replace("\n", " ")


def is_ending_soon(date_str: str, days: int = 30) -> bool:
    try:
        end_part = date_str.split("~")[-1].strip()
        end_date = datetime.strptime(end_part, "%Y/%m/%d").date()
        days_left = (end_date - date.today()).days
        return 0 <= days_left <= days
    except Exception:
        return False


def fetch_page(url: str, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"   ⚠️  第 {attempt} 次失敗：{e}")
            if attempt < retries:
                wait = attempt * 5
                print(f"   ⏳ 等待 {wait} 秒後重試...")
                time.sleep(wait)
    print(f"   ❌ 連線失敗 {url}，已重試 {retries} 次")
    return None


# ══════════════════════════════════════════════════════
# OCR：下載圖片並用 Tesseract 擷取文字
# ══════════════════════════════════════════════════════

def clean_ocr(text: str) -> str:
    """清理 OCR 雜訊：按行過濾，中文字數不足的行丟掉"""
    cleaned = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        chinese_count = len(re.findall(r'[\u4e00-\u9fff]', line))
        total_len = len(line.replace(" ", ""))
        # 保留：至少4個中文字，或中文佔比超過30%
        if chinese_count >= 4 or (total_len > 0 and chinese_count / total_len > 0.3):
            cleaned.append(line)
    return "\n".join(cleaned)


def ocr_from_url(image_url: str) -> str:
    if not OCR_AVAILABLE:
        return ""
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        raw = resp.content
        if len(raw) < 1000:
            return ""

        try:
            img = Image.open(BytesIO(raw))
            img.verify()
            img = Image.open(BytesIO(raw)).convert("RGB")
        except Exception:
            return ""

        w, h = img.size
        all_lines = []

        if h > 2000:
            chunk_h = 1500
            overlap = 100
            y = 0
            while y < h:
                chunk = img.crop((0, y, w, min(y + chunk_h, h)))
                chunk = chunk.resize((w * 2, chunk.height * 2), Image.LANCZOS)
                text = pytesseract.image_to_string(chunk, lang="chi_tra+eng")
                all_lines.extend(text.splitlines())
                y += chunk_h - overlap
        else:
            img = img.resize((w * 2, h * 2), Image.LANCZOS)
            text = pytesseract.image_to_string(img, lang="chi_tra+eng")
            all_lines = text.splitlines()

        return clean_ocr("\n".join(all_lines))

    except Exception as e:
        print(f"   ⚠️  OCR 失敗：{e}")
        return ""


# ══════════════════════════════════════════════════════
# 爬蟲：公益募款內頁文章
# ══════════════════════════════════════════════════════

def parse_fundraising_detail(soup) -> dict:
    result = {}

    content = soup.select_one("div.uk-width-2-3\\@m")
    if not content:
        content = soup.select_one("div.uk-container.uk-container-small")
    if not content:
        return result

    labels = content.select("span.uk-label")
    result["labels"] = [clean(l.text) for l in labels]

    meta_tags = content.select("p.uk-article-meta")
    result["permit_number"] = ""
    result["date_range"]    = ""
    for meta in meta_tags:
        text = clean(meta.text)
        if "勸募字號" in text:
            result["permit_number"] = text.replace("勸募字號:", "").replace("勸募字號：", "").strip()
        elif "~" in text or "/" in text:
            result["date_range"] = text.strip()

    paragraphs = []
    for tag in content.select("p, h4.uk-h4, p.uk-dropcap"):
        text = tag.get_text(separator=" ", strip=True)
        text = text.replace("\xa0", " ").replace("\r\n", " ").replace("\n", " ").strip()
        if text and len(text) > 10 and "勸募字號" not in text:
            if text not in paragraphs:
                paragraphs.append(text)

    result["article_paragraphs"] = paragraphs
    result["full_article"]       = "\n\n".join(paragraphs)

    images = []
    for img in content.select("figure img"):
        src = img.get("src", "")
        if src and "npochannel.net" in src and src != f"{BASE_URL}/":
            images.append(src)
    result["article_images"] = images

    return result


# ══════════════════════════════════════════════════════
# 爬蟲：集食送愛內頁（抓圖片並批次 OCR）
# ══════════════════════════════════════════════════════

def parse_food_detail(soup, cover_url: str = "") -> dict:
    result = {"article_images": [], "ocr_text": ""}

    content = soup.select_one("div.uk-width-2-3\\@m")
    if not content:
        content = soup.select_one("div.uk-container.uk-container-small")
    if not content:
        content = soup

    image_urls = []
    for img in content.select("img"):
        src = img.get("src", "").strip() or img.get("data-src", "").strip()
        if not src or "npochannel.net" not in src:
            continue
        if src.startswith("/"):
            src = BASE_URL + src
        if src == cover_url:  # 過濾掉列表封面圖
            continue
        if src not in image_urls:
            image_urls.append(src)

    result["article_images"] = image_urls

    if not image_urls:
        print(f"      → 內頁找不到任何圖片")
        return result

    print(f"      → 找到 {len(image_urls)} 張圖片，開始批次 OCR...")

    ocr_parts = []
    for idx, img_url in enumerate(image_urls, 1):
        text = ocr_from_url(img_url)
        if text:
            ocr_parts.append(f"圖{idx}: {text}")
            print(f"         圖{idx}：擷取到 {len(text)} 字元")
        else:
            print(f"         圖{idx}：無文字或跳過")
        time.sleep(0.3)

    result["ocr_text"] = "\n\n".join(ocr_parts)
    return result


# ══════════════════════════════════════════════════════
# 爬蟲：公益募款（列表頁 + 內頁）
# ══════════════════════════════════════════════════════

def crawl_fundraising(url: str) -> list:
    print(f"   🌐 連線到 {url} ...")
    soup = fetch_page(url)
    if not soup:
        return []

    cards = soup.select("div.uk-card.uk-card-default.uk-card-hover")
    print(f"   📦 找到 {len(cards)} 張 Card")

    results = []
    for i, card in enumerate(cards):
        try:
            label_tag = card.select_one("span[class*='uk-label'] a")
            category  = clean(label_tag.text) if label_tag else "未分類"

            npo_tag  = card.select_one("span.cat-txt a")
            npo_name = clean(npo_tag.text) if npo_tag else ""

            link_tag = card.select_one("a[href*='CARD_ID']")
            link     = BASE_URL + link_tag["href"] if link_tag else ""

            img_tag   = card.select_one("div[data-src]")
            image_url = img_tag["data-src"] if img_tag else ""

            title_tag = card.select_one("h5 a")
            title     = clean(title_tag.text) if title_tag else ""

            desc_tag = card.select_one("p.uk-text-small.uk-text-muted")
            desc     = clean(desc_tag.text) if desc_tag else ""

            date_tag = card.select_one("div.uk-width-expand.uk-text-small p")
            date_str = clean(date_tag.text) if date_tag else ""

            if not title or not link:
                continue

            item = {
                "id":          i + 1,
                "type":        "fundraising",
                "category":    category,
                "npo_name":    npo_name,
                "title":       title,
                "description": desc,
                "link":        link,
                "image_url":   image_url,
                "date_range":  date_str,
                "ending_soon": is_ending_soon(date_str) if date_str else False,
            }

            print(f"   🔍 [{i+1}/{len(cards)}] {title[:30]}... → 抓取內頁")
            detail_soup = fetch_page(link)
            if detail_soup:
                detail = parse_fundraising_detail(detail_soup)
                item.update(detail)
            time.sleep(0.8)

            results.append(item)

        except Exception as e:
            print(f"   ⚠️  Card {i+1} 解析失敗：{e}")

    return results


# ══════════════════════════════════════════════════════
# 爬蟲：集食送愛（列表頁 + 內頁圖片批次 OCR）
# ══════════════════════════════════════════════════════

def crawl_food(url: str) -> list:
    print(f"   🌐 連線到 {url} ...")
    soup = fetch_page(url)
    if not soup:
        return []

    cards = soup.select("div.uk-card.uk-card-default.uk-card-hover")
    print(f"   📦 找到 {len(cards)} 張 Card")
    if OCR_AVAILABLE:
        print("   🔤 OCR 已啟用（Tesseract 繁中+英文）")
    else:
        print("   ⚠️  OCR 未啟用，article_images 仍會抓取但不跑 OCR")

    results = []
    for i, card in enumerate(cards):
        try:
            label_tag = card.select_one("span[class*='uk-label'] a")
            category  = clean(label_tag.text) if label_tag else "集食送愛"

            link_tag = card.select_one("a[href*='CARD_ID']")
            link     = BASE_URL + link_tag["href"] if link_tag else ""

            img_tag   = card.select_one("div[data-src]")
            image_url = img_tag["data-src"] if img_tag else ""

            title_tag = card.select_one("h5 a")
            title     = clean(title_tag.text) if title_tag else ""

            desc_tag = card.select_one("p.uk-text-small.uk-text-muted")
            desc     = clean(desc_tag.text) if desc_tag else ""

            date_tag = card.select_one("div.uk-width-expand.uk-text-small p")
            date_str = clean(date_tag.text) if date_tag else ""

            if not title or not link:
                continue

            print(f"   🔍 [{i+1}/{len(cards)}] {title[:30]}... → 進入內頁")

            article_images = []
            ocr_text       = ""
            detail_soup = fetch_page(link)
            if detail_soup:
                detail         = parse_food_detail(detail_soup, cover_url=image_url)
                article_images = detail.get("article_images", [])
                ocr_text       = detail.get("ocr_text", "")

            # 合併描述 + OCR 文字作為 full_article
            combined = []
            if desc:
                combined.append(desc)
            if ocr_text:
                combined.append(f"[圖片文字]\n{ocr_text}")
            full_article = "\n\n".join(combined)

            item = {
                "id":             i + 1,
                "type":           "food",
                "category":       category,
                "npo_name":       "",
                "title":          title,
                "description":    desc,
                "ocr_text":       ocr_text,
                "full_article":   full_article,
                "link":           link,
                "image_url":      image_url,
                "article_images": article_images,
                "date_range":     date_str,
                "ending_soon":    is_ending_soon(date_str) if date_str else False,
            }

            results.append(item)
            time.sleep(0.8)

        except Exception as e:
            print(f"   ⚠️  Card {i+1} 解析失敗：{e}")

    return results


# ══════════════════════════════════════════════════════
# 儲存 JSON
# ══════════════════════════════════════════════════════

def save_json(data: list, filename: str):
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total":      len(data),
        "data":       data
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n   💾 已儲存 {len(data)} 筆資料 → {filename}")

    ending = [c for c in data if c.get("ending_soon")]
    if ending:
        print(f"   ⏰ {len(ending)} 筆即將在 30 天內結束：")
        for c in ending:
            print(f"      → {c.get('title','')[:30]}...（{c.get('date_range','')}）")


# ══════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════

def run(choice: str) -> list:
    page = PAGES[choice]
    print(f"\n{'='*55}")
    print(f"🕷️  爬取：{page['name']}")
    print(f"{'='*55}")

    if page["type"] == "fundraising":
        return crawl_fundraising(page["url"])
    elif page["type"] == "food":
        return crawl_food(page["url"])
    return []


def main():
    print("\n" + "="*55)
    print("  🌉 AI 善意橋樑 — NPO Channel 爬蟲")
    print("="*55)
    print("  請選擇要爬取的頁面：\n")
    for key, val in PAGES.items():
        print(f"    {key}. {val['name']}")
    print("    3. 全部（分開儲存）")
    print("="*55)

    choice = sys.argv[1] if len(sys.argv) > 1 else input("  請輸入選項（1-3）：").strip()

    os.makedirs("output", exist_ok=True)

    if choice in PAGES:
        data = run(choice)
        save_json(data, f"output/{PAGES[choice]['type']}.json")

    elif choice == "3":
        print("\n📂 爬取所有頁面...")
        for key in PAGES:
            data = run(key)
            save_json(data, f"output/{PAGES[key]['type']}.json")

    else:
        print("❌ 無效選項，請輸入 1、2 或 3")
        sys.exit(1)

    print("\n🎉 爬蟲完成！")


if __name__ == "__main__":
    main()