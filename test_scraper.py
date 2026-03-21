#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manuel scraper test aracı.
Kullanım: python test_scraper.py <URL>
Örnek:    python test_scraper.py https://www.trendyol.com/...
"""

import asyncio, sys
from scrapers.router import scrape_product
from scraper_router import detect_platform

async def main():
    if len(sys.argv) < 2:
        print("Kullanım: python test_scraper.py <URL>")
        print("Örnek:    python test_scraper.py https://www.amazon.com.tr/dp/...")
        return

    url = sys.argv[1].strip()
    platform = detect_platform(url)

    if not platform:
        print(f"❌ Desteklenmeyen platform: {url}")
        return

    print(f"🔍 Platform: {platform}")
    print(f"🌐 URL: {url}")
    print("-" * 60)

    data = await scrape_product(url, platform)

    if not data:
        print("❌ Veri çekilemedi!")
        return

    print(f"✅ Başlık   : {data.get('title')}")
    print(f"💰 Fiyat    : {data.get('price')} TL")
    print(f"🖼  Resim    : {data.get('image_url','–')[:60]}...")
    print(f"⭐ Rating   : {data.get('rating')}")
    print(f"💬 Yorum    : {data.get('review_count')}")
    print(f"📦 Stok     : {data.get('stock','–')}")
    if data.get('brand'):
        print(f"🏷  Marka    : {data.get('brand')}")
    if data.get('seller'):
        print(f"🏪 Satıcı   : {data.get('seller')}")
    if data.get('barcode'):
        print(f"📊 Barkod   : {data.get('barcode')}")

if __name__ == "__main__":
    asyncio.run(main())
