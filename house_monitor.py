"""
好室正居房源監控
每日爬取 house-cg.com.tw，發現新房源時透過 Telegram 推播。
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

SITE_URL = "https://house-cg.com.tw/"
SEEN_FILE = "seen_listings.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def scrape_listings() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print(f"載入 {SITE_URL} ...")
        await page.goto(SITE_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)  # 等待懶載入

        # 截圖存起來（GitHub Actions 可下載 debug artifact）
        await page.screenshot(path="screenshot.png", full_page=True)
        print("截圖已儲存：screenshot.png")

        # 嘗試找房源卡片（試多種 selector）
        cards = await page.evaluate("""
            () => {
                const results = [];
                const SELECTORS = [
                    '[class*="property"]', '[class*="house"]', '[class*="listing"]',
                    '[class*="room"]', '[class*="rent"]', '[class*="unit"]',
                    '[class*="card"]', 'article', 'li'
                ];

                for (const sel of SELECTORS) {
                    const els = Array.from(document.querySelectorAll(sel));
                    // 篩選：2~50 個元素、每個都有一定文字量
                    const valid = els.filter(el => el.innerText.trim().length > 20);
                    if (valid.length >= 2 && valid.length <= 50) {
                        valid.forEach(el => {
                            const text = el.innerText.trim();
                            const link = el.querySelector('a');
                            const href = link ? (link.getAttribute('href') || '') : '';
                            results.push({ selector: sel, text: text.slice(0, 300), href });
                        });
                        console.log('Matched selector:', sel, 'count:', valid.length);
                        break;
                    }
                }
                return results;
            }
        """)

        print(f"找到 {len(cards)} 個候選卡片")

        listings = []
        if cards:
            for card in cards:
                uid = hashlib.md5(
                    (card["href"] + card["text"][:80]).encode()
                ).hexdigest()[:12]
                listings.append({
                    "id": uid,
                    "title": card["text"][:120],
                    "url": card["href"],
                    "selector": card["selector"],
                })
        else:
            # fallback：對整頁主要文字做 hash，偵測任何內容變化
            print("找不到卡片結構，改用整頁 hash 模式")
            body_text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText.trim()"
            )
            page_hash = hashlib.md5(body_text.encode()).hexdigest()
            listings = [{"id": page_hash, "title": "full_page_hash", "url": SITE_URL, "selector": "body"}]

        # 儲存 HTML 供除錯
        html = await page.content()
        Path("page_dump.html").write_text(html, encoding="utf-8")

        await browser.close()
        return listings


def load_state() -> dict:
    if Path(SEEN_FILE).exists():
        return json.loads(Path(SEEN_FILE).read_text(encoding="utf-8"))
    return {"listings": [], "first_run": True}


def save_state(listings: list[dict]) -> None:
    state = {"listings": listings, "first_run": False}
    Path(SEEN_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
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


def make_url(raw: str) -> str:
    if not raw:
        return SITE_URL
    if raw.startswith("http"):
        return raw
    return "https://house-cg.com.tw" + raw


async def main() -> None:
    print("=== 好室正居房源監控 ===")

    listings = await scrape_listings()
    print(f"本次抓到 {len(listings)} 筆")

    state = load_state()
    is_first_run = state.get("first_run", True)
    seen_ids = {item["id"] for item in state.get("listings", [])}
    new_listings = [l for l in listings if l["id"] not in seen_ids]

    if is_first_run:
        print(f"[首次執行] 儲存 {len(listings)} 筆為基準，不發出通知")
        save_state(listings)
        await send_telegram(
            f"✅ <b>好室正居監控已啟動！</b>\n\n"
            f"目前記錄 <b>{len(listings)}</b> 筆資料為基準\n"
            f"有新房源釋出時會立即通知你 🏠\n\n"
            f"🔗 {SITE_URL}"
        )
        return

    if new_listings:
        print(f"發現 {len(new_listings)} 筆新房源！")
        for listing in new_listings:
            title = listing["title"].replace("<", "&lt;").replace(">", "&gt;")
            url = make_url(listing.get("url", ""))
            await send_telegram(
                f"🏠 <b>好室正居｜新房源！</b>\n\n"
                f"{title}\n\n"
                f"🔗 <a href='{url}'>前往查看</a>"
            )
    else:
        print("沒有新房源")
        await send_telegram(
            f"🔍 <b>好室正居今日無新房源</b>\n\n"
            f"🔗 {SITE_URL}"
        )

    save_state(listings)


if __name__ == "__main__":
    asyncio.run(main())
