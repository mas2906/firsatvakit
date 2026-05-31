#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform bazlı fingerprint ve anti-bot yönetimi.

Dışa açılan API:
  PROFILES                                  — per-platform fingerprint profil listesi
  get_profile(platform) -> dict             — rastgele profil seç
  make_curl_session(platform, proxy=None)   — (CurlSession, profile) döner
  validate_abck(cookie_value) -> bool       — HepsiB/Akamai _abck geçerliliği
  make_pw_context(browser, platform, proxy) — Playwright context (yeniden kullanılabilir)
  apply_stealth(page)                       — playwright-stealth sayfa bazlı uygula
  content_ok(html, platform) -> bool        — geçerli içerik mi, blok mu?
  is_not_found(html, platform) -> bool      — 404 / ürün yok sayfası mı?
  FetchResult                               — fetch sonucu dataclass
  LayeredFetcher                            — httpx → curl_cffi → Playwright karar akışı

Bağımlılıklar (zaten yüklü):
  curl-cffi>=0.6   playwright   playwright-stealth
  # opsiyonel: browserforge  (PROFILES otomatik güncel tutmak için)
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("fingerprint")

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CurlSession = None
    CURL_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════
# PROFILES
#
# Her platform için fingerprint profil listesi.
#   impersonate — curl_cffi TLS fingerprint kimliği
#   ua          — impersonate ile BİREBİR eşleşen User-Agent
#   headers     — tam header seti (Sec-Ch-Ua, Sec-Fetch-*, Accept-Language tr-TR)
#
# KURAL: impersonate sürümü == UA sürümü.
#   chrome136 + "Chrome/136" UA  ✔
#   chrome136 + "Chrome/131" UA  ✗  → JA3/JA4 uyumsuzluğu → bot tespiti
#
# Safari profilleri: Sec-Ch-Ua gönderilmez (Safari bu header'ı bilmez).
# ═══════════════════════════════════════════════════════════════════

PROFILES: dict[str, list[dict]] = {

    # ── Amazon TR ────────────────────────────────────────────────────
    # iOS Safari → 403. Sadece Android Chrome veya desktop Chrome kullan.
    "amazon": [
        {
            "impersonate": "chrome136",
            "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Cache-Control":             "no-cache",
                "Pragma":                    "no-cache",
                "Referer":                   "https://www.amazon.com.tr/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="136", "Chromium";v="136", "Not/A)Brand";v="99"',
                "Sec-Ch-Ua-Mobile":          "?1",
                "Sec-Ch-Ua-Platform":        '"Android"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
        {
            "impersonate": "chrome131",
            "ua": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Cache-Control":             "no-cache",
                "Pragma":                    "no-cache",
                "Referer":                   "https://www.amazon.com.tr/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="131", "Chromium";v="131", "Not/A)Brand";v="24"',
                "Sec-Ch-Ua-Mobile":          "?1",
                "Sec-Ch-Ua-Platform":        '"Android"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
        {
            "impersonate": "chrome124",
            "ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br",
                "Cache-Control":             "no-cache",
                "Referer":                   "https://www.amazon.com.tr/",
                "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile":          "?1",
                "Sec-Ch-Ua-Platform":        '"Android"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
        {
            "impersonate": "chrome136",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Cache-Control":             "max-age=0",
                "Dnt":                       "1",
                "Priority":                  "u=0, i",
                "Referer":                   "https://www.amazon.com.tr/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="136", "Chromium";v="136", "Not/A)Brand";v="99"',
                "Sec-Ch-Ua-Mobile":          "?0",
                "Sec-Ch-Ua-Platform":        '"Windows"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Te":                        "trailers",
                "Upgrade-Insecure-Requests": "1",
            },
        },
    ],

    # ── Trendyol ─────────────────────────────────────────────────────
    # Cloudflare + JS challenge. iOS Safari TLS = birincil bypass.
    # Fallback: Chrome desktop (farklı JA3 fingerprint).
    "trendyol": [
        {
            "impersonate": "safari18_0",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control":   "no-cache",
                "Pragma":          "no-cache",
                "Referer":         "https://www.trendyol.com/",
                # Sec-Ch-Ua yok — Safari göndermez
            },
        },
        {
            "impersonate": "safari17_5",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control":   "no-cache",
                "Referer":         "https://www.trendyol.com/",
            },
        },
        {
            "impersonate": "safari17_0",
            "ua": "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer":         "https://www.trendyol.com/",
            },
        },
        {
            "impersonate": "chrome136",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Cache-Control":             "max-age=0",
                "Referer":                   "https://www.trendyol.com/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="136", "Chromium";v="136", "Not/A)Brand";v="99"',
                "Sec-Ch-Ua-Mobile":          "?0",
                "Sec-Ch-Ua-Platform":        '"Windows"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
    ],

    # ── HepsiB ───────────────────────────────────────────────────────
    # Akamai Bot Manager → _abck cookie + JS sensor.
    # iOS Safari TLS fingerprint ile bypass → geçersiz _abck → Playwright fallback.
    "hepsiburada": [
        {
            "impersonate": "safari18_0",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control":   "no-cache",
                "Pragma":          "no-cache",
                "Referer":         "https://www.hepsiburada.com/",
            },
        },
        {
            "impersonate": "safari17_5",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control":   "no-cache",
                "Referer":         "https://www.hepsiburada.com/",
            },
        },
        {
            "impersonate": "safari17_0",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer":         "https://www.hepsiburada.com/",
            },
        },
        {
            "impersonate": "safari16",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
            "headers": {
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer":         "https://www.hepsiburada.com/",
            },
        },
    ],

    # ── N11 ──────────────────────────────────────────────────────────
    # Birincil katman GraphQL API → fingerprint kritik değil.
    # HTML fallback için standart Chrome desktop.
    "n11": [
        {
            "impersonate": "chrome136",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Cache-Control":             "max-age=0",
                "Referer":                   "https://www.n11.com/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="136", "Chromium";v="136", "Not/A)Brand";v="99"',
                "Sec-Ch-Ua-Mobile":          "?0",
                "Sec-Ch-Ua-Platform":        '"Windows"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
        {
            "impersonate": "chrome131",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "headers": {
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language":           "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding":           "gzip, deflate, br, zstd",
                "Referer":                   "https://www.n11.com/",
                "Sec-Ch-Ua":                 '"Google Chrome";v="131", "Chromium";v="131", "Not/A)Brand";v="24"',
                "Sec-Ch-Ua-Mobile":          "?0",
                "Sec-Ch-Ua-Platform":        '"Windows"',
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Sec-Fetch-User":            "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        },
    ],
}


