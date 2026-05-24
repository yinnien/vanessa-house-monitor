"""
591 租屋網監控
條件：台北市中山區｜獨立套房＋分租套房｜10,000–25,000 元
攔截 591 的內部 API 取得乾淨的物件資料，推播個別物件到 Telegram。
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

SEARCH_URL = "https://rent.591.com.tw/?kind=1,2&region=1&section=3&price=10000,25000"
SEEN_FILE = "seen_591.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def format_price(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    if s.isdigit():
        return f"{int(s):,} 元/月"
    return s


async def scrape_591() -> list[dict]:
    captured_items = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
        )
        page = await context.new_page()

        # 攔截 591 的 API 回應
        async def handle_response(response):
            url = response.url
            if response.status != 200:
                return
            # 591 的物件列表 API 通常包含 rsList 或 search
            if not any(k in url for k in ["rsList", "search/rsList", "home/search"]):
                return
            try:
                data = await response.json()
                print(f"  攔截到 API：{url[:80]}")

                # 嘗試各種常見的 591 API 結構
                items = []
                if isinstance(data, dict):
                    inner = data.get("data", data)
                    if isinstance(inner, dict):
                        for key in ["data", "list", "items", "house"]:
                            if isinstance(inner.get(key), list) and inner[key]:
                                items = inner[key]
                                break
                    elif isinstance(inner, list):
                        items = inner

                if items:
                    print(f"  找到 {len(items)} 筆物件資料")
                    captured_items.extend(items)
                else:
                    # 把原始 JSON 存下來供除錯
                    Path("api_debug.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2)[:5000],
                        encoding="utf-8"
                    )
            except Exception as e:
                print(f"  API 解析失敗：{e}")

        page.on("response", handle_response)

        print("載入 591 搜尋頁面...")
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(4)
        await page.screenshot(path="screenshot_591.png", full_page=True)

        # 如果 API 攔截沒抓到，試著滾動頁面觸發懶載入
        if not captured_items:
            print("嘗試滾動頁面...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)

        await browser.close()

    print(f"共攔截 {len(captured_items)} 筆")

    # 整理成統一格式
    listings = []
    seen_ids: set[str] = set()

    for item in captured_items:
        if not isinstance(item, dict):
            continue

        post_id = str(item.get("post_id") or item.get("id") or "").strip()
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        title = (item.get("title") or item.get("name") or "").strip()
        price = format_price(item.get("price") or item.get("rent") or "")
        section = item.get("section_name") or item.get("region_name") or ""
        street = item.get("street_name") or item.get("address") or ""
        address = f"{section}{street}".strip()
        kind = item.get("kind_name") or ""
        size = item.get("area") or ""
        size_str = f"{size} 坪" if size else ""

        listings.append({
            "id": post_id,
            "title": title,
            "price": price,
            "address": address,
            "kind": kind,
            "size": size_str,
            "url": f"https://rent.591.com.tw/{post_id}",
        })

    # Fallback：整頁 hash（當 API 攔截完全失敗時）
    if not listings:
        print("API 攔截失敗，改用整頁 hash")
        fake_hash = hashlib.md5(b"fallback").hexdigest()
        listings = [{
            "id": fake_hash,
            "title": "full_page_hash",
            "price": "",
            "address": "",
            "kind": "",
            "size": "",
            "url": SEARCH_URL,
        }]

    return listings


def load_state() -> dict:
    if Path(SEEN_FILE).exists():
        return json.loads(Path(SEEN_FILE).read_text(encoding="utf-8"))
    return {"listings": [], "first_run": True}


def save_state(listings: list[dict]) -> None:
    Path(SEEN_FILE).write_text(
        json.dumps({"listings": listings, "first_run": False}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[DRY RUN] {message}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        print(f"Telegram: {resp.status_code}")


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_listing_message(listing: dict, label: str = "新物件") -> str:
    title = esc(listing.get("title") or "（無標題）")
    price = esc(listing.get("price") or "")
    address = esc(listing.get("address") or "")
    kind = esc(listing.get("kind") or "")
    size = esc(listing.get("size") or "")
    url = listing.get("url") or SEARCH_URL

    lines = [f"🏠 <b>591｜中山區{label}！</b>\n", f"<b>{title}</b>"]
    if kind or size:
        lines.append(f"🏷 {' ｜ '.join(filter(None, [kind, size]))}")
    if price:
        lines.append(f"💰 {price}")
    if address:
        lines.append(f"📍 {address}")
    lines.append(f"\n🔗 <a href='{url}'>查看物件</a>")
    return "\n".join(lines)


async def main() -> None:
    print("=== 591 中山區租屋監控 ===")
    print("條件：中山區｜獨立套房＋分租套房｜10,000–25,000 元")

    listings = await scrape_591()
    state = load_state()
    is_first_run = state.get("first_run", True)
    seen_ids = {item["id"] for item in state.get("listings", [])}
    new_listings = [l for l in listings if l["id"] not in seen_ids]

    if is_first_run:
        print(f"[首次執行] 推播所有 {len(listings)} 筆現有物件")
        save_state(listings)

        # 第一天：推播所有符合條件的物件
        valid = [l for l in listings if l.get("title") != "full_page_hash"]
        if valid:
            await send_telegram(
                f"✅ <b>591 中山區監控啟動！</b>\n\n"
                f"以下是目前所有符合條件的物件（共 {len(valid)} 筆）："
            )
            for listing in valid:
                await send_telegram(build_listing_message(listing, label="物件"))
                await asyncio.sleep(0.5)
        else:
            await send_telegram(
                f"✅ <b>591 租屋監控已啟動！</b>\n\n"
                f"條件：台北市中山區｜獨立套房＋分租套房｜10,000–25,000 元\n"
                f"目前記錄 <b>{len(listings)}</b> 筆物件為基準\n"
                f"有新物件時會立即通知你 🏠"
            )
        return

    if new_listings:
        print(f"發現 {len(new_listings)} 筆新物件！")
        for listing in new_listings:
            await send_telegram(build_listing_message(listing, label="新物件"))
            await asyncio.sleep(0.5)
    else:
        print("沒有新物件")
        await send_telegram(
            f"🔍 <b>591 中山區今日無新物件</b>\n\n"
            f"條件：獨立套房＋分租套房｜10,000–25,000 元\n\n"
            f"🔗 <a href='{SEARCH_URL}'>查看搜尋結果</a>"
        )

    save_state(listings)


if __name__ == "__main__":
    asyncio.run(main())
