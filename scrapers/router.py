#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform bazlı scraper yönlendirici — 4 katmanlı CDP mimarisi.

Katman 0 (Trendyol): Public API → tarayıcı yok
Katman 1: curl_cffi stream_fetch
Katman 2-3: Playwright (BrowserPool varsa pooled page, yoksa yeni browser)
"""

from typing import Optional

from scrapers.amazon      import scrape_amazon
from scrapers.trendyol    import scrape_trendyol
from scrapers.n11         import scrape_n11
from scrapers.hepsiburada import scrape_hepsiburada


async def scrape_product(url: str, platform: str, pool=None, price_only: bool = False,
                         cached_image: str = None) -> Optional[dict]:
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
        return await fn(url, pool=pool, price_only=price_only, cached_image=cached_image)
    except Exception as e:
        print(f"[scraper/{platform}] Hata: {e}")
        return None