# ═══════════════════════════════════════════════════════════════════
# TEMEL YARDIMCILAR
# ═══════════════════════════════════════════════════════════════════

def get_profile(platform: str) -> dict:
    """Platform için rastgele bir fingerprint profili döndür."""
    profiles = PROFILES.get(platform) or PROFILES["trendyol"]
    return random.choice(profiles)


def make_curl_session(
    platform: str,
    proxy: Optional[str] = None,
    timeout: float = 20.0,
) -> tuple:
    """
    Platform için curl_cffi AsyncSession oluştur.

    Returns: (CurlSession, profile_dict)
      session — doğrudan `await session.get(url, headers=...)` şeklinde kullan
      profile — {"impersonate", "ua", "headers"} — User-Agent ve header set için

    Dönen session yeniden kullanılabilir; pool yönetimi caller'a ait.

    Örnek:
        session, profile = make_curl_session("amazon", timeout=25.0)
        headers = {**profile["headers"], "User-Agent": profile["ua"]}
        r = await session.get(url, headers=headers, allow_redirects=True)
    """
    if not CURL_AVAILABLE:
        raise RuntimeError("curl_cffi yüklü değil — pip install curl-cffi")

    profile = get_profile(platform)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = CurlSession(
        impersonate=profile["impersonate"],
        timeout=timeout,
        proxies=proxies,
    )
    return session, profile


def validate_abck(cookie_value: Optional[str]) -> bool:
    """
    HepsiB / Akamai _abck cookie geçerliliğini kontrol et.

    Akamai format: [hash]~[status]~[timestamp]~...
      status = 0    → JS challenge başarılı, geçerli
      status = -1   → bot tespiti / challenge çözülemedi → Playwright fallback

    Returns: True → geçerli, False → geçersiz (Playwright'a git)
    """
    if not cookie_value:
        return False
    return "~-1~" not in cookie_value


