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
import os
import random
import re
import time as _time
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit

from scrapers.utils import RateLimiter

# crawlee varsayılan olarak RAM'in %25'ini (24GB makinede ~6GB) tavan sayıyor ve
# "Memory is critically overloaded" deyip kendi autoscaler'ını kısıyordu.
os.environ.setdefault("CRAWLEE_MEMORY_MBYTES", "12288")

log = logging.getLogger("scraper")

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _CURL_OK = True
except ImportError:
    CurlSession = None
    _CURL_OK = False

# Camoufox: Playwright fallback'ın tarayıcı motoru. C++ seviyesinde fingerprint
# enjeksiyonu yapan patched Firefox — vanilla Chromium+browserforge'a göre
# Cloudflare/bot tespiti karşısında çok daha zor ayırt edilir. Kurulu değilse
# (ör. henüz `camoufox fetch` çalıştırılmamış bir ortam) sessizce eski
# PlaywrightCrawler davranışına düşülür.
try:
    from camoufox import AsyncNewBrowser as _camoufox_new_browser
    from crawlee.browsers import PlaywrightBrowserPlugin as _PWPlugin, PlaywrightBrowserController as _PWController
    from crawlee._utils.context import ensure_context as _ensure_context
    _CAMOUFOX_OK = True
except ImportError:
    _CAMOUFOX_OK = False

# Crawlee disk storage birikimini önle:
# 1) global service_locator → MemoryStorageClient
# 2) global StorageInstanceManager sıfırla (disk-tabanlı cached queue'ları temizler)
try:
    import os as _os
    from crawlee._service_locator import ServiceLocator as _CrawleeSL, service_locator as _crawlee_sl
    from crawlee.storage_clients import MemoryStorageClient as _MSC
    from crawlee.configuration import Configuration as _CrawleeConfig
    _crawlee_sl._storage_client = _MSC()
    _tmp_storage = _os.environ.get("CRAWLEE_STORAGE_DIR", r"D:\firsatvakti_temp\crawlee_storage")
    _crawlee_sl._configuration = _CrawleeConfig(storage_dir=_tmp_storage)
    _CrawleeSL.global_storage_instance_manager = None
except Exception:
    pass

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
_SESSIONS: dict[str, list] = {p: [] for p in ("trendyol", "n11", "amazon", "hepsiburada", "karaca")}
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


async def reset_all_sessions(close_delay: float = 90.0) -> int:
    """Tüm platformların curl_cffi oturum havuzunu sıfırlar → birikmiş tüm
    çerezler gider (tarama turu başa döndüğünde çağrılır, bkz. local_scraper).

    Sadece `session.cookies.clear()` yetmez: curl tanıtıcısı kendi çerez
    motorunda da çerez tutabiliyor. Bu yüzden havuz komple yenileniyor. Eski
    oturumlar HEMEN kapatılmaz — o an onlarla istek atan worker'lar olabilir;
    close_delay saniye sonra arka planda kapatılır. Kapatılan oturum sayısını döner."""
    old: list = []
    for platform in _SESSIONS:
        async with _lock(platform):
            old.extend(_SESSIONS[platform])
            _SESSIONS[platform] = []
            _SESSION_IDX[platform] = 0

    async def _close_later(sessions: list) -> None:
        await asyncio.sleep(close_delay)
        for s in sessions:
            try:
                s.cookies.clear()
                await s.close()
            except Exception:
                pass

    if old:
        asyncio.create_task(_close_later(old))
    return len(old)


def drop_session(platform: str) -> None:
    pool = _SESSIONS[platform]
    if not pool:
        return
    idx = _SESSION_IDX[platform] % len(pool)
    try:
        pool.pop(idx)
    except Exception:
        pass


