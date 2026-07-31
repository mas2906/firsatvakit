#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Platform yönlendirici — her platform kendi scraper modülünden import edilir."""

from typing import Optional

from scrapers.amazon      import scrape_amazon
from scrapers.trendyol    import scrape_trendyol
from scrapers.n11         import scrape_n11
from scrapers.hepsiburada import scrape_hepsiburada

_SCRAPERS = {
    "trendyol":    scrape_trendyol,
    "n11":         scrape_n11,
    "amazon":      scrape_amazon,
    "hepsiburada": scrape_hepsiburada,
}


async def scrape_product(
    url: str,
    platform: str,
    pool=None,
    price_only: bool = False,
    cached_image: Optional[str] = None,
) -> Optional[dict]:
    fn = _SCRAPERS.get(platform)
    if not fn:
        print(f"[router] Desteklenmeyen platform: {platform}")
        return None
    try:
        return await fn(url, price_only=price_only, cached_image=cached_image)
    except Exception as e:
        print(f"[scraper/{platform}] Hata: {e}")
        return None
