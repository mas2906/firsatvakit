#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser cookie hasadı — Camoufox (Firefox) veya Playwright stealth.

Akış:
  1. Platform anasayfasına browser ile git
  2. Anti-bot JS çalışsın ve cookie'leri oluştursun (wait_ms)
  3. Cookie'leri TTL süresi boyunca önbellekle
  4. curl_cffi session'larına enjekte et
  5. 429/403 gelince force_refresh() ile yenile

Desteklenen platformlar: hepsiburada, amazon, trendyol, n11
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("browser_cookies")

_PLATFORM_CONFIG: dict[str, dict] = {
    "hepsiburada": {
        "url":     "https://www.hepsiburada.com/",
        "ttl":     300,     # Akamai _abck 5dk'da bir güncelleniyor
        "wait_ms": 4000,    # Akamai sensor JS için bekle
    },
    "amazon": {
        "url":     "https://www.amazon.com.tr/",
        "ttl":     600,
        "wait_ms": 3000,
    },
    "trendyol": {
        "url":     "https://www.trendyol.com/",
        "ttl":     1500,    # CF clearance ~30dk geçerli
        "wait_ms": 5000,    # Cloudflare JS challenge için bekle
    },
    "n11": {
        "url":     "https://www.n11.com/",
        "ttl":     1500,
        "wait_ms": 5000,
    },
}


# ── Camoufox (Firefox anti-detection) ────────────────────────────────────────

async def _harvest_camoufox(url: str, proxy_url: Optional[str], wait_ms: int) -> dict:
    from camoufox.async_api import AsyncCamoufox
    kwargs: dict = {"headless": True, "geoip": True}
    if proxy_url:
        from scrapers.proxy_pool import ProxyPool
        pd = ProxyPool.playwright_dict(proxy_url)
        if pd:
            kwargs["proxy"] = pd
    async with AsyncCamoufox(**kwargs) as browser:
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(wait_ms)
            cookies = await page.context.cookies()
            return {c["name"]: c["value"] for c in cookies}
        finally:
            await page.close()


# ── Playwright stealth fallback ───────────────────────────────────────────────

async def _harvest_playwright(url: str, proxy_url: Optional[str], wait_ms: int) -> dict:
    from playwright.async_api import async_playwright
    from scrapers.utils import STEALTH_SCRIPT

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        ctx_kwargs: dict = {
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
            "ignore_https_errors": True,
        }
        if proxy_url:
            from scrapers.proxy_pool import ProxyPool
            pd = ProxyPool.playwright_dict(proxy_url)
            if pd:
                ctx_kwargs["proxy"] = pd
        ctx = await browser.new_context(**ctx_kwargs)
        await ctx.add_init_script(STEALTH_SCRIPT)
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(wait_ms)
            cookies = await ctx.cookies()
            return {c["name"]: c["value"] for c in cookies}
        finally:
            try:
                await page.close()
                await ctx.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# ── Harvester ─────────────────────────────────────────────────────────────────

class BrowserCookieHarvester:
    """
    Platform başına browser cookie havuzu.
    Cookie'ler TTL süresi boyunca önbelleklenir.
    Camoufox yüklü değilse Playwright stealth devreye girer.
    """

    def __init__(self):
        self._cache: dict[str, dict[str, str]] = {}
        self._expires: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {
            p: asyncio.Lock() for p in _PLATFORM_CONFIG
        }

    def _fresh(self, platform: str) -> bool:
        return (platform in self._cache and
                time.monotonic() < self._expires.get(platform, 0))

    async def get(self, platform: str) -> dict[str, str]:
        """Cache geçerliyse döndür; değilse browser ile tazele."""
        if self._fresh(platform):
            return dict(self._cache[platform])
        return await self._do_refresh(platform)

    async def force_refresh(self, platform: str) -> dict[str, str]:
        """429/403 sonrası zorla yenile; lock ile tek eşzamanlı yenileme."""
        self._expires[platform] = 0
        return await self._do_refresh(platform)

    async def _do_refresh(self, platform: str) -> dict[str, str]:
        async with self._locks[platform]:
            if self._fresh(platform):
                return dict(self._cache[platform])

            cfg = _PLATFORM_CONFIG[platform]
            proxy_url = await _pick_proxy()
            cookies = await self._harvest(cfg["url"], proxy_url, cfg["wait_ms"])

            if cookies:
                self._cache[platform] = cookies
                self._expires[platform] = time.monotonic() + cfg["ttl"]
                log.info(
                    f"[browser_cookies] ✔ {platform} — {len(cookies)} cookie, "
                    f"TTL={cfg['ttl']}s"
                )
            else:
                log.warning(f"[browser_cookies] ✘ {platform} — cookie alınamadı")

            return dict(cookies)

    async def _harvest(self, url: str, proxy_url: Optional[str], wait_ms: int) -> dict:
        # Camoufox önce
        try:
            cookies = await _harvest_camoufox(url, proxy_url, wait_ms)
            if cookies:
                log.debug(f"[browser_cookies] camoufox başarılı — {url}")
                return cookies
        except ImportError:
            log.debug("[browser_cookies] camoufox kurulu değil — Playwright deneniyor")
        except Exception as e:
            log.warning(f"[browser_cookies] camoufox hatası: {e}")

        # Playwright fallback
        try:
            cookies = await _harvest_playwright(url, proxy_url, wait_ms)
            if cookies:
                log.debug(f"[browser_cookies] Playwright başarılı — {url}")
                return cookies
        except Exception as e:
            log.warning(f"[browser_cookies] Playwright hatası: {e}")

        return {}


# ── Yardımcılar ───────────────────────────────────────────────────────────────

async def _pick_proxy() -> Optional[str]:
    try:
        from scrapers.proxy_pool import get_proxy_pool
        pp = get_proxy_pool()
        if pp.has_proxies:
            return await pp.get()
    except Exception:
        pass
    return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_harvester: Optional[BrowserCookieHarvester] = None


def get_browser_harvester() -> BrowserCookieHarvester:
    global _harvester
    if _harvester is None:
        _harvester = BrowserCookieHarvester()
    return _harvester