# ── Rate limiter'lar (platform başına) ─────────────────────────────────────────
# Ürünler günde bir kez taranıyor (scheduler.py'deki tur bazlı kuyruk) — bu yüzden
# hız değil güvenlik önceliklidir. Değerler, en büyük kataloğu (hepsiburada) bile
# düşük concurrency ile günde rahatça bitirecek şekilde bilerek geniş tutuldu.
# Trendyol daha önce çok daha düşük değerlerde (2.0-5.5sn, concurrency=6) blok
# yemişti; 8-20sn + concurrency=1 o olaydaki değerin hâlâ belirgin şekilde
# üzerinde, ama eski 15-40sn'ye göre daha hızlı — adaptif backoff (record_failure)
# blok sinyali gelirse aralığı otomatik genişletir.
# 2026-10-16: ~%25 hızlandırıldı (8-20 → 6-15 vb.). Ban testinde captcha/engel
# görülmedi; asıl yavaşlatan şey kaldırılmış HB ürünlerinin kategori sayfasının
# "blok" sayılmasıydı (bkz. redirected_away). Trendyol'un ban yediği 2-5.5sn'nin
# hâlâ belirgin şekilde üzerinde.
# 2026-10-31 (kullanıcı isteği): tüm platformlarda istekler arası en fazla 4sn.
# DİKKAT: Trendyol geçmişte 2-5.5sn + concurrency=6'da ban vermişti; engel
# gelirse record_failure() aralığı yine 4 katına kadar genişletir (güvenlik ağı).
RL: dict[str, RateLimiter] = {
    "trendyol":    RateLimiter(2.0, 4.0),
    "n11":         RateLimiter(2.0, 4.0),
    # Amazon istisna (2026-11-01): 2-4sn'de birkaç saat sonra IP'ye engel ara
    # sayfası gösterip 9 saat bırakmadı. İhtiyaç saatte ~40 ürün; 15-30sn ile
    # saatte ~150'ye kadar çıkılabiliyor (ihtiyacın 3-4 katı).
    # 2026-11-01 akşam: kullanıcı isteğiyle 15-30 → 8-15sn (16 Eki'de 8-20sn'de
    # %92 başarı vardı). Engel gelirse amazon_pw 15dk askıya alınır.
    "amazon":      RateLimiter(8.0, 15.0),
    "hepsiburada": RateLimiter(2.0, 4.0),
    "karaca":      RateLimiter(2.0, 4.0),
}

# ── Circuit breaker ───────────────────────────────────────────────────────────
_STREAK: dict[str, int] = {}
_DISABLED: dict[str, float] = {}
_STREAK_LIMIT = 4
_COOLDOWN_S = 120
# Amazon engel ara sayfası ("Alışverişe devam etmek için tıklayın") gösterince
# 2 dk sonra aynı hızla dönmek engeli saatlerce uzatıyordu (2026-11-01: 03-12
# arası saatte 1-16 başarı). Engel gelince uzun süre geri çekil.
_COOLDOWN_BY_KEY = {"amazon_pw": 15 * 60}


def cb_ok(key: str) -> bool:
    return _time.time() > _DISABLED.get(key, 0.0)


def cb_remaining(key: str) -> float:
    """Devre kesicinin açılmasına kalan saniye (askıda değilse 0)."""
    return max(0.0, _DISABLED.get(key, 0.0) - _time.time())


def cb_fail(key: str) -> None:
    _STREAK[key] = _STREAK.get(key, 0) + 1
    if _STREAK[key] >= _STREAK_LIMIT:
        cooldown = _COOLDOWN_BY_KEY.get(key, _COOLDOWN_S)
        _DISABLED[key] = _time.time() + cooldown
        _STREAK[key] = 0
        log.warning(f"[{key}] {_STREAK_LIMIT} ardışık hata → {cooldown}s askı")


def cb_reset(key: str) -> None:
    _STREAK[key] = 0


