#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform bazlı scraper yönlendirici.

Her platform kendi dosyasında:
  scrapers/amazon.py
  scrapers/trendyol.py
  scrapers/n11.py
  scrapers/hepsiburada.py
  scrapers/utils.py  ← ortak yardımcılar
"""

from typing import Optional

from scrapers.amazon      import scrape_amazon
from scrapers.trendyol    import scrape_trendyol
from scrapers.n11         import scrape_n11
from scrapers.hepsiburada import scrape_hepsiburada


async def scrape_product(url: str, platform: str) -> Optional[dict]:
    scrapers = {
        "amazon":      scrape_amazon,
        "trendyol":    scrape_trendyol,
        "n11":         scrape_n11,
        "hepsiburada": scrape_hepsiburada,
    }
    fn = scrapers.get(platform)
    if not fn:
        print(f"[router] Desteklenmeyen platform: {platform}")
        return None
    try:
        return await fn(url)
    except Exception as e:
        print(f"[scraper/{platform}] Hata: {e}")
        return None
