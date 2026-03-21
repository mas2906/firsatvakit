#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tüm scraperlar için ortak yardımcı fonksiyonlar."""

import re
from typing import Optional

CLOUDFLARE_TITLES = ["Attention Required", "Just a moment", "Checking your browser"]

STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR', 'tr']});
    window.chrome = {runtime: {}};
"""

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

DEFAULT_HEADERS = {
    "User-Agent": UA_POOL[0],
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def normalize_image_url(url: str | None) -> str | None:
    """Protokol-relative veya eksik URL'leri düzelt."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return None


def parse_price_tr(text: str) -> Optional[float]:
    """'1.299,99 TL' → 1299.99"""
    t = re.sub(r"[^\d,.]", "", (text or "").strip())
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        v = float(t)
        return v if v > 0 else None
    except Exception:
        return None


def parse_price_tr_clean(text: str) -> Optional[float]:
    """
    Türkçe ve standart fiyat formatlarını doğru parse eder:
      '49.999 TL'   → 49999.0  (binlik nokta)
      '1.299,99 TL' → 1299.99  (binlik nokta + ondalık virgül)
      '49999.00'    → 49999.0  (ondalık nokta)
      '1299.99'     → 1299.99  (ondalık nokta)
    """
    t = re.sub(r"[^\d,.]", "", (text or "").strip())
    if not t:
        return None

    if "," in t and "." in t:
        # Her ikisi varsa: Türk formatı (1.299,99) mı ABD formatı (1,299.99) mı?
        if t.rfind(".") > t.rfind(","):
            # Son ayraç nokta → ABD formatı: 1,299.99
            t = t.replace(",", "")
        else:
            # Son ayraç virgül → TR formatı: 1.299,99
            t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        parts = t.split(",")
        if len(parts[-1]) <= 2:
            t = t.replace(",", ".")   # ondalık virgül: 1299,99 → 1299.99
        else:
            t = t.replace(",", "")   # binlik virgül: 1,299 → 1299
    elif "." in t:
        parts = t.split(".")
        if len(parts[-1]) <= 2:
            pass  # ondalık nokta: 49999.00 → değiştirme
        else:
            t = t.replace(".", "")   # binlik nokta: 49.999 → 49999

    try:
        v = float(t)
        return v if v > 10 else None
    except Exception:
        return None