# ── Satıştan kaldırılmış ürün tespiti ─────────────────────────────────────────
# HB ve N11 kaldırılan ürünü HTTP 301/302 ile kategori ya da arama sayfasına
# yönlendiriyor. Yalnızca curl'ün SUNUCU tarafı yönlendirme sonrası URL'sine
# bakılır (Playwright'ın istemci tarafı page.url'i değil — o yanlış-pozitif
# üretmişti). Tek sinyalle silinmez: VPS ikinci tekrarda siler.
_HB_CODE_RE = re.compile(r"-pm?-([a-z0-9]{8,})", re.I)


def redirected_away(platform: str, orig_url: str, final_url: str) -> bool:
    if not final_url or final_url.rstrip("/") == orig_url.rstrip("/"):
        return False
    final_l = final_url.lower()
    if platform == "hepsiburada":
        m = _HB_CODE_RE.search(orig_url)
        return bool(m) and "hepsiburada.com" in final_l and m.group(1).lower() not in final_l
    if platform == "n11":
        return "/urun/" in orig_url and "n11.com" in final_l and "/urun/" not in final_l
    return False


# ── price_filter yardımcısı ───────────────────────────────────────────────────
def price_filter(data: Optional[dict], price_only: bool, cached_image: Optional[str]) -> Optional[dict]:
    if not data or not price_only:
        return data
    if data.get("dead_url") or data.get("removed") or data.get("blocked") or data.get("error"):
        return data
    # barcode da taşınır: VPS COALESCE ile yalnızca boşsa yazar. Eskiden fiyat
    # taramaları barkodu atıyordu; ilk taramada alınamayan barkod hiç kaydedilmiyordu.
    out = {k: data[k] for k in ("price", "stock", "cart_discount", "coupon", "barcode") if k in data and data[k] is not None}
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


# ── Proxy (opsiyonel) ──────────────────────────────────────────────────────────
# .env'de LOCAL_PROXY veya PROXY_HOST/PROXY_USER/PROXY_PASS/PROXY_PORT tanımlı
# değilse None döner ve davranış bugünküyle birebir aynı kalır (proxies.txt'nin
# belgelediği şemayla uyumlu).
def _build_proxy_configuration():
    urls = []
    if _os.environ.get("LOCAL_PROXY"):
        urls.append(_os.environ["LOCAL_PROXY"])
    elif _os.environ.get("PROXY_HOST"):
        host = _os.environ["PROXY_HOST"]
        port = _os.environ.get("PROXY_PORT", "")
        user = _os.environ.get("PROXY_USER", "")
        pw   = _os.environ.get("PROXY_PASS", "")
        auth = f"{user}:{pw}@" if user else ""
        hostport = f"{host}:{port}" if port else host
        urls.append(f"http://{auth}{hostport}")
    if not urls:
        return None
    try:
        from crawlee.proxy_configuration import ProxyConfiguration
        return ProxyConfiguration(proxy_urls=urls)
    except Exception:
        log.warning("[crawlee] Proxy yapılandırması başarısız, proxysiz devam")
        return None


_PROXY_CONFIG = _build_proxy_configuration()


# ── İnsan-benzeri sayfa etkileşimi ────────────────────────────────────────────
# Referans: github.com/mas2906/ecom scraper'ının _human_scroll'u — gaussian
# adım büyüklüğü, ara sıra geri kaydırma ve fare hareketi eklendi. Onların
# kategori-listesi taramasındaki "sayfa sonuna in" kısmı burada yok — biz tek
# bir ürün detay sayfasındayız, amaç "okuyormuş gibi" birkaç doğal adım atmak.
async def _human_interact(page) -> None:
    """Veri çekmeden önce kısa bir scroll + okuma duraklaması — sabit,
    makine-gibi bir gezinme yerine gerçek kullanıcı davranışına yaklaşır."""
    try:
        vp_h = await page.evaluate("window.innerHeight") or 900
        steps = random.randint(2, 4)
        for _ in range(steps):
            step = max(100, int(random.gauss(vp_h * 0.5, vp_h * 0.2)))
            await page.mouse.wheel(0, step)
            await asyncio.sleep(random.uniform(0.3, 0.9))

            # %25 ihtimalle "yanlış yere kaydırdım" gibi geri dönüş
            if random.random() < 0.25:
                await page.mouse.wheel(0, -random.randint(80, 250))
                await asyncio.sleep(random.uniform(0.2, 0.5))

            # %35 ihtimalle fare hareketi (fingerprint çeşitliliği)
            if random.random() < 0.35:
                try:
                    vp_w = await page.evaluate("window.innerWidth") or 1280
                    await page.mouse.move(
                        random.randint(80, max(81, vp_w - 80)),
                        random.randint(80, max(81, vp_h - 80)),
                    )
                except Exception:
                    pass

            # %15 ihtimalle "ürüne bakıyormuş gibi" uzun duraklama
            if random.random() < 0.15:
                await asyncio.sleep(random.uniform(1.0, 2.5))

        await asyncio.sleep(random.uniform(0.3, 1.2))
    except Exception:
        pass


