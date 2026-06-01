#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon.com.tr scraper — v8  (GitHub araştırması + TLS fingerprint optimizasyonu)

Katmanlar:
  0) curl_cffi iOS Safari   — safari TLS fingerprint, daha az bot tespiti (birincil)
  1) curl_cffi Chrome       — chrome TLS fingerprint, UA-impersonate eşleşmesi, HTTP/2 tam header set
  2) Playwright             — ASIN arama motoru, stealth, cookie kalıcılığı (son çare)

Referans: amzpy (github.com/theonlyanil/amzpy), curl_cffi luminati guide,
          ScraperAPI Amazon WAF guide, brightdata curl_cffi blog
"""

import os
import re
import logging
import json
import random
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from scrapers.utils import (
    UA_POOL, RateLimiter,
    detect_cart_discount, get_playwright_sem,
    parse_price_tr_clean, get_stealth_headers,
)

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CurlSession = None
    CURL_AVAILABLE = False

log = logging.getLogger("amazon")

import time as _time
from datetime import datetime as _dt

def _is_night_amazon() -> bool:
    """00:00–07:00 arası — Amazon gece modu (2x hız)."""
    return 0 <= _dt.now().hour < 7

# ── Rate limiter ─────────────────────────────────────────────────
# Gündüz: ~8 istek/dk → 2407 ürün → ~5 tur/gün
# Gece (00-07): ~16 istek/dk → ~2.5 tur/gün ekstra
_limiter        = RateLimiter(min_delay=1.0, max_delay=2.0)   # gündüz
_limiter_fast   = RateLimiter(min_delay=0.5, max_delay=1.0)   # price_only
_limiter_mobile = RateLimiter(min_delay=1.0, max_delay=2.0)   # mobile gündüz
_limiter_night        = RateLimiter(min_delay=0.5, max_delay=1.0)   # gece 2x
_limiter_fast_night   = RateLimiter(min_delay=0.25, max_delay=0.5)  # gece price_only
_limiter_mobile_night = RateLimiter(min_delay=0.5,  max_delay=1.0)  # gece mobile

def _get_limiter(): return _limiter_night if _is_night_amazon() else _limiter
def _get_limiter_fast(): return _limiter_fast_night if _is_night_amazon() else _limiter_fast
def _get_limiter_mobile(): return _limiter_mobile_night if _is_night_amazon() else _limiter_mobile

# ── Chrome curl circuit breaker ──────────────────────────────────
_curl_block_streak   = 0
_curl_disabled_until = 0.0
_curl_cooldown_mult  = 1
_CURL_BLOCK_LIMIT    = 5
_CURL_COOLDOWN_BASE  = 60

def _curl_ok() -> bool:
    return CURL_AVAILABLE and _time.time() > _curl_disabled_until

def _on_curl_block() -> None:
    global _curl_block_streak, _curl_disabled_until, _curl_cooldown_mult
    _curl_block_streak += 1
    if _curl_block_streak >= _CURL_BLOCK_LIMIT:
        cooldown = min(_CURL_COOLDOWN_BASE * _curl_cooldown_mult, 300)
        _curl_disabled_until = _time.time() + cooldown
        _curl_cooldown_mult  = min(_curl_cooldown_mult * 2, 5)
        _curl_block_streak   = 0
        log.warning(f"[amazon/curl] {_CURL_BLOCK_LIMIT} ardışık block → {cooldown}s askı")

def _on_curl_success() -> None:
    global _curl_block_streak, _curl_cooldown_mult
    _curl_block_streak  = 0
    _curl_cooldown_mult = 1

# ── Mobile circuit breaker ────────────────────────────────────────
_mobile_block_streak   = 0
_mobile_disabled_until = 0.0
_MOBILE_BLOCK_LIMIT    = 4    # 4 ardışık block → 90s askı
_MOBILE_COOLDOWN       = 90

def _mobile_ok() -> bool:
    return CURL_AVAILABLE and _time.time() > _mobile_disabled_until

def _on_mobile_block() -> None:
    global _mobile_block_streak, _mobile_disabled_until
    _mobile_block_streak += 1
    if _mobile_block_streak >= _MOBILE_BLOCK_LIMIT:
        _mobile_disabled_until = _time.time() + _MOBILE_COOLDOWN
        _mobile_block_streak   = 0
        log.warning(f"[amazon/mobile] {_MOBILE_BLOCK_LIMIT} ardışık block → {_MOBILE_COOLDOWN}s askı")

def _on_mobile_success() -> None:
    global _mobile_block_streak
    _mobile_block_streak = 0


# ═══════════════════════════════════════════════════════════════
# USER-AGENT VE HEADER TANIMI
# ═══════════════════════════════════════════════════════════════

# iOS Safari UA'ları — safari TLS fingerprint ile birebir eşleşmeli
_IOS_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
]

# Android Chrome UA'ları — mobil pool için iOS yanında
_ANDROID_UAS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

_AMAZON_MOBILE_UAS = _IOS_UAS + _ANDROID_UAS

# Chrome desktop UA'ları — impersonate profile ile eşleşmeli
# KEY INSIGHT: UA-impersonate uyumsuzluğu bot tespitini tetikler
_CHROME_UAS: dict[str, str] = {
    "chrome120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "chrome124": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "chrome131": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "chrome136": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "chrome142": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "chrome146": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

_CHROME_SEC_CH_UA: dict[str, str] = {
    "chrome120": '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
    "chrome124": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "chrome131": '"Google Chrome";v="131", "Chromium";v="131", "Not/A)Brand";v="24"',
    "chrome136": '"Google Chrome";v="136", "Chromium";v="136", "Not/A)Brand";v="99"',
    "chrome142": '"Google Chrome";v="142", "Chromium";v="142", "Not/A)Brand";v="24"',
    "chrome146": '"Google Chrome";v="146", "Chromium";v="146", "Not/A)Brand";v="24"',
}

# Chrome desktop header seti — HTTP/2 tam eşleşme
# zstd: Chrome 120+, Priority + Te: Chrome 119+ HTTP/2
# Sec-Fetch-Site: none — ilk (doğrudan) navigasyon için doğru değer
_CURL_HEADERS: dict[str, str] = {
    "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding":          "gzip, deflate, br, zstd",
    "Accept-Language":          "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control":            "max-age=0",
    "Dnt":                      "1",
    "Priority":                 "u=0, i",
    "Sec-Ch-Ua-Mobile":         "?0",
    "Sec-Ch-Ua-Platform":       '"Windows"',
    "Sec-Fetch-Dest":           "document",
    "Sec-Fetch-Mode":           "navigate",
    "Sec-Fetch-Site":           "none",
    "Sec-Fetch-User":           "?1",
    "Te":                       "trailers",
    "Upgrade-Insecure-Requests": "1",
}

# iOS Safari header seti — Sec-Ch-Ua yok (Safari bilmez), daha sade
_MOBILE_IOS_HEADERS: dict[str, str] = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
    "Referer":         "https://www.amazon.com.tr/",
}

# Android Chrome için ek header'lar
_MOBILE_ANDROID_EXTRA: dict[str, str] = {
    "Sec-Ch-Ua-Mobile":  "?1",
    "Sec-Ch-Ua-Platform": '"Android"',
    "Sec-Fetch-Dest":    "document",
    "Sec-Fetch-Mode":    "navigate",
    "Sec-Fetch-Site":    "none",
    "Sec-Fetch-User":    "?1",
}


# ── Amazon-specific stealth script ───────────────────────────────
_AMAZON_STEALTH = """
(function(){
  // 1. webdriver izlerini kaldır
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  try { delete navigator.__proto__.webdriver; } catch(e) {}

  // 2. Gerçek Chrome plugin listesi
  const makePlugin = (name, filename, desc, mimeTypes) => {
    const p = Object.create(Plugin.prototype);
    Object.defineProperties(p, {
      name: {value: name}, filename: {value: filename},
      description: {value: desc}, length: {value: mimeTypes.length},
    });
    mimeTypes.forEach((mt, i) => { p[i] = mt; });
    return p;
  };
  const plugins = [
    makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', []),
    makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', []),
    makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', '', []),
    makePlugin('Microsoft Edge PDF Viewer', 'msedgepdf', '', []),
    makePlugin('WebKit built-in PDF', 'webkit-web-plugin', '', []),
  ];
  Object.defineProperty(navigator, 'plugins', { get: () => plugins });
  Object.defineProperty(navigator, 'mimeTypes', { get: () => [] });

  // 3. Dil, platform, donanım
  Object.defineProperty(navigator, 'languages',          {get: () => ['tr-TR','tr','en-US','en']});
  Object.defineProperty(navigator, 'platform',           {get: () => 'Win32'});
  Object.defineProperty(navigator, 'hardwareConcurrency',{get: () => 8});
  Object.defineProperty(navigator, 'deviceMemory',       {get: () => 8});
  Object.defineProperty(navigator, 'maxTouchPoints',     {get: () => 0});
  Object.defineProperty(navigator, 'vendor',             {get: () => 'Google Inc.'});
  Object.defineProperty(navigator, 'appVersion',         {get: () => '5.0 (Windows)'});

  // 4. Chrome runtime objesi (tam)
  if (!window.chrome) {
    window.chrome = {
      app: {isInstalled: false},
      runtime: {
        id: undefined,
        connect: function(){},
        sendMessage: function(){},
        onMessage: {addListener: function(){}, removeListener: function(){}},
      },
      loadTimes: function(){
        return {
          requestTime: Date.now()/1000 - 0.5,
          startLoadTime: Date.now()/1000 - 0.4,
          commitLoadTime: Date.now()/1000 - 0.3,
          finishDocumentLoadTime: Date.now()/1000,
          finishLoadTime: Date.now()/1000,
          firstPaintTime: 0, firstPaintAfterLoadTime: 0,
          navigationType: 'Other',
          wasFetchedViaSpdy: true, wasNpnNegotiated: true,
          npnNegotiatedProtocol: 'h2',
          wasAlternateProtocolAvailable: false,
          connectionInfo: 'h2',
        };
      },
      csi: function(){
        return {startE: Date.now(), onloadT: Date.now(), pageT: 500, tran: 15};
      },
    };
  }

  // 5. Permissions API — notifications için gerçek değer
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
      if (params && params.name === 'notifications') {
        return Promise.resolve({state: 'default', onchange: null});
      }
      return origQuery(params);
    };
  }

  // 6. WebGL fingerprint — yaygın Intel değerleri
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
      return getParam.call(this, p);
    };
  } catch(e) {}

  // 7. iframe içinde de webdriver gizle
  try {
    const origDescriptor = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      get: function() {
        const cw = origDescriptor.get.call(this);
        try { Object.defineProperty(cw.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
        return cw;
      }
    });
  } catch(e) {}

  // 8. Otomasyon tespit metodlarını kaldır
  ['$cdc_asdjflasutopfhvcZLmcfl_', '$chrome_asyncScriptInfo', '__$webdriverAsyncExecutor',
   '__lastWatirAlert', '__lastWatirConfirm', '__lastWatirPrompt'].forEach(k => {
    try { delete window[k]; } catch(e) {}
  });
})();
"""


# ═══════════════════════════════════════════════════════════════
# BLOCK / NOT-FOUND TESPİT
# ═══════════════════════════════════════════════════════════════

CAPTCHA_HINTS = [
    "robot check", "validatecaptcha", "type the characters",
    "enter the characters", "not a robot", "güvenlik kontrolü",
    "lütfen aşağıdaki karakterleri girin", "captcha",
]
BLOCK_HINTS = [
    "üzgünüz", "isteğinizi işlemeye", "sorun üzerinde çalışıyoruz",
    "something went wrong", "we're sorry",
    "api-services-support@amazon",    # amzpy: bu string block sayfasında kesinlikle var
]
NOT_FOUND_HINTS = [
    "sitemizde işlev gösteren bir sayfaya karşılık gelmiyor",
    "aradığınız bir şey mi var",
    "sayfa bulunamadı",
    "dogs of amazon",
]


def _is_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    t = html[:10000].lower()
    return any(h in t for h in CAPTCHA_HINTS + BLOCK_HINTS)


def _is_not_found(html: str) -> bool:
    if not html:
        return False
    t = html[:5000].lower()
    return any(h in t for h in NOT_FOUND_HINTS)


# ═══════════════════════════════════════════════════════════════
# COOKIE YÖNETİMİ  (tüm katmanlar arası paylaşım)
# ═══════════════════════════════════════════════════════════════

_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "amazon_cookies.json")
_cached_cookies: dict | None = None
_cookie_file_mtime: float | None = None


def _load_amazon_cookies() -> dict:
    global _cached_cookies, _cookie_file_mtime
    try:
        path = os.path.abspath(_COOKIE_FILE)
        mtime = os.path.getmtime(path)
        if _cached_cookies is not None and _cookie_file_mtime == mtime:
            return _cached_cookies
        with open(path, "r", encoding="utf-8") as f:
            cookies_list = json.load(f)
        _cached_cookies = {c["name"]: c["value"] for c in cookies_list if "name" in c and "value" in c}
        _cookie_file_mtime = mtime
        return _cached_cookies
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug(f"[amazon] cookie yüklenemedi: {e}")
        return {}


def _save_cookies_file(cookie_dict: dict) -> None:
    try:
        path = os.path.abspath(_COOKIE_FILE)
        existing = _load_amazon_cookies()
        existing.update(cookie_dict)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"name": k, "value": v} for k, v in existing.items()], f, ensure_ascii=False)
        global _cached_cookies, _cookie_file_mtime
        _cached_cookies = None   # force reload next time
    except Exception as e:
        log.debug(f"[amazon] cookie kaydetme hatası: {e}")


def _push_cookies_to_sessions(cookie_dict: dict) -> None:
    """Cookie güncellemesini tüm curl sessionlara aktar."""
    if not cookie_dict:
        return
    for pool_list in (_MOBILE_SESSIONS, _SESSIONS):
        for entry in pool_list:
            if entry is None:
                continue
            session = entry[0]
            try:
                session.cookies.update(cookie_dict)
            except Exception:
                pass


async def _save_playwright_cookies(page) -> None:
    try:
        cookies = await page.context.cookies()
        if not cookies:
            return
        cookie_dict = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
        _save_cookies_file(cookie_dict)
        _push_cookies_to_sessions(cookie_dict)
        log.info(f"[amazon] {len(cookies)} cookie Playwright'tan kaydedildi → tüm sessionlara aktarıldı")
    except Exception as e:
        log.debug(f"[amazon] Playwright cookie kaydetme hatası: {e}")


# ═══════════════════════════════════════════════════════════════
# KATMAN 0: iOS Safari session pool
# ═══════════════════════════════════════════════════════════════

MOBILE_IMPERSONATE_POOL = ["safari18_0"]  # iOS Safari TLS — HB ile aynı yöntem

_AMAZON_SEM = None
def _get_amazon_sem():
    global _AMAZON_SEM
    if _AMAZON_SEM is None:
        _AMAZON_SEM = asyncio.Semaphore(3)
    return _AMAZON_SEM
_MOBILE_POOL_SIZE   = 3    # 3 paralel session (birincil katman)
_MOBILE_SESSIONS: list = []
_MOBILE_SESSIONS_LOCK = asyncio.Lock()
_mobile_session_idx   = 0


async def _get_mobile_session() -> tuple:
    global _MOBILE_SESSIONS, _mobile_session_idx
    new_entry = None
    async with _MOBILE_SESSIONS_LOCK:
        if len(_MOBILE_SESSIONS) < _MOBILE_POOL_SIZE:
            try:
                imp = random.choice(MOBILE_IMPERSONATE_POOL)
                from scrapers.proxy_pool import get_proxy_pool
                _pp  = get_proxy_pool()
                proxy = _pp.get() if _pp.has_proxies else None
                s    = CurlSession(
                    impersonate=imp, timeout=15,
                    proxies=_pp.curl_dict(proxy) if proxy else None,
                )
                cookies = _load_amazon_cookies()
                if cookies:
                    s.cookies.update(cookies)
                _MOBILE_SESSIONS.append((s, imp, proxy))
                new_entry = (s, imp, proxy)
                log.info(f"[amazon/mobile] Yeni session: {imp} proxy={'✔' if proxy else '✘'}")
            except Exception as e:
                log.warning(f"[amazon/mobile] Session oluşturulamadı: {e}")
                if not _MOBILE_SESSIONS:
                    raise
        if not _MOBILE_SESSIONS:
            raise RuntimeError("Mobile session pool boş")
        _mobile_session_idx = (_mobile_session_idx + 1) % len(_MOBILE_SESSIONS)
        result = _MOBILE_SESSIONS[_mobile_session_idx]

    if new_entry:
        s, imp, proxy = new_entry
        try:
            ua = random.choice(_IOS_UAS)
            warmup_hdrs = {**_MOBILE_IOS_HEADERS, "User-Agent": ua}
            r = await s.get("https://www.amazon.com.tr/", headers=warmup_hdrs, timeout=10, stream=True)
            await r.aclose()
        except Exception:
            pass
    return result


def _reset_mobile_session() -> None:
    global _MOBILE_SESSIONS, _mobile_session_idx
    if not _MOBILE_SESSIONS:
        return
    bad = _mobile_session_idx % len(_MOBILE_SESSIONS)
    _MOBILE_SESSIONS[bad] = None  # type: ignore
    _MOBILE_SESSIONS = [s for s in _MOBILE_SESSIONS if s is not None]
    log.info(f"[amazon/mobile] Session #{bad} sıfırlandı, kalan={len(_MOBILE_SESSIONS)}")


async def _via_mobile(url: str) -> Optional[dict]:
    """Layer 0: Android Chrome mobile — proxy destekli birincil katman."""
    if not CURL_AVAILABLE:
        return None
    proxy = None
    try:
        await _get_limiter_mobile().wait()
        session, imp, proxy = await _get_mobile_session()
        ua = random.choice(_IOS_UAS)
        headers = {**_MOBILE_IOS_HEADERS, "User-Agent": ua}

        r = await session.get(url, headers=headers, allow_redirects=True, timeout=15)
        if r.status_code in (404, 410):
            return {"not_found": True}
        if r.status_code in (403, 429, 503):
            _on_mobile_block()
            _reset_mobile_session()
            return None
        if r.status_code != 200:
            _reset_mobile_session()
            return None

        html = r.text
        if not html or len(html) < 500:
            return None
        if _is_not_found(html):
            return {"not_found": True}
        if _is_blocked(html):
            log.info(f"[amazon/mobile] Block algılandı — session sıfırlanıyor (imp={imp})")
            _on_mobile_block()
            _reset_mobile_session()
            return None

        # Cookie'leri dosyaya kaydet + chrome sessionlara aktar
        if hasattr(r, "cookies") and r.cookies:
            try:
                cdict = dict(r.cookies)
                _save_cookies_file(cdict)
                _push_cookies_to_sessions(cdict)
            except Exception:
                pass

        data = await asyncio.to_thread(_parse, html)
        if data:
            log.info(f"[amazon/mobile] ✔ imp={imp} ua=iOS proxy={'✔' if proxy else '✘'}")
            _on_mobile_success()
            if proxy:
                from scrapers.proxy_pool import get_proxy_pool
                get_proxy_pool().mark_ok(proxy)
        return data

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.debug(f"[amazon/mobile] Hata: {e}")
        if proxy:
            from scrapers.proxy_pool import get_proxy_pool
            get_proxy_pool().mark_failed(proxy)
        _reset_mobile_session()
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 1: Chrome curl_cffi session pool
# ═══════════════════════════════════════════════════════════════

IMPERSONATE_POOL = ["chrome136", "chrome131", "chrome124", "chrome120", "chrome142", "chrome146"]
_POOL_SIZE    = 3
_SESSIONS: list = []
_SESSIONS_LOCK = asyncio.Lock()
_session_idx   = 0


async def _get_or_create_session() -> tuple:
    global _SESSIONS, _session_idx
    new_entry = None
    async with _SESSIONS_LOCK:
        if len(_SESSIONS) < _POOL_SIZE:
            from scrapers.proxy_pool import get_proxy_pool
            proxy = get_proxy_pool().get()
            imp   = random.choice(IMPERSONATE_POOL)
            s     = CurlSession(
                impersonate=imp,
                timeout=12,
                proxies=get_proxy_pool().curl_dict(proxy) if proxy else None,
            )
            cookies = _load_amazon_cookies()
            if cookies:
                s.cookies.update(cookies)
            _SESSIONS.append((s, imp, proxy))
            new_entry = (s, imp, proxy)
            log.info(f"[amazon] Yeni Chrome session #{len(_SESSIONS)}: {imp} proxy={'✔' if proxy else '✘'}")

        if not _SESSIONS:
            raise RuntimeError("Chrome session pool boş")
        _session_idx = (_session_idx + 1) % len(_SESSIONS)
        result = _SESSIONS[_session_idx]

    if new_entry:
        s, imp, proxy = new_entry
        ua = _CHROME_UAS.get(imp, _CHROME_UAS["chrome131"])
        warmup_hdrs = {
            **_CURL_HEADERS,
            "User-Agent": ua,
            "Sec-Ch-Ua": _CHROME_SEC_CH_UA.get(imp, _CHROME_SEC_CH_UA["chrome131"]),
        }
        try:
            await s.get("https://www.amazon.com.tr/", headers=warmup_hdrs, timeout=8)
            log.info(f"[amazon] Chrome session warm-up tamamlandı: {imp}")
        except Exception:
            pass
    return result


def _reset_session() -> None:
    global _SESSIONS, _session_idx
    if not _SESSIONS:
        return
    bad = _session_idx % len(_SESSIONS)
    try:
        _SESSIONS[bad] = None  # type: ignore
    except Exception:
        pass
    _SESSIONS = [s for s in _SESSIONS if s is not None]
    log.info(f"[amazon] Chrome session #{bad} sıfırlandı, kalan={len(_SESSIONS)}")


async def _via_curl(url: str) -> Optional[dict]:
    """Layer 1: Chrome TLS fingerprint, tam HTTP/2 header seti."""
    if not CURL_AVAILABLE:
        return None
    try:
        session, imp, proxy = await _get_or_create_session()
        ua = _CHROME_UAS.get(imp, _CHROME_UAS["chrome131"])
        headers = {
            **_CURL_HEADERS,
            "User-Agent": ua,
            "Sec-Ch-Ua": _CHROME_SEC_CH_UA.get(imp, _CHROME_SEC_CH_UA["chrome131"]),
            "Referer": "https://www.amazon.com.tr/",
        }
        r    = await session.get(url, headers=headers, allow_redirects=True)
        html = r.text

        if r.status_code in (404, 410):
            return {"not_found": True}
        if r.status_code != 200:
            _reset_session()
            return None
        if not html or len(html) < 500:
            return None
        if _is_not_found(html):
            return {"not_found": True}
        if _is_blocked(html):
            log.info(f"[amazon/curl] CAPTCHA/block → session sıfırlanıyor (imp={imp})")
            _reset_session()
            _on_curl_block()
            if proxy:
                from scrapers.proxy_pool import get_proxy_pool
                get_proxy_pool().mark_failed(proxy)
            return {"blocked": True}

        if proxy:
            from scrapers.proxy_pool import get_proxy_pool
            get_proxy_pool().mark_ok(proxy)

        # Cookie paylaşımı
        if hasattr(r, "cookies") and r.cookies:
            try:
                cdict = dict(r.cookies)
                _save_cookies_file(cdict)
                _push_cookies_to_sessions(cdict)
            except Exception:
                pass

        return await asyncio.to_thread(_parse, html)

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.debug(f"[amazon/curl] Hata: {e}")
        _reset_session()
        return None


# ═══════════════════════════════════════════════════════════════
# ANA FONKSİYON
# ═══════════════════════════════════════════════════════════════

async def scrape_amazon(url: str, pool=None, price_only: bool = False,
                        cached_image: str = None) -> Optional[dict]:
    async with _get_amazon_sem():
        result = await _scrape_amazon(url, pool, price_only=price_only)
    if not result:
        return None
    if result.get("dead_url") or result.get("not_found"):
        return result
    if price_only:
        out = {k: result[k] for k in ("price", "stock", "cart_discount", "coupon") if k in result}
        if cached_image:
            out["image_url"] = cached_image
        return out
    return result


async def _scrape_amazon(url: str, pool, price_only: bool = False) -> Optional[dict]:
    await (_get_limiter_fast() if price_only else _get_limiter()).wait()

    # Layer 0: iOS Safari curl_cffi — birincil (HB ile aynı)
    if _mobile_ok():
        data = await _via_mobile(url)
        if data and data.get("dead_url"):   return data
        if data and data.get("not_found"):  return data
        if data and data.get("price"):
            _on_mobile_success()
            return data
        _on_mobile_block()
        log.info(f"[amazon] mobile başarısız ({_mobile_block_streak}/{_MOBILE_BLOCK_LIMIT}) → Chrome")
    else:
        remain = max(0, _mobile_disabled_until - _time.time())
        log.info(f"[amazon/mobile] circuit breaker aktif ({remain:.0f}s) → Chrome")

    # Layer 1: Chrome curl_cffi — mobile blokta yedek
    if _curl_ok():
        data = await _via_curl(url)
        if data and data.get("dead_url"):   return data
        if data and data.get("not_found"):  return data
        if data and data.get("price"):
            _on_curl_success()
            return data
        if data and data.get("blocked"):
            _on_curl_block()
    else:
        remain = max(0, _curl_disabled_until - _time.time())
        log.info(f"[amazon/curl] circuit breaker aktif ({remain:.0f}s) → Playwright")

    # Layer 2: Playwright — son çare
    if pool is not None:
        data = await _via_playwright_search(url, pool=pool)
        if data and data.get("price"):
            log.info(f"[amazon/pw] ✔ title={str(data.get('title',''))[:50]} price={data.get('price')}")
        return data

    log.warning(f"[amazon] pool yok, tüm yöntemler başarısız")
    return None


def _price_only_filter(data: Optional[dict], price_only: bool, cached_image: str = None) -> Optional[dict]:
    if not price_only or not data:
        return data
    result = {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}
    if cached_image:
        result["image_url"] = cached_image
    return result


# ═══════════════════════════════════════════════════════════════
# KATMAN 2: Playwright — ASIN arama motoru
# ═══════════════════════════════════════════════════════════════

_search_counter   = 0
_SEARCH_RESET_EVERY = 40


async def _via_playwright_search(url: str, pool=None) -> Optional[dict]:
    global _search_counter

    asin_m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if not asin_m:
        return await _via_playwright(url, pool=pool)

    asin = asin_m.group(1)
    _search_counter += 1

    if pool is None:
        return await _launch_playwright(url)

    page = await pool.acquire()
    try:
        from scrapers.cdp_base import setup_resource_blocking_amazon
        await setup_resource_blocking_amazon(page)
        await page.add_init_script(_AMAZON_STEALTH)

        log.info(f"[amazon/search] #{_search_counter} ASIN={asin}")
        # Direkt ürün sayfasına git — search navigation zaman kaybı
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)

        if page.is_closed():
            return None

        try:
            await page.wait_for_selector(
                "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_offerDisplay_desktop",
                timeout=5000,
            )
        except Exception:
            pass

        await asyncio.sleep(random.uniform(0.3, 0.8))
        if not page.is_closed():
            await page.mouse.wheel(0, random.randint(300, 600))
            await asyncio.sleep(random.uniform(0.2, 0.4))

        html = await page.content() if not page.is_closed() else None
        if html and len(html) > 5000:
            if _is_not_found(html):
                return {"not_found": True}
            if _is_blocked(html):
                return None
            data = await asyncio.to_thread(_parse, html)
            if data and data.get("price") and random.random() < 0.3:
                await _save_playwright_cookies(page)
            return data
        return None

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.error(f"[amazon/search] Hata: {e}")
        return None
    finally:
        await pool.release(page)


async def _via_playwright(url: str, pool=None) -> Optional[dict]:
    if pool is None:
        return await _launch_playwright(url)
    page = await pool.acquire()
    try:
        from scrapers.cdp_base import setup_resource_blocking_amazon
        await setup_resource_blocking_amazon(page)
        await page.add_init_script(_AMAZON_STEALTH)

        if random.random() < 0.15:
            try:
                await page.goto("https://www.amazon.com.tr/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(random.uniform(0.5, 1.2))
            except Exception:
                pass

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if page.is_closed():
            return None
        try:
            await page.wait_for_selector(
                "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_offerDisplay_desktop",
                timeout=5000,
            )
        except Exception:
            pass
        if page.is_closed():
            return None
        await asyncio.sleep(random.uniform(0.3, 0.8))
        if not page.is_closed():
            await page.mouse.wheel(0, random.randint(300, 600))
            await asyncio.sleep(random.uniform(0.2, 0.5))

        html = await page.content() if not page.is_closed() else None
        if html and len(html) > 5000:
            if _is_not_found(html):
                return {"not_found": True}
            if _is_blocked(html):
                return None
            data = await asyncio.to_thread(_parse, html)
            if data and data.get("price") and random.random() < 0.3:
                await _save_playwright_cookies(page)
            return data
        return None
    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.error(f"[amazon/playwright] Hata: {e}")
        return None
    finally:
        await pool.release(page)


async def _launch_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth()
        except Exception:
            _stealth = None

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ])
            try:
                _ua = random.choice(UA_POOL)
                context = await browser.new_context(
                    user_agent=_ua,
                    locale="tr-TR",
                    timezone_id="Europe/Istanbul",
                    viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
                    extra_http_headers=get_stealth_headers(_ua),
                )
                await context.add_init_script(_AMAZON_STEALTH)
                page = await context.new_page()
                if _stealth:
                    await _stealth.apply_stealth_async(page)
                await page.route("**/*.{gif,svg,ico,woff,woff2}", lambda r: r.abort())
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                try:
                    await page.wait_for_selector("#productTitle", timeout=8000)
                except Exception:
                    pass
                try:
                    await page.wait_for_selector(
                        "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_offerDisplay_desktop",
                        timeout=5000,
                    )
                except Exception:
                    pass
                await page.mouse.wheel(0, random.randint(250, 600))
                await asyncio.sleep(random.uniform(0.3, 0.7))
                html = await page.content()
            finally:
                await browser.close()

        if _is_not_found(html):
            return {"not_found": True}
        if _is_blocked(html):
            return None
        return await asyncio.to_thread(_parse, html)

    except NotImplementedError:
        log.info("[amazon/playwright] Python sürümü desteklenmiyor")
        return None
    except Exception as e:
        log.error(f"[amazon/playwright] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# PARSE  — HTML → dict
# ═══════════════════════════════════════════════════════════════

def _parse_price_tr(price_text: str) -> Optional[float]:
    return parse_price_tr_clean(price_text)


def _parse_coupon(soup: BeautifulSoup, html: str) -> Optional[str]:
    for sel in [
        "#couponDeals", "#couponBadge_feature_div", ".couponBadge",
        "#prime_free_tie_feature_div", "[id*='coupon']", "[class*='coupon-badge']",
    ]:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if t and len(t) > 2:
                return t[:200]
    for pat in [
        r'(%\s*\d+)\s*(?:ekstra\s+)?(?:indirim\s+)?kupon',
        r'kupon\s*uygula[^<"]{0,80}',
        r'\d+\s*TL\s*kupon',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(0)[:200].strip()
    return None


def _parse_variants(soup: BeautifulSoup, html: str) -> list:
    variants: list = []
    seen: set = set()

    for li in soup.select(
        "#twister_feature_div li[data-dp-url], "
        "#twister_feature_div li[data-defaultasin], "
        "#variation_color_name li[data-dp-url], "
        "#variation_size_name li[data-dp-url]"
    ):
        dp_url = li.get("data-dp-url", "") or ""
        m = re.search(r"/dp/([A-Z0-9]{10})", dp_url)
        asin = m.group(1) if m else None
        if not asin:
            da = (li.get("data-defaultasin") or "").strip()
            if re.match(r"^[A-Z0-9]{10}$", da):
                asin = da
        if not asin or asin in seen:
            continue
        seen.add(asin)
        name = (li.get("title") or "").strip()
        if not name:
            img = li.select_one("img")
            if img:
                name = (img.get("alt") or "").strip()
        if not name:
            span = li.select_one("span.a-size-base, span")
            if span:
                name = span.get_text(strip=True)
        if not name:
            name = asin
        variants.append({"asin": asin, "name": name[:100], "price": None})

    if not variants:
        m = re.search(r'"asinToDpUrl"\s*:\s*(\{[^}]{10,5000}\})', html)
        if m:
            try:
                for asin_key in json.loads(m.group(1)):
                    if re.match(r"^[A-Z0-9]{10}$", asin_key) and asin_key not in seen:
                        seen.add(asin_key)
                        variants.append({"asin": asin_key, "name": asin_key, "price": None})
            except Exception:
                pass

    price_map: dict = {}
    for pat in [
        r'"priceMap"\s*:\s*(\{[^}]{10,10000}\})',
        r'"variationPrices"\s*:\s*(\{[^}]{10,10000}\})',
    ]:
        pm = re.search(pat, html)
        if pm:
            try:
                raw = json.loads(pm.group(1))
                for k, v in raw.items():
                    if re.match(r"^[A-Z0-9]{10}$", k):
                        p = parse_price_tr_clean(str(v))
                        if p:
                            price_map[k] = p
            except Exception:
                pass
        if price_map:
            break

    if not price_map:
        for dm in re.finditer(r'"([A-Z0-9]{10})"[^}]{0,300}"displayPrice"\s*:\s*"([^"]+)"', html):
            p = parse_price_tr_clean(dm.group(2))
            if p:
                price_map[dm.group(1)] = p

    for v in variants:
        if v["asin"] in price_map:
            v["price"] = price_map[v["asin"]]

    return variants[:50]


_NOISE_SELECTORS = [
    "#similarities_feature_div",
    "#sims-consolidated-1_feature_div",
    "#sims-consolidated-2_feature_div",
    "[id*='sims-']",
    "[id*='p13n']",
    "[id*='sp_detail']",
    ".a-carousel-viewport",
    "#HLCXComparisonWidget_feature_div",
    "[id*='comparison']",
    "[id*='viewed-together']",
    "[id*='discovery']",
    "#discovery-and-inspiration",
    "#rhf",
    "[id*='rhf_']",
    "[class*='_vc-pfo']",
    "[class*='vc-pfo']",
    "#buyers-also-bought-recommendation-carousel",
    "[data-component-type='s-search-results']",
]


def _parse(html: str) -> Optional[dict]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for sel in _NOISE_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass

    # ── Başlık ──────────────────────────────────────────────
    title = None
    for sel in ["#productTitle", "h1#title span", "span#productTitle"]:
        el = soup.select_one(sel)
        if el:
            t = re.sub(r"\s+", " ", el.get_text()).strip()
            if t and len(t) > 3:
                title = t
                break

    if not title:
        t_el = soup.select_one("title")
        if t_el:
            raw = re.sub(r"\s+", " ", t_el.get_text()).strip()
            raw = re.sub(r"^Amazon\.com(\.tr)?[:\s]*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*[:–\-]\s*Amazon.*$", "", raw, flags=re.IGNORECASE)
            if raw and len(raw) > 5:
                title = raw

    if not title:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                if ld.get("name"):
                    title = ld["name"]
                    break
            except Exception:
                pass

    # ── Fiyat ───────────────────────────────────────────────
    price = None
    price_text = None

    for sel in [
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#corePriceDisplay_mobile_feature_div span.a-offscreen",
        "#corePrice_feature_div span.a-offscreen",
        "#apex_offerDisplay_desktop span.a-offscreen",
        "#apex_offerDisplay_mobile span.a-offscreen",
        "#mobile-buybox span.a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            price_text = el.get_text(strip=True) or None
            if price_text:
                break

    if not price_text:
        container = soup.select_one(
            "#corePriceDisplay_desktop_feature_div, #corePriceDisplay_mobile_feature_div, "
            "#corePrice_feature_div, #apex_offerDisplay_desktop, #apex_offerDisplay_mobile, "
            "#mobile-buybox, #buybox, #ppd"
        )
        if container:
            whole = container.select_one("span.a-price-whole")
            frac  = container.select_one("span.a-price-fraction")
            if whole:
                w = whole.get_text(strip=True).replace(".", "").replace(",", "")
                f = (frac.get_text(strip=True) if frac else "00").strip()
                if w.isdigit():
                    price_text = f"{w},{f} TL"

    if price_text:
        price = _parse_price_tr(price_text)

    if not price:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                offers = ld.get("offers", {})
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    p = offers.get("price") or offers.get("lowPrice")
                    if p:
                        price = parse_price_tr_clean(str(p))
                        break
            except Exception:
                pass

    if not price:
        m = re.search(r'"priceAmount"[:\s]*([\d.]+)', html)
        if m:
            try:
                price = float(m.group(1))
            except Exception:
                pass

    # ── Resim ──────────────────────────────────────────────
    image_url = None
    for sel in ["#landingImage", "#imgBlkFront", "#main-image", "img.a-dynamic-image"]:
        img = soup.select_one(sel)
        if img:
            image_url = img.get("data-old-hires") or img.get("src")
            if image_url and image_url.startswith("http"):
                break
            image_url = None

    if not image_url:
        for img in soup.select("img[data-a-dynamic-image]"):
            try:
                dyn = json.loads(img.get("data-a-dynamic-image", "{}"))
                if dyn:
                    best = max(dyn.keys(), key=lambda k: sum(dyn[k]))
                    if best.startswith("http"):
                        image_url = best
                        break
            except Exception:
                pass

    if not image_url:
        og = soup.find("meta", {"property": "og:image"})
        if og and og.get("content"):
            image_url = og["content"]

    if not image_url:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                img = ld.get("image")
                if isinstance(img, list) and img:
                    image_url = img[0]
                elif isinstance(img, str) and img:
                    image_url = img
                if image_url:
                    break
            except Exception:
                pass

    if not image_url:
        m = re.search(r'"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        if m:
            image_url = m.group(1)

    # ── Rating ──────────────────────────────────────────────
    rating = None
    rating_el = soup.select_one("span.a-icon-alt")
    if rating_el:
        m = re.search(r"([\d,]+)", rating_el.text)
        if m:
            r_val = float(m.group(1).replace(",", "."))
            if 0 < r_val <= 5:
                rating = r_val

    # ── Yorum sayısı ────────────────────────────────────────
    reviews = None
    rev_el = soup.select_one("#acrCustomerReviewText")
    if rev_el:
        m = re.search(r"[\d.]+", rev_el.text.replace(".", "").replace(",", ""))
        if m:
            reviews = int(m.group())

    # ── Stok ────────────────────────────────────────────────
    stock = None
    for sel in [
        "#availability span.primary-availability-message",
        ".primary-availability-message",
        "#availability span",
        "#availability",
        "div#availability span",
        "#availabilityInsideBuyBox_feature_div span",
        "#availability_feature_div span",
        "#desktop_qualifiedBuyBox_feature_div span",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                tl = t.lower()
                if any(k in tl for k in (
                    "stokta", "kargo", "teslim", "kaldı", "sepete ekle",
                    "satışta", "sipariş ver", "in stock", "available"
                )):
                    stock = "Stokta Var"
                elif any(k in tl for k in (
                    "mevcut değil", "tükendi", "stokta yok", "out of stock",
                    "currently unavailable", "temporarily out of stock",
                    "unavailable", "geçici olarak stokta yok", "şu an için stokta yok"
                )):
                    stock = "Stok Yok"
                elif "stok" not in tl and "yok" in tl:
                    stock = "Stok Yok"
                else:
                    stock = t
                break

    has_atc = bool(soup.select_one(
        "#add-to-cart-button, input[name='submit.add-to-cart'], "
        "#add-to-cart-announce, #attachSiNAButton"
    ))
    has_buy_now = bool(soup.select_one("#buy-now-button, #buy-now-button-announce"))
    has_oos     = bool(soup.select_one("#outOfStock, .outOfStock"))
    html_low    = html.lower()

    if has_atc or has_buy_now:
        stock = "Stokta Var"
    elif has_oos:
        stock = "Stok Yok"
    elif "currently unavailable" in html_low or "temporarily out of stock" in html_low:
        stock = "Stok Yok"
    elif stock == "Stokta Var":
        stock = "Stokta Var"
    else:
        stock = "Stok Yok"

    if stock == "Stok Yok":
        price = None

    # ── Barkod ──────────────────────────────────────────────
    barcode = None
    for row in soup.select(
        "#productDetails_techSpec_section_1 tr, "
        "#productDetails_detailBullets_sections1 tr, "
        ".a-normal tr"
    ):
        label = row.select_one("th, td:first-child")
        value = row.select_one("td:last-child")
        if label and value:
            lbl = label.get_text(strip=True).lower()
            if any(k in lbl for k in ["ean", "gtin", "barkod", "barcode", "upc"]):
                bc = re.sub(r"[^\d]", "", value.get_text(strip=True))
                if bc and 8 <= len(bc) <= 14:
                    barcode = bc
                    break

    if not barcode:
        for pat in [r'"ean"\s*:\s*"(\d{8,14})"', r'"gtin"\s*:\s*"(\d{8,14})"', r'EAN[:\s]+(\d{8,14})']:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                barcode = m.group(1)
                break

    # ── Sepette indirim ─────────────────────────────────────
    cart_discount = detect_cart_discount(html)
    if not cart_discount:
        for sel in ["#priceBadging_feature_div", "#promoPriceBlockMessage_feature_div"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                cart_discount = True
                break

    coupon   = _parse_coupon(soup, html)
    variants = _parse_variants(soup, html)

    log.info(
        f"[amazon] parse: title={'✔' if title else '✘'} price={price} barcode={barcode} "
        f"image={'✔' if image_url else '✘'} stock={stock} "
        f"cart_discount={cart_discount} coupon={'✔' if coupon else '✘'} variants={len(variants)}"
    )
    return {
        "title":        title,
        "price":        price,
        "image_url":    image_url,
        "rating":       rating,
        "review_count": reviews,
        "stock":        stock,
        "barcode":      barcode,
        "cart_discount": cart_discount,
        "coupon":       coupon,
        "variants":     variants if variants else None,
    }


# ═══════════════════════════════════════════════════════════════
# SCRAPE.DO TABANLI CLI ARAÇLARI  (token ile doğrudan erişim)
# ═══════════════════════════════════════════════════════════════

API_BASE = "https://api.scrape.do"
_VARIATION_PRIORITY = ["color", "size", "style", "pattern", "material", "fit"]


def _cli_get_token(token_arg: str = None) -> str:
    token = token_arg or os.environ.get("SCRAPEDO_TOKEN", "")
    if not token:
        raise ValueError("Scrape.do token gerekli. --token parametresi veya SCRAPEDO_TOKEN env var kullanın.")
    return token


def _cli_asin_from_url(url: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else url.strip("/").split("/")[-1]


def _cli_fetch_html(target_url: str, token: str) -> str:
    import requests
    r = requests.get(
        f"{API_BASE}/",
        params={"token": token, "url": target_url, "render": "true"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def _cli_fetch_raw(target_url: str, token: str) -> str:
    import requests
    r = requests.get(
        f"{API_BASE}/",
        params={"token": token, "url": target_url},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def _cli_extract_price(soup: BeautifulSoup) -> str:
    whole = soup.find("span", class_="a-price-whole")
    frac  = soup.find("span", class_="a-price-fraction")
    if whole:
        return f"${whole.text.strip('.')}.{frac.text.strip() if frac else '00'}"
    off = soup.select_one("span.a-offscreen")
    return off.text.strip() if off else "N/A"


def _cli_parse_usd(price_str: str) -> Optional[float]:
    m = re.search(r"[\d]+\.?\d*", price_str.replace(",", ""))
    try:
        return float(m.group()) if m else None
    except Exception:
        return None


def _cli_soup_text(el, default: str = "N/A") -> str:
    return el.get_text(strip=True) if el else default


def _cli_find_match(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else "N/A"


def _cli_upsert_product(db, asin: str, source_url: str, title: str,
                        image: str, rating_str: str, price_str: str, now: str) -> int:
    """products + price_history upsert eder, product_id döner."""
    try:
        rating_val = float(rating_str) if rating_str and rating_str not in ("N/A", "") else None
    except ValueError:
        rating_val = None

    cur = db.execute(
        "INSERT OR IGNORE INTO products"
        "(platform,asin_or_id,source_url,title,image_url,rating,first_seen_at,last_seen_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("amazon", asin, source_url, title, image, rating_val, now, now),
    )
    db.commit()

    if cur.rowcount > 0:
        product_id = cur.lastrowid
    else:
        db.execute("UPDATE products SET last_seen_at=? WHERE source_url=?", (now, source_url))
        row = db.execute("SELECT id FROM products WHERE source_url=?", (source_url,)).fetchone()
        db.commit()
        product_id = row["id"]

    price_val = _cli_parse_usd(price_str)
    if price_val:
        db.execute(
            "INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
            (product_id, price_val, "USD", now),
        )
        db.commit()

    return product_id


# ----------------------------------------------------------------------------- #
#  1) TEMEL ÜRÜN VERİSİ
# ----------------------------------------------------------------------------- #

def scrape_product_html(target_url: str, token: str) -> dict:
    """Tek ürün sayfasını parse eder: isim, ASIN, fiyat, görsel, rating."""
    soup    = BeautifulSoup(_cli_fetch_html(target_url, token), "lxml")
    name_el = soup.find(id="productTitle")
    name    = name_el.text.strip() if name_el else "N/A"
    asin    = _cli_asin_from_url(target_url)
    price   = _cli_extract_price(soup)
    img_el  = soup.find("img", {"id": "landingImage"})
    image   = img_el["src"] if img_el and img_el.has_attr("src") else "N/A"
    rat_el  = soup.find(class_="AverageCustomerReviews")
    rating  = rat_el.text.strip().split(" out of")[0] if rat_el else "N/A"
    return {"ASIN": asin, "Product Name": name, "Price": price, "Image": image, "Rating": rating}


def scrape_product_api(asin: str, token: str, geocode: str = "us", zipcode: str = "10001") -> dict:
    """Scrape.do PDP endpoint'i ile yapılandırılmış JSON döndürür."""
    import requests
    return requests.get(
        f"{API_BASE}/plugin/amazon/pdp",
        params={"token": token, "asin": asin, "geocode": geocode, "zipcode": zipcode},
        timeout=30,
    ).json()


