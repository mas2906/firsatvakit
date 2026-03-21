#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon bot tespiti debug aracı.
Kullanım: python test_amazon_debug.py <Amazon URL>
"""

import asyncio, sys, re

async def main():
    if len(sys.argv) < 2:
        print("Kullanım: python test_amazon_debug.py <URL>")
        return

    url = sys.argv[1].strip()

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            locale="tr-TR",
            viewport={"width": 1366, "height": 768},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)

        print(f"📄 Sayfa başlığı: {await page.title()}")
        print(f"🌐 URL: {page.url}")

        # #productTitle var mı?
        try:
            await page.wait_for_selector("#productTitle", timeout=8000)
            title_el = await page.query_selector("#productTitle")
            title = await title_el.inner_text() if title_el else None
            print(f"✅ #productTitle bulundu: {title}")
        except Exception:
            print("❌ #productTitle 8 saniyede gelmedi")

        # CAPTCHA / robot kontrolü
        html = await page.content()
        if "robot" in html.lower() or "captcha" in html.lower():
            print("🚨 CAPTCHA/Robot sayfası tespit edildi!")
        elif "Üzgünüz" in html or "Sorry" in html:
            print("🚨 Amazon erişim engeli sayfası!")
        else:
            print("✅ Normal sayfa görünüyor")

        # Fiyat var mı?
        price_el = await page.query_selector("span.a-price span.a-offscreen")
        if price_el:
            print(f"💰 Fiyat bulundu: {await price_el.inner_text()}")
        else:
            print("❌ Fiyat bulunamadı")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