def already_on(page, url: str) -> bool:
    """crawlee handler'a sayfayı zaten açmış olarak verir — HTML'den okuyan
    handler'lar aynı URL'yi ikinci kez açmasın (sorgu/fragman yok sayılır)."""
    try:
        cur, want = urlsplit(page.url or ""), urlsplit(url)
        return cur.netloc == want.netloc and cur.path.rstrip("/") == want.path.rstrip("/")
    except Exception:
        return False


async def _warmup_navigation(page, base_url: str) -> None:
    """Asıl hedefe gitmeden önce platformun ana sayfasını ziyaret eder —
    gerçek kullanıcı izlenimi + çerez/oturum ısınması sağlar (referans:
    github.com/mas2906/ecom scraper'ı, hem amazon hem trendyol'da kullanılıyor).
    Başarısız olursa sessizce geçilir — asıl hedefe gidiş buna bağımlı değil."""
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(random.uniform(1.5, 3.0))
        await _human_interact(page)
    except Exception:
        pass


# ── Camoufox tarayıcı eklentisi ────────────────────────────────────────────────
if _CAMOUFOX_OK:
    class _CamoufoxPlugin(_PWPlugin):
        """PlaywrightBrowserPlugin'in Chromium yerine Camoufox (patched Firefox)
        başlatan versiyonu. Windows fingerprint + tr-TR locale: mevcut curl_cffi
        Chrome/Windows profilleriyle (bkz. CHROME_PROFILES) ve Accept-Language
        header'larıyla tutarlı kalması için."""

        @_ensure_context
        async def new_browser(self) -> "_PWController":
            if not self._playwright:
                raise RuntimeError("Playwright başlatılmamış.")
            browser = await _camoufox_new_browser(
                self._playwright,
                headless=True,
                os=["windows"],
                locale="tr-TR",
                humanize=True,
                block_webrtc=True,
            )
            return _PWController(
                browser=browser,
                max_open_pages_per_browser=1,
                header_generator=None,  # Camoufox kendi header/fingerprint'ini üretir
            )


# ── crawlee PlaywrightCrawler (tek URL) ────────────────────────────────────────
# Motor: Camoufox varsa Camoufox (patched Firefox), yoksa vanilla Chromium +
# crawlee'nin browserforge fingerprint enjeksiyonu.
# Tüm platformlar ve kuyruklar (normal+express) için TEK global sınır: aynı anda
# en fazla bu kadar gerçek tarayıcı instance açık olabilir. Platform başına
# concurrent ayarları bu sınırı aşabilir (poll+express çakışması dahil) — asıl
# CPU/RAM tavanını burada garanti ediyoruz.
# 2026-10-31: 4→6 — Trendyol 4 worker + Amazon 2 + HB/N11 fallback'leri 4 slot
# için kuyruğa giriyor, Trendyol işleri ortalama ~165sn sürüyordu. Makine 24GB.
_GLOBAL_PW_SEM = asyncio.Semaphore(8)  # 2026-10-31: 6→8 (kalıcı tarayıcı havuzu, açılış maliyeti yok)