def run_product(args) -> None:
    from db import get_db
    token = _cli_get_token(getattr(args, "token", None))
    urls  = args.url if isinstance(args.url, list) else [args.url]
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if getattr(args, "api", False):
        for url in urls:
            asin = _cli_asin_from_url(url) if "/dp/" in url else url
            print(scrape_product_api(asin, token))
        return

    db = get_db()
    for url in urls:
        data = scrape_product_html(url, token)
        pid  = _cli_upsert_product(
            db, data["ASIN"], url, data["Product Name"],
            data["Image"], data["Rating"], data["Price"], now,
        )
        print(f"✓ DB #{pid} | {data['ASIN']}: {data['Product Name'][:55]}... | {data['Price']} | {data['Rating']}")


# ----------------------------------------------------------------------------- #
#  2) TÜM VARYASYONLAR  (renk / boyut / model)
# ----------------------------------------------------------------------------- #

def _parse_variation_dimensions(soup: BeautifulSoup) -> dict:
    """Sayfadaki tüm varyasyon boyutlarını ve seçeneklerini çıkarır."""
    dimensions = {}
    for row in soup.find_all("div", {"id": re.compile(r"inline-twister-row-.*")}):
        dim_name = row.get("id", "").replace("inline-twister-row-", "").replace("_name", "")
        options  = []
        for option in row.find_all("li", {"data-asin": True}):
            asin = option.get("data-asin")
            if asin and option.get("data-initiallyUnavailable") != "true":
                swatch = option.find("span", {"class": "swatch-title-text-display"})
                img    = option.find("img")
                button = option.find("span", {"class": "a-button-text"})
                option_name = (
                    swatch.text.strip() if swatch
                    else img.get("alt", "").strip() if img and img.get("alt")
                    else button.get_text(strip=True) if button and button.get_text(strip=True) != "Select"
                    else "Unknown"
                )
                options.append({"name": option_name, "asin": asin})
        if options:
            dimensions[dim_name] = options
    return dimensions


