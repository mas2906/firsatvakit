#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# VPS Scraper için minimal utils - RateLimiter EKLENDİ

import asyncio
import random
import time as _time
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

log = logging.getLogger("utils_v2")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

PLAYWRIGHT_SEM = asyncio.Semaphore(2)

CLOUDFLARE_TITLES = ["Attention Required", "Just a moment", "Checking your browser"]

class RateLimiter:
    def __init__(self, min_delay: float, max_delay: float):
        self._min = min_delay
        self._max = max_delay
        self._last_ts = 0.0
        self._sem = asyncio.Semaphore(1)

    async def wait(self):
        async with self._sem:
            elapsed = _time.monotonic() - self._last_ts
            delay = random.uniform(self._min, self._max) - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_ts = _time.monotonic()

def parse_price_tr_clean(text: str) -> Optional[float]:
    """
    Türkçe ve standart fiyat formatlarını doğru parse eder.
    Yuvarlama YAPILMAZ — fiyat ne ise tam olarak döner.

      '49.999 TL'   → 49999.0   (binlik nokta)
      '1.299,99 TL' → 1299.99   (binlik nokta + ondalık virgül)
      '1.799,28 TL' → 1799.28
      '399,80 TL'   → 399.8     (Python float — matematiksel olarak 399.80 ile aynı)
      '49999.00'    → 49999.0   (ondalık nokta)
    """
    t = re.sub(r"[^\d,.]", "", (text or "").strip())
    if not t:
        return None

    if "," in t and "." in t:
        if t.rfind(".") > t.rfind(","):
            t = t.replace(",", "")           # ABD: 1,299.99
        else:
            t = t.replace(".", "").replace(",", ".")  # TR: 1.299,99
    elif "," in t:
        parts = t.split(",")
        if len(parts[-1]) <= 2:
            t = t.replace(",", ".")          # ondalık virgül: 399,80
        else:
            t = t.replace(",", "")           # binlik virgül: 1,299
    elif "." in t:
        parts = t.split(".")
        if len(parts[-1]) > 2:
            t = t.replace(".", "")           # binlik nokta: 49.999

    try:
        v = float(t)
        # 10 TL altı veya 999.999 TL üstü → muhtemelen yanlış parse
        return v if 10 < v < 1_000_000 else None
    except Exception:
        return None


def normalize_image_url(url: str | None) -> str | None:
    if not url: return None
    url = url.strip()
    if url.startswith('//'): return 'https:' + url
    if url.startswith('http'): return url
    return None

def detect_cart_discount(html: str) -> bool:
    patterns = ['sepette indirim', 'sepete indirim', '2. ürüne', 'ikinci ürüne']
    return any(p in (html or '').lower() for p in patterns)

