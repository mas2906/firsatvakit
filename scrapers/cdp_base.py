#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDP tabanlı scraper altyapısı — BrowserPool, resource blocking, DOM extract, XHR intercept.
"""

import asyncio
import logging
import random
from typing import Optional

from scrapers.utils import UA_POOL, STEALTH_SCRIPT, get_stealth_headers

log = logging.getLogger("cdp_base")

# Bloklanacak kaynak tipleri
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "texttrack", "eventsource", "websocket"}

# Bloklanacak URL kalıpları
BLOCKED_URL_PATTERNS = [
    "google-analytics", "googletagmanager", "facebook.net", "facebook.com/tr",
    "doubleclick", "adsystem", "criteo", "hotjar", "clarity.ms",
    "adservice", "tracker", "beacon", "analytics", "pixel",
    ".woff", ".woff2", ".ttf", ".otf",
]


async def setup_resource_blocking(page) -> None:
    """Sayfa için gereksiz kaynakları (img, css, font, reklam) engelle."""
    async def _handle_route(route):
        req = route.request
        if req.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        url_lower = req.url.lower()
        for pattern in BLOCKED_URL_PATTERNS:
            if pattern in url_lower:
                await route.abort()
                return
        await route.continue_()

    await page.route("**/*", _handle_route)


# Amazon için hafif blocking — CSS/JS/image açık, sadece reklam/tracker engel
AMAZON_BLOCKED_TYPES = {"media", "websocket", "texttrack", "eventsource"}
AMAZON_BLOCKED_PATTERNS = [
    "google-analytics", "googletagmanager", "doubleclick",
    "adsystem", "criteo", "hotjar", "clarity.ms",
]

async def setup_resource_blocking_amazon(page) -> None:
    """Amazon için hafif resource blocking — CSS ve JS'e dokunma."""
    async def _handle_route(route):
        req = route.request
        if req.resource_type in AMAZON_BLOCKED_TYPES:
            await route.abort()
            return
        url_lower = req.url.lower()
        for pattern in AMAZON_BLOCKED_PATTERNS:
            if pattern in url_lower:
                await route.abort()
                return
        await route.continue_()

    await page.route("**/*", _handle_route)


async def cdp_dom_extract(page, selectors: dict) -> dict:
    """Runtime.evaluate ile CSS selector'lardan veri çek — BeautifulSoup'tan çok daha hızlı."""
    js = """
    (selectors) => {
        const result = {};
        for (const [key, selectorStr] of Object.entries(selectors)) {
            const sels = selectorStr.split(',').map(s => s.trim());
            let value = null;
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) {
                    if (key === 'image') {
                        value = el.src || el.getAttribute('data-src') || el.getAttribute('data-lazy-src') || '';
                    } else {
                        value = (el.textContent || el.innerText || '').trim();
                    }
                    if (value) break;
                }
            }
            result[key] = value;
        }
        return result;
    }
    """
    try:
        return await page.evaluate(js, selectors)
    except Exception as e:
        log.debug(f"[cdp_dom_extract] Hata: {e}")
        return {}