class VariationScraper:
    """Çok boyutlu ürünlerin (iPhone gibi) tüm varyasyonlarını derinlik-öncelikli gezer ve DB'ye yazar."""

    def __init__(self, product_url: str, token: str):
        self.product_url = product_url
        self.token       = token
        self.scraped: set  = set()
        self.dim_names: list = []
        self._db    = None
        self._now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self._count = 0

    def run(self) -> None:
        from db import get_db
        self._db   = get_db()
        soup       = BeautifulSoup(_cli_fetch_html(self.product_url, self.token), "lxml")
        dimensions = _parse_variation_dimensions(soup)

        self.dim_names = sorted(
            dimensions.keys(),
            key=lambda x: _VARIATION_PRIORITY.index(x.lower()) if x.lower() in _VARIATION_PRIORITY else len(_VARIATION_PRIORITY),
        )
        print(f"Bulunan boyutlar: {list(dimensions.keys())}")
        for dn, opts in dimensions.items():
            print(f"  {dn}: {len(opts)} seçenek")
        print(f"\nGezme sırası: {self.dim_names}\nVaryasyon taraması başlıyor...\n")

        self._scrape(_cli_asin_from_url(self.product_url))
        print(f"\nToplam {self._count} benzersiz varyasyon DB'ye kaydedildi.")

    def _scrape(self, asin: str, dim_index: int = 0, prefix: str = "") -> None:
        if asin in self.scraped:
            return

        url       = f"{self.product_url.split('/dp/')[0]}/dp/{asin}/?th=1&psc=1"
        soup      = BeautifulSoup(_cli_fetch_html(url, self.token), "lxml")
        name_el   = soup.find(id="productTitle")
        name      = name_el.text.strip() if name_el else "N/A"
        price     = _cli_extract_price(soup)
        page_dims = _parse_variation_dimensions(soup)

        if dim_index >= len(self.dim_names) or not page_dims:
            self.scraped.add(asin)
            selections = self._read_selections(soup, asin)
            img_el = soup.find("img", {"id": "landingImage"})
            image  = img_el["src"] if img_el and img_el.has_attr("src") else "N/A"
            pid    = _cli_upsert_product(self._db, asin, url, name, image, "N/A", price, self._now)
            self._count += 1
            sel_str = ", ".join(f"{k}:{v}" for k, v in selections.items())
            print(f"{prefix}✓ DB #{pid} | {asin}: {price} | {sel_str}")
            return

        current_dim = self.dim_names[dim_index]
        if current_dim in page_dims:
            options = page_dims[current_dim]
            if dim_index == 0:
                print(f"{prefix}{len(options)} adet {current_dim} seçeneği bulundu")
            for i, option in enumerate(options):
                if dim_index == 0:
                    print(f"{prefix}{current_dim} {i+1}/{len(options)}: {option['name']}")
                self._scrape(option["asin"], dim_index + 1, prefix + "  ")
        else:
            self._scrape(asin, dim_index + 1, prefix)

    def _read_selections(self, soup: BeautifulSoup, asin: str) -> dict:
        selections = {}
        for dim_name in self.dim_names:
            for row_id in [f"inline-twister-row-{dim_name}_name", f"inline-twister-row-{dim_name}"]:
                row = soup.find("div", {"id": row_id})
                if not row:
                    continue
                selected = row.find("span", {"class": re.compile(r".*a-button-selected.*")})
                if selected:
                    swatch = selected.find("span", {"class": "swatch-title-text-display"})
                    img    = selected.find("img")
                    selections[dim_name] = (
                        swatch.text.strip() if swatch
                        else img.get("alt", "").strip() if img and img.get("alt")
                        else selected.get_text(strip=True) if "Select" not in selected.get_text(strip=True)
                        else "N/A"
                    )
                else:
                    for option in row.find_all("li", {"data-asin": asin}):
                        swatch = option.find("span", {"class": "swatch-title-text-display"})
                        button = option.find("span", {"class": "a-button-text"})
                        selections[dim_name] = (
                            swatch.text.strip() if swatch
                            else button.get_text(strip=True) if button and "Select" not in button.get_text(strip=True)
                            else "N/A"
                        )
                        break
                break
        return selections


