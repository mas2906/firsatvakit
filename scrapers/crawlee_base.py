#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crawlee scraping altyapısı — tüm platform scraperlarının kullandığı ortak katman.

Sağladığı özellikler:
  - curl_cffi AsyncSession havuzu (platform başına)
  - Platform başına RateLimiter
  - Circuit breaker (art arda hata → geçici askı)
  - Playwright fallback (_crawlee_pw_scrape)
  - price_filter yardımcısı
"""

import asyncio
import logging
import random
import time as _time
from typing import Optional

from scrapers.utils import RateLimiter

log = logging.getLogger("scraper")

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _CURL_OK = True
except ImportError:
    CurlSession = None
    _CURL_OK = False

# ── User-agent havuzları ───────────────────────────────────────────────────────
IOS_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
]

ANDROID_UAS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

# Chrome desktop profilleri — Amazon için (farklı TLS fingerprint)
CHROME_PROFILES = [
    {
        "impersonate": "chrome131",
        "ua":          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua":   '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform":    '"Windows"',
        "mobile":      "?0",
    },
    {
        "impersonate": "chrome120",
        "ua":          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "sec_ch_ua":   '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="8"',
        "platform":    '"macOS"',
        "mobile":      "?0",
    },
    {
        "impersonate": "chrome124",
        "ua":          "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec_ch_ua":   '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="99"',
        "platform":    '"Linux"',
        "mobile":      "?0",
    },
]


def make_chrome_headers(profile: dict, referer: Optional[str] = None) -> dict:
    """Verilen Chrome profili için gerçek tarayıcıya yakın header seti."""
    h = {
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding":           "gzip, deflate, br, zstd",
        "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control":             "max-age=0",
        "Sec-Ch-Ua":                 profile["sec_ch_ua"],
        "Sec-Ch-Ua-Mobile":          profile["mobile"],
        "Sec-Ch-Ua-Platform":        profile["platform"],
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "same-origin" if referer else "none",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent":                profile["ua"],
    }
    if referer:
        h["Referer"] = referer
    return h


# ── curl_cffi session havuzu (platform başına) ────────────────────────────────
_SESSIONS: dict[str, list] = {p: [] for p in ("trendyol", "n11", "amazon", "hepsiburada")}
_SESSION_LOCK: dict[str, Optional[asyncio.Lock]] = {p: None for p in _SESSIONS}
_SESSION_IDX: dict[str, int] = {p: 0 for p in _SESSIONS}
_POOL_SIZE = 3


def _lock(platform: str) -> asyncio.Lock:
    if _SESSION_LOCK[platform] is None:
        _SESSION_LOCK[platform] = asyncio.Lock()
    return _SESSION_LOCK[platform]


async def get_session(platform: str, impersonate: str = "safari18_0") -> "CurlSession":
    async with _lock(platform):
        pool = _SESSIONS[platform]
        if len(pool) < _POOL_SIZE:
            s = CurlSession(impersonate=impersonate, timeout=20)
            pool.append(s)
            log.debug(f"[{platform}] Yeni curl session #{len(pool)}")
        _SESSION_IDX[platform] = (_SESSION_IDX[platform] + 1) % len(pool)
        return pool[_SESSION_IDX[platform]]


def drop_session(platform: str) -> None:
    pool = _SESSIONS[platform]
    if not pool:
        return
    idx = _SESSION_IDX[platform] % len(pool)
    try:
        pool.pop(idx)
    except Exception:
        pass


# ── Rate limiter'lar (platform başına) ───────────────────────────────────────
RL: dict[str, RateLimiter] = {
    "trendyol":    RateLimiter(0.5, 1.0),
    "n11":         RateLimiter(0.4, 0.8),
    "amazon":      RateLimiter(2.0, 4.0),
    "hepsiburada": RateLimiter(1.5, 3.0),
}

# ── Circuit breaker ───────────────────────────────────────────────────────────
_STREAK: dict[str, int] = {}
_DISABLED: dict[str, float] = {}
_STREAK_LIMIT = 4
_COOLDOWN_S = 120


def cb_ok(key: str) -> bool:
    return _time.time() > _DISABLED.get(key, 0.0)


def cb_fail(key: str) -> None:
    _STREAK[key] = _STREAK.get(key, 0) + 1
    if _STREAK[key] >= _STREAK_LIMIT:
        _DISABLED[key] = _time.time() + _COOLDOWN_S
        _STREAK[key] = 0
        log.warning(f"[{key}] {_STREAK_LIMIT} ardışık hata → {_COOLDOWN_S}s askı")


def cb_reset(key: str) -> None:
    _STREAK[key] = 0


# ── price_filter yardımcısı ───────────────────────────────────────────────────
def price_filter(data: Optional[dict], price_only: bool, cached_image: Optional[str]) -> Optional[dict]:
    if not data or not price_only:
        return data
    if data.get("dead_url") or data.get("error"):
        return data
    out = {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}
    if cached_image:
        out["image_url"] = cached_image
    return out


# ── Playwright kaynak engelleme ───────────────────────────────────────────────
_BLOCK_TYPES = {"image", "stylesheet", "font", "media", "texttrack", "eventsource"}
_BLOCK_PATTERNS = [
    "google-analytics", "googletagmanager", "facebook.net", "doubleclick",
    "adsystem", "criteo", "hotjar", "clarity.ms", "adservice", "analytics",
]


async def _block_route(route) -> None:
    req = route.request
    if req.resource_type in _BLOCK_TYPES:
        await route.abort()
        return
    url_l = req.url.lower()
    for p in _BLOCK_PATTERNS:
        if p in url_l:
            await route.abort()
            return
    await route.continue_()


# ── crawlee PlaywrightCrawler (tek URL, browserforge fingerprint) ─────────────
async def crawlee_pw_scrape(
    url: str,
    platform: str,
    page_handler,
    timeout: float = 55.0,
) -> Optional[dict]:
    """
    Tek URL için crawlee PlaywrightCrawler çalıştırır.
    browserforge fingerprint otomatik enjekte edilir.
    """
    try:
        from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
        from crawlee import ConcurrencySettings
    except ImportError:
        log.warning("[crawlee] PlaywrightCrawler import başarısız")
        return None

    result: dict = {}

    # min_concurrency=1: CPU yükü yüksek olsa bile autoscaler concurrency'yi
    # 0'a indirmesin — tek URL için en az 1 işlem garantilenir
    crawler = PlaywrightCrawler(
        headless=True,
        max_requests_per_crawl=1,
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            max_concurrency=1,
            desired_concurrency=1,
        ),
    )

    @crawler.router.default_handler
    async def _handler(ctx: PlaywrightCrawlingContext):
        page = ctx.page
        await page.route("**/*", _block_route)
        data = await page_handler(page, ctx.request.url)
        if data:
            result.update(data)

    try:
        await asyncio.wait_for(crawler.run([url]), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(f"[crawlee/{platform}] PW timeout: {url[:60]}")
    except Exception as e:
        log.debug(f"[crawlee/{platform}] PW hata: {e}")

    return result if result else None
