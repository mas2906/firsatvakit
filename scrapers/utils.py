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


class RateLimiter:
    """Asyncio tabanlı adaptif rate limiter — platform başına sıralı bekleme.

    Ürünler günde bir kez tarandığı için (bkz. scheduler.py'deki tur bazlı
    kuyruk), gerçek darboğaz asla hız değil — burada bilinçli olarak geniş ve
    güvenli bir aralık kullanılır, tüm platform ayrımı/rotasyon kaldırıldı.

    min_delay/max_delay taban (en hızlı güvenli) aralıktır. record_failure()
    her blok/hata sinyalinde aralığı üstel olarak genişletir (taban hızın en
    fazla _MAX_MULTIPLIER katına kadar); record_success() ardışık
    _RECOVERY_STREAK başarıdan sonra aralığı kademeli olarak tabana geri
    daraltır. Böylece platform bir blok dalgasından sonra otomatik yavaşlar,
    sular durulunca elle müdahale gerekmeden tekrar hızlanır.
    """

    _BACKOFF_FACTOR   = 1.6
    _RECOVERY_FACTOR  = 0.85
    _RECOVERY_STREAK  = 5
    _MAX_MULTIPLIER   = 4.0

    def __init__(self, min_delay: float, max_delay: float):
        self._base_min = min_delay
        self._base_max = max_delay
        self._min = min_delay
        self._max = max_delay
        self._last_ts = 0.0
        self._sem = asyncio.Semaphore(1)
        self._ok_streak = 0

    def record_failure(self) -> None:
        """Blok/hata sinyali: aralığı genişlet (yavaşla)."""
        self._ok_streak = 0
        cap_min = self._base_min * self._MAX_MULTIPLIER
        cap_max = self._base_max * self._MAX_MULTIPLIER
        new_min = min(cap_min, self._min * self._BACKOFF_FACTOR)
        new_max = min(cap_max, self._max * self._BACKOFF_FACTOR)
        if new_min > self._min or new_max > self._max:
            log.warning(f"[rate-limit] blok sinyali → yavaşlıyor ({self._min:.1f}-{self._max:.1f}s → {new_min:.1f}-{new_max:.1f}s)")
        self._min, self._max = new_min, new_max

    def record_success(self) -> None:
        """Başarı sinyali: _RECOVERY_STREAK ardışık başarıdan sonra bir adım hızlan."""
        self._ok_streak += 1
        if self._ok_streak < self._RECOVERY_STREAK:
            return
        self._ok_streak = 0
        if self._min <= self._base_min and self._max <= self._base_max:
            return
        new_min = max(self._base_min, self._min * self._RECOVERY_FACTOR)
        new_max = max(self._base_max, self._max * self._RECOVERY_FACTOR)
        log.info(f"[rate-limit] {self._RECOVERY_STREAK} ardışık başarı → hızlanıyor ({self._min:.1f}-{self._max:.1f}s → {new_min:.1f}-{new_max:.1f}s)")
        self._min, self._max = new_min, new_max

    async def wait(self) -> None:
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