def run_variations(args) -> None:
    token = _cli_get_token(getattr(args, "token", None))
    VariationScraper(args.url, token).run()


# ----------------------------------------------------------------------------- #
#  3) SATICI TEKLİFLERİ
# ----------------------------------------------------------------------------- #

def scrape_sellers_html(asin: str, token: str) -> list:
    """Amazon aodAjaxMain endpoint'inden tüm satıcı tekliflerini parse eder."""
    target_url = f"https://www.amazon.com/gp/product/ajax/aodAjaxMain/?asin={asin}"
    soup = BeautifulSoup(_cli_fetch_raw(target_url, token), "lxml")
    offers = []

    for sold_by in soup.find_all("div", id="aod-offer-soldBy"):
        container = sold_by.parent
        while container and not container.find("span", class_="a-price-whole"):
            container = container.parent
        if not container:
            continue

        whole = container.find("span", class_="a-price-whole")
        frac  = container.find("span", class_="a-price-fraction")
        price = f"${whole.text.strip('.')}.{frac.text.strip()}" if whole else "N/A"

        seller_link = sold_by.find("a", href=lambda x: x and "/gp/aag/main" in x)
        seller      = seller_link.text.strip() if seller_link else "Amazon.com"

        rating_div  = sold_by.find("div", id="aod-offer-seller-rating")
        rating_text = _cli_soup_text(rating_div.find("span", class_="a-icon-alt")) if rating_div else ""
        count_text  = _cli_soup_text(
            rating_div.find("span", id=lambda x: x and "seller-rating-count" in str(x))
        ) if rating_div else ""

        seller_rating = _cli_find_match(r"(\d+\.?\d*) out of 5", rating_text)
        rating_count  = _cli_find_match(r"\((\d[\d,]*)\s*ratings\)", count_text)
        positive_pct  = _cli_find_match(r"(\d+)%\s*positive", count_text)
        if positive_pct != "N/A":
            positive_pct += "%"

        ships_div  = container.find("div", id="aod-offer-shipsFrom")
        ships_from = _cli_soup_text(ships_div.find("span", class_="a-color-base")) if ships_div else "N/A"
        condition  = _cli_soup_text(container.find("div", id="aod-offer-heading"), "New")

        offers.append({
            "asin": asin, "price": price, "condition": condition, "seller": seller,
            "seller_rating": seller_rating, "rating_count": rating_count,
            "positive_pct": positive_pct, "ships_from": ships_from,
        })
    return offers


