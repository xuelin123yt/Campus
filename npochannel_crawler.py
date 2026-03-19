"""
AI 善意橋樑 — NPO Channel 爬蟲
支援四個頁面：公益募款 / 集食送愛 / 溫馨影片 / 公益夥伴（含內頁）
"""

import requests
from bs4 import BeautifulSoup
import json
import time
from datetime import datetime, date
import sys
import os

BASE_URL = "https://www.npochannel.net"

PAGES = {
    "1": {"name": "公益募款", "url": f"{BASE_URL}/Fundraising", "type": "fundraising"},
    "2": {"name": "集食送愛", "url": f"{BASE_URL}/Ad2",         "type": "food"},
    "3": {"name": "溫馨影片", "url": f"{BASE_URL}/Story",       "type": "story"},
    "4": {"name": "公益夥伴", "url": f"{BASE_URL}/Cooperation", "type": "partner"},
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


def clean_number(text: str) -> int:
    """把 '1,663,400' 或 '26個' 這類字串轉成數字"""
    try:
        return int("".join(filter(str.isdigit, text.replace(",", ""))))
    except Exception:
        return 0


def is_ending_soon(date_str: str, days: int = 30) -> bool:
    try:
        end_part = date_str.split("~")[-1].strip()
        end_date = datetime.strptime(end_part, "%Y/%m/%d").date()
        days_left = (end_date - date.today()).days
        return 0 <= days_left <= days
    except Exception:
        return False


def fetch_page(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"   ❌ 連線失敗 {url}：{e}")
        return None


# ══════════════════════════════════════════════════════
# 爬蟲：公益募款 / 集食送愛 / 溫馨影片
# ══════════════════════════════════════════════════════

def crawl_cards(url: str, page_type: str) -> list:
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

            results.append({
                "id":          i + 1,
                "type":        page_type,
                "category":    category,
                "npo_name":    npo_name,
                "title":       title,
                "description": desc,
                "link":        link,
                "image_url":   image_url,
                "date_range":  date_str,
                "ending_soon": is_ending_soon(date_str) if date_str else False,
            })

            print(f"   ✅ [{i+1}] {title[:40]}...")

        except Exception as e:
            print(f"   ⚠️  Card {i+1} 解析失敗：{e}")

    return results


# ══════════════════════════════════════════════════════
# 爬蟲：公益夥伴內頁
# ══════════════════════════════════════════════════════

def parse_partner_detail(soup) -> dict:
    """
    解析公益夥伴內頁，抓取：
    - 簡介
    - 基本資料（社區數、據點數、員工數、加入年份）
    - 公益支持（天數、NPO 數、募款計畫數）
    - 帶動捐款（單次、定期、總計）
    - 集食送愛數據
    """
    result = {}

    # ── 簡介 ──────────────────────────────────────────
    desc_tag = soup.select_one("div.uk-width-1-1 h5")
    result["description"] = clean(desc_tag.text) if desc_tag else ""

    # ── 基本資料區塊（社區數 / 據點數 / 員工數 / 加入時間）──
    # 找所有 h1.uk-heading-primary 的父層區塊
    info_blocks = soup.select(
        "div.uk-grid.uk-grid-divider.uk-grid-medium.uk-child-width-1-2 > div"
    )
    stat_keys = ["communities", "locations", "employees", "joined_year"]
    stats = {}
    for idx, block in enumerate(info_blocks[:4]):
        h1 = block.select_one("h1")
        if h1:
            # 移除 small tag 的文字，只取數字本體
            small = h1.find("small")
            if small:
                small.extract()
            stats[stat_keys[idx]] = clean_number(h1.text)
    result["stats"] = stats

    # ── 公益支持、帶動捐款、集食送愛 ──────────────────
    # 用 h3 標題來定位各個區塊
    sections = soup.select("h3.uk-heading-bullet")

    for section in sections:
        title = clean(section.text)
        panel = section.find_next_sibling("div")  # 緊接著的 div 就是數據區

        if not panel:
            continue

        # ── 公益支持 ──────────────────────────────────
        if "公益支持" in title:
            h1_tags = panel.select("h1.uk-heading-primary")
            charity = {}
            keys    = ["days", "npo_count", "fundraising_plans"]
            for idx, h1 in enumerate(h1_tags[:3]):
                small = h1.find("small")
                if small:
                    small.extract()
                if idx < len(keys):
                    charity[keys[idx]] = clean_number(h1.text)
            result["charity_impact"] = charity

        # ── 帶動捐款 ──────────────────────────────────
        elif "帶動捐款" in title:
            sub_divs = panel.select("div.uk-grid > div")
            donations = {}
            cat_map   = {0: "one_time", 1: "recurring", 2: "total"}
            for i, div in enumerate(sub_divs[:3]):
                h2s   = div.select("h2.uk-heading-primary")
                entry = {}
                sub_keys = ["people", "count", "amount"]
                for j, h2 in enumerate(h2s[:3]):
                    small = h2.find("small")
                    if small:
                        small.extract()
                    entry[sub_keys[j]] = clean_number(h2.text)
                donations[cat_map[i]] = entry
            result["donations"] = donations

        # ── 集食送愛 ──────────────────────────────────
        elif "集食送愛" in title:
            h2s      = panel.select("h2.uk-heading-primary")
            food_keys = ["people", "count", "portions", "amount"]
            food      = {}
            for j, h2 in enumerate(h2s[:4]):
                small = h2.find("small")
                if small:
                    small.extract()
                food[food_keys[j]] = clean_number(h2.text)
            result["food_delivery"] = food

    return result


def crawl_partner(url: str) -> list:
    """爬公益夥伴列表頁，再逐一進內頁抓詳細資料"""
    print(f"   🌐 連線到 {url} ...")
    soup = fetch_page(url)
    if not soup:
        return []

    cards = soup.select("div.uk-card.uk-card-default")
    print(f"   📦 找到 {len(cards)} 張 Card，開始抓內頁...")

    results = []
    for i, card in enumerate(cards):
        try:
            # ── 列表頁基本資料 ────────────────────────
            cat_tag  = card.select_one("span.cat-txt")
            category = clean(cat_tag.text) if cat_tag else ""

            img_tag   = card.select_one("div[data-src]")
            image_url = img_tag["data-src"] if img_tag else ""

            link_tag  = card.select_one("a[href*='VENDER_ID']")
            inner_url = BASE_URL + link_tag["href"] if link_tag else ""

            name_tag = card.select_one("h4")
            name     = clean(name_tag.text) if name_tag else ""

            footer_items  = card.select("div.uk-card-footer div.uk-text-small")
            org_type      = clean(footer_items[0].text) if len(footer_items) > 0 else ""
            location      = clean(footer_items[1].text) if len(footer_items) > 1 else ""

            official_tag  = card.select_one("a[href^='http']")
            official_link = official_tag["href"] if official_tag else ""

            if not name or not inner_url:
                continue

            item = {
                "id":            i + 1,
                "type":          "partner",
                "category":      category,
                "name":          name,
                "org_type":      org_type,
                "location":      location,
                "inner_url":     inner_url,
                "official_link": official_link,
                "image_url":     image_url,
            }

            # ── 進內頁抓詳細資料 ──────────────────────
            print(f"   🔍 [{i+1}/{len(cards)}] {name} → 抓取內頁...")
            detail_soup = fetch_page(inner_url)
            if detail_soup:
                detail = parse_partner_detail(detail_soup)
                item.update(detail)  # 合併到同一個 dict

            results.append(item)

            # 禮貌性延遲，避免對伺服器造成負擔
            time.sleep(0.8)

        except Exception as e:
            print(f"   ⚠️  Card {i+1} 解析失敗：{e}")

    return results


# ══════════════════════════════════════════════════════
# 儲存 JSON
# ══════════════════════════════════════════════════════

def save_json(data, filename: str):
    flat  = data if isinstance(data, list) else [i for v in data.values() for i in v]
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total":      len(flat),
        "data":       data
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n   💾 已儲存 {len(flat)} 筆資料 → {filename}")

    ending = [c for c in flat if c.get("ending_soon")]
    if ending:
        print(f"   ⏰ {len(ending)} 筆即將在 30 天內結束：")
        for c in ending:
            label = c.get("title") or c.get("name") or ""
            print(f"      → {label[:30]}...（{c.get('date_range', '')}）")


# ══════════════════════════════════════════════════════
# 執行單一頁面
# ══════════════════════════════════════════════════════

def run(choice: str) -> list:
    page = PAGES[choice]
    print(f"\n{'='*55}")
    print(f"🕷️  爬取：{page['name']}")
    print(f"{'='*55}")
    if page["type"] == "partner":
        return crawl_partner(page["url"])
    else:
        return crawl_cards(page["url"], page["type"])


# ══════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════

def main():
    print("\n" + "="*55)
    print("  🌉 AI 善意橋樑 — NPO Channel 爬蟲")
    print("="*55)
    print("  請選擇要爬取的頁面：\n")
    for key, val in PAGES.items():
        print(f"    {key}. {val['name']}")
    print("    5. 全部資料（分開儲存）")
    print("    6. 全部資料（合併成一個 JSON）")
    print("\n" + "="*55)

    choice = sys.argv[1] if len(sys.argv) > 1 else input("  請輸入選項（1-6）：").strip()

    os.makedirs("output", exist_ok=True)

    if choice in PAGES:
        data = run(choice)
        save_json(data, f"output/{PAGES[choice]['type']}.json")

    elif choice == "5":
        print("\n📂 分開儲存所有頁面...")
        for key in PAGES:
            data = run(key)
            save_json(data, f"output/{PAGES[key]['type']}.json")

    elif choice == "6":
        print("\n📂 合併儲存所有頁面...")
        all_data = {}
        for key in PAGES:
            all_data[PAGES[key]["type"]] = run(key)
        save_json(all_data, "output/all_cases.json")

    else:
        print("❌ 無效選項，請輸入 1～6")
        sys.exit(1)

    print("\n🎉 爬蟲完成！")


if __name__ == "__main__":
    main()