async def make_pw_context(browser, platform: str = "default", proxy: Optional[str] = None):
    """
    Platform için Playwright browser context oluştur.

    - tr-TR locale, Europe/Istanbul timezone
    - Platform uyumlu desktop Chrome UA (Playwright'ta mobile UA gereksiz —
      TLS fingerprint zaten Chromium'dur)
    - Platform-spesifik Referer / Accept-Language header
    - STEALTH_SCRIPT init script eklenir

    Context bir kez oluşturulup BrowserPool içinde yeniden kullanılır.
    playwright-stealth sayfa bazlı uygulanmalı → bkz. apply_stealth(page).

    Entegrasyon — cdp_base.py BrowserPool._new_context() içinde:

        from scrapers.fingerprint import make_pw_context
        async def _new_context(self):
            proxy_dict = self._get_proxy_dict()
            proxy_url = proxy_dict["server"] if proxy_dict else None
            return await make_pw_context(self._browser, platform=self.name, proxy=proxy_url)
    """
    from scrapers.utils import STEALTH_SCRIPT

    _w = random.choice([1280, 1366, 1440, 1920])
    _h = random.choice([768, 800, 900, 1080])

    # Playwright için desktop Chrome profili tercih et
    all_profiles = PROFILES.get(platform, PROFILES["n11"])
    desktop = [p for p in all_profiles if "Windows" in p["ua"] or "Macintosh" in p["ua"]]
    profile = random.choice(desktop) if desktop else random.choice(all_profiles)

    # User-Agent hariç platform header'larını extra_http_headers olarak geç
    extra = {k: v for k, v in profile["headers"].items() if k.lower() != "user-agent"}

    kwargs: dict = {
        "viewport":            {"width": _w, "height": _h},
        "user_agent":          profile["ua"],
        "locale":              "tr-TR",
        "timezone_id":         "Europe/Istanbul",
        "java_script_enabled": True,
        "ignore_https_errors": True,
        "extra_http_headers":  extra,
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    ctx = await browser.new_context(**kwargs)
    await ctx.add_init_script(STEALTH_SCRIPT)
    return ctx


async def apply_stealth(page) -> None:
    """
    Playwright sayfasına playwright-stealth uygula.
    context.new_page() sonrası, page.goto() öncesi çağır.

    playwright-stealth yüklü değilse sessizce atlanır.

    Örnek:
        page = await pool.acquire()
        await apply_stealth(page)   # ← ekle
        await page.goto(url, ...)
    """
    try:
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"[fingerprint] playwright-stealth uygulanamadı: {e}")


# ═══════════════════════════════════════════════════════════════════
# BLOCK / NOT-FOUND TESPİT — platform bazlı
# ═══════════════════════════════════════════════════════════════════

_BLOCK_SIGNALS: dict[str, list[str]] = {
    "amazon": [
        "robot check", "validatecaptcha", "type the characters",
        "enter the characters", "not a robot",
        "api-services-support@amazon",
        "something went wrong", "we're sorry",
        "üzgünüz", "isteğinizi işlemeye",
        "sorun üzerinde çalışıyoruz",
    ],
    "trendyol": [
        "cloudflare", "just a moment", "challenge-platform",
        "attention required", "captcha", "ddos-guard",
    ],
    "hepsiburada": [
        "access denied", "bot tespit", "blocked",
        "bu sayfaya erişim engellendi",
        "cloudflare", "tarayıcınızı doğrulayın",
    ],
    "n11": [
        "access denied", "cloudflare", "503 service", "site maintenance",
    ],
}

_NOT_FOUND_SIGNALS: dict[str, list[str]] = {
    "amazon": [
        "sitemizde işlev gösteren bir sayfaya karşılık gelmiyor",
        "aradığınız bir şey mi var", "dogs of amazon",
    ],
    "trendyol": [
        "sayfa bulunamadı", "ürün bulunamadı", "böyle bir sayfa",
    ],
    "hepsiburada": [
        "sayfa bulunamadı", "ürün bulunamadı", "bu ürün artık",
    ],
    "n11": [
        "ürün bulunamadı", "böyle bir ürün", "sayfa bulunamadı",
    ],
}


def content_ok(html: str, platform: str) -> bool:
    """
    HTML geçerli mi, yoksa anti-bot bloğu mu?

    False → blok veya içerik çok küçük → üst katmana fallback yap.
    True  → parse edilebilir içerik.

    Not: 404 kontrolü için ayrıca is_not_found() kullan.
    """
    if not html or len(html) < 1500:
        return False
    sample = html[:8000].lower()
    return not any(s in sample for s in _BLOCK_SIGNALS.get(platform, []))