# ── Kalıcı Camoufox tarayıcıları (platform başına) ────────────────────────────
# crawlee her run()'da tarayıcı havuzunu açıp kapatıyor (havuz paylaşılamıyor):
# ürün başı ~9sn sadece Camoufox açılışı (2026-10-31 ölçüm: 33sn'nin 9sn'si).
# Burada platform başına bir tarayıcı açık tutulur; her ürün yine kendi temiz
# context'inde (çerezsiz) açılır. _PW_RETIRE_AFTER sayfada bir tarayıcı
# yenilenir — Camoufox parmak izi tarayıcı açılışında üretildiği için böylece
# parmak izi de dönüşümlü değişir.
_PW_RETIRE_AFTER = 40
_SELF_NAVIGATING = {"trendyol", "n11"}


class _BrowserSlot:
    def __init__(self, browser) -> None:
        self.browser = browser
        self.pages = 0
        self.inflight = 0
        self.retired = False


# Platform başına tarayıcı HAVUZU, her tarayıcıda aynı anda tek sayfa: tek
# tarayıcıyı birkaç işle paylaşınca (Trendyol 4 worker) sonraki sayfalar ~50sn
# takılıp boş dönüyordu (2026-10-31 test). Boşta tarayıcı yoksa yenisi açılır;
# havuz büyüklüğü doğal olarak o platformun eşzamanlı iş sayısını geçmez.
_PERSIST: dict[str, list[_BrowserSlot]] = {}
_PERSIST_LOCKS: dict[str, asyncio.Lock] = {}
_PW_START_LOCK = asyncio.Lock()
_PLAYWRIGHT = None


async def _close_quietly(obj) -> None:
    try:
        await asyncio.wait_for(obj.close(), timeout=15)
    except Exception:
        pass


async def _acquire_browser(platform: str) -> _BrowserSlot:
    global _PLAYWRIGHT
    async with _PW_START_LOCK:
        if _PLAYWRIGHT is None:
            from playwright.async_api import async_playwright
            _PLAYWRIGHT = await async_playwright().start()
    async with _PERSIST_LOCKS.setdefault(platform, asyncio.Lock()):
        pool = _PERSIST.setdefault(platform, [])
        for s in list(pool):
            if s.inflight == 0 and (not s.browser.is_connected() or s.pages >= _PW_RETIRE_AFTER):
                pool.remove(s)
                s.retired = True
                asyncio.create_task(_close_quietly(s.browser))
        slot = next((s for s in pool if s.inflight == 0), None)
        if slot is None:
            browser = await _camoufox_new_browser(
                _PLAYWRIGHT, headless=True, os=["windows"], locale="tr-TR",
                humanize=True, block_webrtc=True,
            )
            slot = _BrowserSlot(browser)
            pool.append(slot)
            log.info(f"[pw/{platform}] yeni Camoufox tarayıcısı açıldı (havuz={len(pool)})")
        slot.pages += 1
        slot.inflight += 1
        return slot


def _release_browser(slot: _BrowserSlot) -> None:
    slot.inflight -= 1