def scrape_sellers_api(asin: str, token: str, geocode: str = "us", zipcode: str = "10001") -> dict:
    """Scrape.do offer-listing endpoint'i ile yapılandırılmış JSON döndürür."""
    import requests
    return requests.get(
        f"{API_BASE}/plugin/amazon/offer-listing",
        params={"token": token, "asin": asin, "geocode": geocode, "zipcode": zipcode},
        timeout=30,
    ).json()


def run_sellers(args) -> None:
    from db import get_db
    token = _cli_get_token(getattr(args, "token", None))
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if getattr(args, "api", False):
        print(scrape_sellers_api(args.asin, token))
        return

    offers = scrape_sellers_html(args.asin, token)
    print(f"{args.asin} için {len(offers)} satıcı teklifi bulundu")
    if not offers:
        return

    db = get_db()
    for offer in offers:
        db.execute(
            "INSERT INTO seller_offers"
            "(asin,price,condition,seller,seller_rating,rating_count,positive_pct,ships_from,scraped_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (offer["asin"], offer["price"], offer["condition"], offer["seller"],
             offer["seller_rating"], offer["rating_count"], offer["positive_pct"],
             offer["ships_from"], now),
        )
    db.commit()
    print(f"✓ {len(offers)} teklif seller_offers tablosuna kaydedildi (ASIN={args.asin})")