def is_not_found(html: str, platform: str) -> bool:
    """404 / ürün kaldırılmış sayfası mı?"""
    if not html:
        return False
    sample = html[:5000].lower()
    return any(s in sample for s in _NOT_FOUND_SIGNALS.get(platform, []))


# ═══════════════════════════════════════════════════════════════════
# LAYERED FETCHER — httpx → curl_cffi → Playwright
# ═══════════════════════════════════════════════════════════════════

@dataclass
class FetchResult:
    """fetch() dönüş değeri."""
    html:      Optional[str] = None
    layer:     str            = ""      # "httpx" | "curl" | "playwright"
    blocked:   bool           = False
    not_found: bool           = False
    dead_url:  bool           = False

    @property
    def ok(self) -> bool:
        return bool(self.html)


class LayeredFetcher:
    """
    Platform bazlı 3-katmanlı HTTP fetcher.
    httpx (hızlı) → curl_cffi (TLS fingerprint) → Playwright (JS rendering)

    Mevcut scraper akışına minimal entegrasyon:

        _fetcher = LayeredFetcher("amazon")   # modül seviyesinde bir kez oluştur

        async def scrape_amazon(url, pool=None):
            result = await _fetcher.fetch(url, pool=pool)
            if result.dead_url:  return {"dead_url": True}
            if result.not_found: return {"not_found": True}
            if result.ok:
                return await asyncio.to_thread(_parse, result.html)
            return None

    Session yönetimi: curl_cffi session instance seviyesinde tutulur.
    Bir instance'ı platform başına tek, uzun ömürlü nesne olarak kullan.
    """

    def __init__(
        self,
        platform:  str,
        proxy_url: Optional[str]   = None,
        timeout:   float           = 20.0,
    ):
        self.platform  = platform
        self._proxy:    Optional[str]         = proxy_url
        self._timeout:  float                 = timeout
        self._session:  Optional[CurlSession] = None
        self._profile:  Optional[dict]        = None

    # ── iç yardımcılar ──────────────────────────────────────────────

    def _get_session(self) -> tuple:
        if self._session is None:
            self._session, self._profile = make_curl_session(
                self.platform, proxy=self._proxy, timeout=self._timeout
            )
        return self._session, self._profile

    def set_proxy(self, proxy_url: Optional[str]) -> None:
        """Proxy değişince session yeniden oluşturulur."""
        if proxy_url != self._proxy:
            self._proxy = proxy_url
            self.reset()

    def reset(self) -> None:
        """Mevcut curl session'ı iptal et — bir sonraki istekte yenisi açılır."""
        self._session = None
        self._profile = None

    @staticmethod
    def _is_dead_url(exc: str) -> bool:
        return any(k in exc for k in (
            "ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED",
            "Name or service not known", "getaddrinfo failed",
        ))

    # ── Katman 0: httpx ─────────────────────────────────────────────

    async def _httpx(self, url: str) -> FetchResult:
        import httpx
        try:
            profile = get_profile(self.platform)
            headers = {**profile["headers"], "User-Agent": profile["ua"]}
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=True,
                proxy=self._proxy,
            ) as c:
                r = await c.get(url, headers=headers)
            if r.status_code in (404, 410):
                return FetchResult(not_found=True, layer="httpx")
            if r.status_code != 200:
                return FetchResult(layer="httpx")
            html = r.text
            if is_not_found(html, self.platform):
                return FetchResult(not_found=True, layer="httpx")
            if not content_ok(html, self.platform):
                return FetchResult(blocked=True, layer="httpx")
            return FetchResult(html=html, layer="httpx")
        except Exception as e:
            if self._is_dead_url(str(e)):
                return FetchResult(dead_url=True, layer="httpx")
            log.debug(f"[{self.platform}/httpx] {e}")
            return FetchResult(layer="httpx")

    # ── Katman 1: curl_cffi ──────────────────────────────────────────

    async def _curl(self, url: str) -> FetchResult:
        if not CURL_AVAILABLE:
            return FetchResult(layer="curl")
        try:
            session, profile = self._get_session()
            headers = {**profile["headers"], "User-Agent": profile["ua"]}
            r = await session.get(url, headers=headers, allow_redirects=True, timeout=15)

            if r.status_code in (404, 410):
                return FetchResult(not_found=True, layer="curl")
            if r.status_code in (403, 429, 503):
                self.reset()
                return FetchResult(blocked=True, layer="curl")
            if r.status_code != 200:
                self.reset()
                return FetchResult(layer="curl")

            html = r.text
            if is_not_found(html, self.platform):
                return FetchResult(not_found=True, layer="curl")
            if not content_ok(html, self.platform):
                self.reset()
                return FetchResult(blocked=True, layer="curl")

            # HepsiB: Akamai _abck cookie'si geçersizse Playwright'a git
            if self.platform == "hepsiburada" and hasattr(r, "cookies"):
                abck = r.cookies.get("_abck")
                if abck and not validate_abck(abck):
                    log.info("[hepsiburada] _abck ~-1~ → Playwright fallback")
                    return FetchResult(blocked=True, layer="curl")

            return FetchResult(html=html, layer="curl")
        except Exception as e:
            if self._is_dead_url(str(e)):
                return FetchResult(dead_url=True, layer="curl")
            log.debug(f"[{self.platform}/curl] {e}")
            self.reset()
            return FetchResult(layer="curl")

    # ── Katman 2: Playwright ─────────────────────────────────────────

    async def _playwright(self, url: str, pool=None) -> FetchResult:
        if pool is None:
            log.debug(f"[{self.platform}/playwright] pool yok — atlanıyor")
            return FetchResult(layer="playwright")

        from scrapers.cdp_base import setup_resource_blocking
        page = await pool.acquire()
        try:
            await setup_resource_blocking(page)
            await apply_stealth(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if page.is_closed():
                return FetchResult(layer="playwright")

            await asyncio.sleep(random.uniform(0.8, 2.0))
            if not page.is_closed():
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(0.3, 0.7))
            if page.is_closed():
                return FetchResult(layer="playwright")

            html = await page.content()
            if not html:
                return FetchResult(layer="playwright")
            if is_not_found(html, self.platform):
                return FetchResult(not_found=True, layer="playwright")
            if not content_ok(html, self.platform):
                return FetchResult(blocked=True, layer="playwright")
            return FetchResult(html=html, layer="playwright")
        except Exception as e:
            if self._is_dead_url(str(e)):
                return FetchResult(dead_url=True, layer="playwright")
            log.error(f"[{self.platform}/playwright] {e}")
            return FetchResult(layer="playwright")
        finally:
            await pool.release(page)

    # ── Ana akış ─────────────────────────────────────────────────────

    async def fetch(
        self,
        url:        str,
        pool=None,
        skip_httpx: bool = False,
        skip_curl:  bool = False,
    ) -> FetchResult:
        """
        3-katmanlı fetch — hızlıdan yavaşa.

        Args:
            url         — hedef URL
            pool        — BrowserPool instance (Playwright katmanı için)
            skip_httpx  — httpx katmanını atla (token/cookie gerektiren endpointler)
            skip_curl   — curl_cffi katmanını atla (nadiren kullanılır)

        FetchResult alanları:
            .ok         → True ise html geçerli ve parse edilebilir
            .dead_url   → DNS çözülemedi, ürün URL'si ölü
            .not_found  → 404 / ürün kaldırılmış sayfası
            .blocked    → tüm katmanlar engellendi
            .layer      → hangi katman başarılı oldu
        """
        if not skip_httpx:
            r = await self._httpx(url)
            if r.ok or r.dead_url or r.not_found:
                if r.ok:
                    log.debug(f"[{self.platform}] httpx ✔ {len(r.html)} byte")
                return r
            if r.blocked:
                log.info(f"[{self.platform}/httpx] block → curl")

        if not skip_curl:
            r = await self._curl(url)
            if r.ok or r.dead_url or r.not_found:
                if r.ok:
                    log.debug(f"[{self.platform}] curl ✔ {len(r.html)} byte")
                return r
            if r.blocked:
                log.info(f"[{self.platform}/curl] block → Playwright")

        r = await self._playwright(url, pool=pool)
        if r.ok:
            log.debug(f"[{self.platform}] playwright ✔ {len(r.html)} byte")
        elif not r.dead_url and not r.not_found:
            log.warning(f"[{self.platform}] tüm katmanlar başarısız: {url[:70]}")
        return r