async def cdp_intercept_xhr(page, url: str, patterns: list, timeout: int = 15) -> dict:
    """
    Sayfayı aç, XHR/Fetch yanıtlarını dinle, pattern eşleşen JSON'ı yakala.
    Resource blocking aktif — img/css/font yüklenmez.
    """
    captured = {}
    event = asyncio.Event()

    async def on_response(response):
        resp_url = response.url
        for pattern in patterns:
            if pattern in resp_url:
                try:
                    body = await response.json()
                    captured[pattern] = body
                    event.set()
                except Exception:
                    pass

    page.on("response", on_response)
    await setup_resource_blocking(page)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        try:
            await asyncio.wait_for(event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    except Exception as e:
        log.debug(f"[cdp_intercept_xhr] goto hatası: {e}")
    finally:
        page.remove_listener("response", on_response)

    return captured


class BrowserPool:
    """
    Platform başına izole BrowserPool — ayrı Chrome process, ayrı context.
    Her 100 sayfada context yeniden başlatılır; diğer platformlar etkilenmez.
    """

    RESTART_AFTER = 100

    def __init__(self, max_pages: int = 8, name: str = "pool",
                 shared_browser=None, shared_pw=None, use_proxy: bool = False):
        self.max_pages = max_pages
        self.name = name
        self._pw = shared_pw
        self._browser = shared_browser
        self._shared = shared_browser is not None
        self._context = None
        self._sem = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._started = False
        self._page_count = 0
        self._restart_lock = asyncio.Lock()
        self._use_proxy = use_proxy
        self._current_proxy: str | None = None

    async def _get_proxy_dict(self):
        if not self._use_proxy:
            return None
        try:
            from scrapers.proxy_pool import get_proxy_pool
            pool = get_proxy_pool()
            if not pool.has_proxies:
                return None
            self._current_proxy = await pool.get()
            return pool.playwright_dict(self._current_proxy)
        except Exception:
            return None

    async def _new_context(self):
        _w = random.choice([1280, 1366, 1440, 1920])
        _h = random.choice([768, 800, 900, 1080])
        _ua = random.choice(UA_POOL)
        proxy_dict = await self._get_proxy_dict()
        kwargs: dict = dict(
            viewport={"width": _w, "height": _h},
            user_agent=_ua,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers=get_stealth_headers(_ua),
        )
        if proxy_dict:
            kwargs["proxy"] = proxy_dict
            log.info(f"[BrowserPool/{self.name}] Proxy aktif: {proxy_dict['server']}")
        ctx = await self._browser.new_context(**kwargs)
        await ctx.add_init_script(STEALTH_SCRIPT)
        return ctx

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if not self._shared:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--disable-translate",
                        "--no-first-run",
                        "--mute-audio",
                        "--disable-background-timer-throttling",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            self._context = await self._new_context()
            self._started = True
            log.info(f"[BrowserPool/{self.name}] Başladı — max_pages={self.max_pages}")

    async def _restart(self) -> None:
        """Rolling restart — önce yeni context aç, eski context arka planda kapat.
        Slot drain yok → sıfır duruş, devam eden sayfalar etkilenmez."""
        async with self._restart_lock:
            if self._page_count < self.RESTART_AFTER:
                return
            self._page_count = 0
            old_context = self._context
            try:
                self._context = await self._new_context()
                log.info(f"[BrowserPool/{self.name}] Rolling restart — yeni context hazır, eski arka planda kapanıyor")
            except Exception as e:
                log.error(f"[BrowserPool/{self.name}] Yeni context açılamadı: {e}")
                self._context = old_context  # geri al
                return

            async def _close_old(ctx):
                # 0-12 saniye arası random grace: uçuştaki sayfalar bitsin, sonra kapat
                await asyncio.sleep(random.uniform(0, 12))
                try:
                    await ctx.close()
                    log.info(f"[BrowserPool/{self.name}] Eski context kapatıldı")
                except Exception:
                    pass

            if old_context:
                asyncio.create_task(_close_old(old_context))

    async def acquire(self):
        """Havuzdan bir sayfa al (semaphore ile sınırlı)."""
        if not self._started:
            await self.start()
        if self._page_count >= self.RESTART_AFTER:
            await self._restart()
        await self._sem.acquire()
        try:
            page = await self._context.new_page()
            self._page_count += 1
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
            except Exception:
                pass
            return page
        except Exception:
            self._sem.release()
            raise

    async def release(self, page) -> None:
        """Sayfayı kapat ve semaphore'u serbest bırak."""
        try:
            await page.close()
        except Exception:
            pass
        self._sem.release()

    async def stop(self) -> None:
        async with self._lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if not self._shared:
                if self._browser:
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                    self._browser = None
                if self._pw:
                    try:
                        await self._pw.stop()
                    except Exception:
                        pass
                    self._pw = None
            self._started = False
            log.info(f"[BrowserPool/{self.name}] Durduruldu")
            log.info("[BrowserPool] Durduruldu")


async def human_mouse_move(page) -> None:
    """İnsan benzeri bezier eğrisi mouse hareketi — 2-3 durak."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        w, h = vp["width"], vp["height"]
        points = [
            (random.randint(80, w // 3), random.randint(80, h // 3)),
            (random.randint(w // 3, 2 * w // 3), random.randint(h // 4, 3 * h // 4)),
            (random.randint(2 * w // 3, w - 80), random.randint(h // 3, h - 100)),
        ]
        for x, y in points:
            await page.mouse.move(x, y, steps=random.randint(8, 20))
            await asyncio.sleep(random.uniform(0.1, 0.4))
    except Exception:
        pass


async def human_scroll(page, down_min: int = 200, down_max: int = 600) -> None:
    """Aşağı scroll + küçük geri scroll — insan okuma davranışı."""
    try:
        await page.mouse.wheel(0, random.randint(down_min, down_max))
        await asyncio.sleep(random.uniform(0.4, 1.0))
        if random.random() > 0.5:
            await page.mouse.wheel(0, random.randint(down_min // 2, down_max // 2))
            await asyncio.sleep(random.uniform(0.3, 0.7))
        if random.random() > 0.6:
            await page.mouse.wheel(0, -random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass
