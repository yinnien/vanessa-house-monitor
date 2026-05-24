"""
591 租屋網監控
條件：台北市中山區｜獨立套房＋分租套房｜25000 以下
有新物件時透過 Telegram 推播。
"""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

SEARCH_URL = "https://rent.591.com.tw/?kind=1,2&region=1&section=3&price=0,25000"
SEEN_FILE = "seen_591.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def scrape_591() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
        )
        page = await context.new_page()

        print(f"載入 591 搜尋頁面...")
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        await page.screenshot(path="screenshot_591.png", full_page=True)

        # 從頁面提取所有房源連結
        listings = await page.evaluate("""
            () => {
                const results = [];
                const seen_ids = new Set();

                // 591 的房源連結格式：https://rent.591.com.tw/[數字]
                const links = Array.from(document.querySelectorAll('a[href]'));

                links.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const match = href.match(/rent\\.591\\.com\\.tw\\/(\\d{6,})/);
                    if (!match) return;

                    const listing_id = match[1];
                    if (seen_ids.has(listing_id)) return;
                    seen_ids.add(listing_id);

                    // 往上找最近的卡片容器來抓標題、價格
                    let container = a;
                    for (let i = 0; i < 5; i++) {
                        if (container.parentElement) container = container.parentElement;
                        if (container.innerText && container.innerText.trim().length > 20) break;
                    }

                    const text = container.innerText.trim().slice(0, 200);
                    results.push({ id: listing_id, text, href });
                });

                return results;
            }
        """)

        # 也試著抓純數字路徑的連結（相對路徑格式）
        if not listings:
            listings = await page.evaluate("""
                () => {
                    const results = [];
                    const seen_ids = new Set();
                    const links = Array.from(document.querySelectorAll('a[href]'));

                    links.forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const match = href.match(/^\\/(\\d{6,})$/);
                        if (!match) return;

                        const listing_id = match[1];
                        if (seen_ids.has(listing_id)) return;
                        seen_ids.add(listing_id);

                        const text = a.innerText.trim().slice(0, 200);
                        results.push({
                            id: listing_id,
                            text,
                            href: 'https://rent.591.com.tw' + href
                        });
                    });
                    return results;
                }
            """)

        print(f"找到 {len(listings)} 筆物件")

        # 如果完全找不到連結，用整頁 hash 當 fallback
        if not listings:
            print("找不到物件連結，改用整頁 hash")
            body_text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText.trim()"
            )
            page_hash = hashlib.md5(body_text.encode()).hexdigest()
            listings = [{"id": page_hash, "text": "full_page_hash", "href": SEARCH_URL}]

        await browser.close()
        return listings


def load_state() -> dict:
    if Path(SEEN_FILE).exists():
        return json.loads(Path(SEEN_FILE).read_text(encoding="utf-8"))
    return {"listings": [], "first_run": True}


def save_state(listings: list[dict]) -> None:
    Path(SEEN_FILE).write_text(
        json.dumps({"listings": listings, "first_run": False}, ensure_ascii=False, indent=2),
        encoding="utf-8"
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


def parse_listing_info(text: str) -> tuple[str, str]:
    """從文字中試著抽出標題和價格"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else "（無標題）"

    price = ""
    for line in lines:
        if "元" in line or "$" in line:
            price = line
            break

    return title, price


async def main() -> None:
    print("=== 591 中山區租屋監控 ===")
    print(f"搜尋條件：中山區｜獨立套房＋分租套房｜25,000 以下")

    listings = await scrape_591()
    state = load_state()
    is_first_run = state.get("first_run", True)
    seen_ids = {item["id"] for item in state.get("listings", [])}
    new_listings = [l for l in listings if l["id"] not in seen_ids]

    if is_first_run:
        print(f"[首次執行] 儲存 {len(listings)} 筆為基準")
        save_state(listings)
        await send_telegram(
            f"✅ <b>591 租屋監控已啟動！</b>\n\n"
            f"條件：台北市中山區｜獨立套房＋分租套房｜25,000 元以下\n"
            f"目前記錄 <b>{len(listings)}</b> 筆物件為基準\n"
            f"有新物件時會立即通知你 🏠\n\n"
            f"🔗 <a href='{SEARCH_URL}'>查看搜尋結果</a>"
        )
        return

    if new_listings:
        print(f"發現 {len(new_listings)} 筆新物件！")
        for listing in new_listings:
            title, price = parse_listing_info(listing.get("text", ""))
            title = title.replace("<", "&lt;").replace(">", "&gt;")
            url = listing.get("href") or SEARCH_URL
            price_str = f"\n💰 {price}" if price else ""

            await send_telegram(
                f"🏠 <b>591｜中山區新物件！</b>\n\n"
                f"{title}{price_str}\n\n"
                f"🔗 <a href='{url}'>查看物件</a>"
            )
    else:
        print("沒有新物件")
        await send_telegram(
            f"🔍 <b>591 中山區今日無新物件</b>\n\n"
            f"條件：獨立套房＋分租套房｜25,000 以下\n\n"
            f"🔗 <a href='{SEARCH_URL}'>查看搜尋結果</a>"
        )

    save_state(listings)


if __name__ == "__main__":
    asyncio.run(main())
