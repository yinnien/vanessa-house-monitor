"""
591 租屋網監控
條件：台北市中山區｜獨立套房＋分租套房｜10,000–25,000 元
有新物件時透過 Telegram 推播個別物件資訊。
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

        # 提取每筆物件的詳細資訊
        listings = await page.evaluate("""
            () => {
                const results = [];
                const seen_ids = new Set();

                // 找所有指向個別物件的連結
                const links = Array.from(document.querySelectorAll('a[href]'));

                links.forEach(a => {
                    const href = a.getAttribute('href') || '';

                    // 591 物件連結格式：包含 6 位以上數字的路徑
                    const fullMatch = href.match(/rent\\.591\\.com\\.tw\\/(\\d{6,})/);
                    const relMatch = href.match(/^\\/(\\d{6,})$/);
                    const match = fullMatch || relMatch;
                    if (!match) return;

                    const listing_id = match[1];
                    if (seen_ids.has(listing_id)) return;
                    seen_ids.add(listing_id);

                    // 往上找卡片容器（最多往上找 6 層）
                    let card = a;
                    for (let i = 0; i < 6; i++) {
                        if (!card.parentElement) break;
                        card = card.parentElement;
                        const text = card.innerText || '';
                        // 卡片通常同時包含標題和價格
                        if (text.includes('元') && text.length > 30) break;
                    }

                    const raw_text = (card.innerText || a.innerText || '').trim();
                    const lines = raw_text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                    // 從文字行中找標題、價格、地址
                    let title = '';
                    let price = '';
                    let address = '';

                    lines.forEach(line => {
                        if (!price && (line.includes('元') || line.match(/\\d+,\\d+/))) {
                            price = line;
                        } else if (!address && (line.includes('區') || line.includes('街') || line.includes('路') || line.includes('巷'))) {
                            address = line;
                        } else if (!title && line.length > 4 && !line.match(/^\\d/) && !line.includes('坪') && !line.includes('樓')) {
                            title = line;
                        }
                    });

                    // 如果標題沒找到，用第一行
                    if (!title && lines.length > 0) title = lines[0];

                    const full_url = fullMatch
                        ? href
                        : 'https://rent.591.com.tw' + href;

                    results.push({ id: listing_id, title, price, address, url: full_url });
                });

                return results;
            }
        """)

        print(f"找到 {len(listings)} 筆物件")

        # Fallback：整頁 hash
        if not listings:
            print("找不到物件連結，改用整頁 hash")
            body_text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText.trim()"
            )
            page_hash = hashlib.md5(body_text.encode()).hexdigest()
            listings = [{"id": page_hash, "title": "full_page_hash", "price": "", "address": "", "url": SEARCH_URL}]

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


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def main() -> None:
    print("=== 591 中山區租屋監控 ===")
    print("條件：中山區｜獨立套房＋分租套房｜10,000–25,000 元")

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
            f"條件：台北市中山區｜獨立套房＋分租套房｜10,000–25,000 元\n"
            f"目前記錄 <b>{len(listings)}</b> 筆物件為基準\n"
            f"有新物件時會立即通知你 🏠\n\n"
            f"🔗 <a href='{SEARCH_URL}'>查看搜尋結果</a>"
        )
        return

    if new_listings:
        print(f"發現 {len(new_listings)} 筆新物件！")
        for listing in new_listings:
            title = escape(listing.get("title") or "（無標題）")
            price = escape(listing.get("price") or "")
            address = escape(listing.get("address") or "")
            url = listing.get("url") or SEARCH_URL

            price_line = f"\n💰 {price}" if price else ""
            address_line = f"\n📍 {address}" if address else ""

            await send_telegram(
                f"🏠 <b>591｜中山區新物件！</b>\n\n"
                f"<b>{title}</b>"
                f"{price_line}"
                f"{address_line}\n\n"
                f"🔗 <a href='{url}'>查看物件</a>"
            )
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
