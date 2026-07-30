#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scraperlar için ortak yardımcı fonksiyonlar."""

import re
import random
import asyncio
import logging
import time as _time
from datetime import datetime
from typing import Optional

log = logging.getLogger("scraper")

CLOUDFLARE_TITLES = ["Attention Required", "Just a moment", "Checking your browser"]
CAMOUFOX_STEALTH = ""  # geriye dönük uyumluluk sabiti


def normalize_image_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return None


def parse_price_tr_clean(text: Optional[str]) -> Optional[float]:
    """
    Türkçe fiyat string'ini float'a çevirir.
    Desteklenen formatlar: '1.299,99 TL', '1299.99', '1.299', '1,299.99'
    """
    if not text:
        return None
    t = re.sub(r"[^\d.,]", "", str(text).strip())
    if not t:
        return None

    # Nokta binlik ayraç, virgül ondalık (TR): "1.299,99"
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", t):
        t = t.replace(".", "").replace(",", ".")
    # Virgül binlik ayraç, nokta ondalık (US): "1,299.99"
    elif re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", t):
        t = t.replace(",", "")
    # Sadece virgül → ondalık: "1299,99"
    elif "," in t and "." not in t:
        t = t.replace(",", ".")
    # Sadece nokta: "1299.99" veya "1299" → bırak

    try:
        v = float(t)
        return v if v > 0 else None
    except ValueError:
        return None


CART_DISCOUNT_PATTERNS = [
    "sepette indirim",
    "sepete indirim",
    "sepette ekstra",
    "sepete ekstra",
    "2. ürüne",
    "ikinci ürüne",
    "çoklu alım",
    "kombinasyon fiyat",
    "cart discount",
]


def detect_cart_discount(html_text: str) -> bool:
    """HTML metninde sepette indirim göstergesi arar."""
    if not html_text:
        return False
    lower = html_text.lower()
    return any(p in lower for p in CART_DISCOUNT_PATTERNS)


# ── Platform rotasyonu ─────────────────────────────────────────────────────────
# Ban/blok riskini azaltmak için 4 platform aynı anda değil, sırayla taranır.
# 20 dk'lık döngüde her platform 5 dk aktif, 15 dk pasif olur. Tek kaynak burası —
# hem local_scraper.py (iş kuyruğa dispatch edilsin mi) hem RateLimiter (siteye
# gerçekten istek gitsin mi) hem de VPS'teki admin monitor (/api/scraper-monitor)
# aynı hesaplamayı kullanır. UTC kullanıyoruz ki local makine ile VPS'in saat
# dilimi farkı rotasyon durumunu birbirinden saptırmasın.
ROTATION_ORDER    = ["trendyol", "n11", "amazon", "hepsiburada"]
ROTATION_SLOT_MIN = 5


def _rotation_now() -> datetime:
    return datetime.utcnow()


def rotation_active_platform() -> str:
    cycle_min = ROTATION_SLOT_MIN * len(ROTATION_ORDER)
    now = _rotation_now()
    slot = (now.minute % cycle_min) // ROTATION_SLOT_MIN
    return ROTATION_ORDER[slot]


def rotation_minutes_left() -> float:
    """Aktif platformun sırasının bitmesine kalan dakika."""
    cycle_min = ROTATION_SLOT_MIN * len(ROTATION_ORDER)
    now = _rotation_now()
    pos = (now.minute % cycle_min) + now.second / 60.0
    into_slot = pos % ROTATION_SLOT_MIN
    return round(ROTATION_SLOT_MIN - into_slot, 1)


def is_rotation_active(platform: str) -> bool:
    return rotation_active_platform() == platform


class RateLimiter:
    """Asyncio tabanlı rate limiter — platform başına sıralı bekleme.

    `platform` verilirse, rotasyon sırası bu platformda değilken normal
    (priority=False) istekleri bekletir — siteye gerçekten istek gitmeden önce
    rotasyonu burada da uygular (sadece dispatch katmanına güvenmez).
    `priority=True` (örn. yeni eklenen link) rotasyonu atlar, hep hemen geçer.
    """

    def __init__(self, min_delay: float, max_delay: float, platform: Optional[str] = None):
        self._min = min_delay
        self._max = max_delay
        self._last_ts = 0.0
        self._sem = asyncio.Semaphore(1)
        self._platform = platform

    async def wait(self, priority: bool = False) -> None:
        if self._platform and not priority:
            first = True
            while not is_rotation_active(self._platform):
                if first:
                    log.info(f"[rate-limiter/{self._platform}] rotasyon sırası değil — bekleniyor")
                    first = False
                await asyncio.sleep(5)

        async with self._sem:
            elapsed = _time.monotonic() - self._last_ts
            delay = random.uniform(self._min, self._max)
            # Gece saatlerinde (00-07) gerçek bir insan çok daha az alışveriş
            # yapar — temel gecikmeyi yavaşlat.
            if 0 <= datetime.now().hour < 7:
                delay *= random.uniform(1.5, 2.0)
            # İnsan davranışı düz uniform dağılım değildir — çoğu bekleme kısa,
            # ama arada (dikkat dağılması/okuma gibi) daha uzun duraklamalar olur.
            # %12 ihtimalle bu uzun-kuyruklu duraklamayı ekle.
            if random.random() < 0.12:
                delay += random.uniform(self._max, self._max * 3)
            delay -= elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_ts = _time.monotonic()

    def reset(self) -> None:
        self._last_ts = 0.0