async def _persistent_pw_scrape(url: str, platform: str, page_handler) -> dict:
    slot = await _acquire_browser(platform)
    ctx = None
    try:
        ctx = await slot.browser.new_context()
        page = await ctx.new_page()
        await page.route("**/*", _block_route)
        # Trendyol/N11 handler'ları XHR dinleyicisini takıp sayfayı KENDİLERİ
        # açıyor — burada önceden açmak sayfayı iki kez yükletirdi.
        if platform not in _SELF_NAVIGATING:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log.debug(f"[pw/{platform}] goto hata: {e}")
                return {}
            await _human_interact(page)
        return await page_handler(page, url) or {}
    finally:
        if ctx is not None:
            await _close_quietly(ctx)
        _release_browser(slot)


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
    cb_key = f"{platform}_pw"
    if not cb_ok(cb_key):
        log.debug(f"[crawlee/{platform}] devre kesici askıda — browser başlatılmadı")
        return None

    try:
        from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
        from crawlee import ConcurrencySettings
        from crawlee.storage_clients import MemoryStorageClient
    except ImportError:
        log.warning("[crawlee] PlaywrightCrawler import başarısız")
        return None

    result: dict = {}

    if _CAMOUFOX_OK and _PROXY_CONFIG is None:
        async with _GLOBAL_PW_SEM:
            try:
                result.update(await asyncio.wait_for(
                    _persistent_pw_scrape(url, platform, page_handler), timeout=timeout))
            except asyncio.TimeoutError:
                log.warning(f"[pw/{platform}] timeout: {url[:60]}")
            except Exception as e:
                log.debug(f"[pw/{platform}] hata: {e}")
        return _pw_finish(platform, cb_key, result)

    async with _GLOBAL_PW_SEM:
        # Her çağrıda StorageInstanceManager sıfırla: önceki çağrının cached queue'sunu devralmasın
        try:
            _CrawleeSL.global_storage_instance_manager = None
        except Exception:
            pass

        crawler_kwargs = dict(
            max_requests_per_crawl=1,
            # Varsayılan 3 tekrar, tek bir timeout bütçesi içinde (DNS hatasında
            # her biri anında) boşa yanıyordu; asıl tekrar scheduler'da yapılıyor.
            max_request_retries=1,
            storage_client=MemoryStorageClient(),
            proxy_configuration=_PROXY_CONFIG,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                max_concurrency=1,
                desired_concurrency=1,
            ),
        )
        if _CAMOUFOX_OK:
            from crawlee.browsers import BrowserPool
            crawler_kwargs["browser_pool"] = BrowserPool(plugins=[_CamoufoxPlugin()])
        else:
            crawler_kwargs["headless"] = True

        # MemoryStorageClient: disk queue birikimini önler — her çağrı izole
        # min_concurrency=1: CPU yükünde autoscaler concurrency'yi 0'a indirmesin
        # İlk navigasyon: varsayılan "load" (tüm resimler) yerine HTML hazır olunca
        # dön — engelleme de navigasyondan ÖNCE takılsın (eskiden handler içinde,
        # yani ilk yüklemede resim/CSS zaten inmiş oluyordu).
        crawler_kwargs["goto_options"] = {"wait_until": "domcontentloaded"}
        crawler_kwargs["navigation_timeout"] = timedelta(seconds=30)
        crawler = PlaywrightCrawler(**crawler_kwargs)

        @crawler.pre_navigation_hook
        async def _pre_nav(ctx) -> None:
            await ctx.page.route("**/*", _block_route)

        @crawler.router.default_handler
        async def _handler(ctx: PlaywrightCrawlingContext):
            page = ctx.page
            await _human_interact(page)
            data = await page_handler(page, ctx.request.url)
            if data:
                result.update(data)

        try:
            await asyncio.wait_for(crawler.run([url]), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(f"[crawlee/{platform}] PW timeout: {url[:60]}")
        except Exception as e:
            log.debug(f"[crawlee/{platform}] PW hata: {e}")

    return _pw_finish(platform, cb_key, result)


def _pw_finish(platform: str, cb_key: str, result: dict) -> Optional[dict]:
    # Engel sayfası: sonuç döner (neden loglansın diye) ama başarı sayılmaz —
    # devre kesici ve hız sınırlayıcı için hata.
    if result and result.get("blocked"):
        cb_fail(cb_key)
        if platform in RL:
            RL[platform].record_failure()
        return result
    if result:
        cb_reset(cb_key)
        if platform in RL:
            RL[platform].record_success()
    else:
        cb_fail(cb_key)
        if platform in RL:
            RL[platform].record_failure()

    return result if result else None
