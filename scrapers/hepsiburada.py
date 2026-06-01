#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hepsiburada scraper — v3  (amazon.py v8 ile aynı iyileştirmeler)

Primary : iOS Safari TLS impersonation (curl_cffi) — 3 session pool, exp backoff
Fallback: Playwright + stealth
"""

import re
import json
import asyncio
import random
import logging
from typing import Optional
from bs4 import BeautifulSoup

from scrapers.utils import STEALTH_SCRIPT, parse_price_tr_clean, RateLimiter

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _CURL_OK = True
except ImportError:
    CurlSession = None
    _CURL_OK = False

log = logging.getLogger("hb_scraper")

# ── Rate limiter ─────────────────────────────────────────────────
# Hepsiburada: Playwright ağırlıklı, JS bot tespiti var
# Beklenen: ~12 istek/dk → 2717 ürün → ~6 tur/gün
_limiter      = RateLimiter(min_delay=0.6, max_delay=1.2)   # iOS curl_cffi — proxy
_limiter_fast = RateLimiter(min_delay=0.3, max_delay=0.6)   # price_only

# ── iOS Safari UA pool ───────────────────────────────────────────
_IOS_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
]

# ── Android Chrome UA pool ───────────────────────────────────────
_ANDROID_UAS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

_ALL_MOBILE_UAS = _IOS_UAS + _ANDROID_UAS

_IOS_IMPERS = ["safari18_0"]

# ── iOS Safari header seti ───────────────────────────────────────
_MOBILE_IOS_HEADERS: dict = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
    "Referer":         "https://www.hepsiburada.com/",
}

# ── Android Chrome ek headerları ────────────────────────────────
_ANDROID_EXTRA: dict = {
    "Sec-Ch-Ua-Mobile":  "?1",
    "Sec-Ch-Ua-Platform": '"Android"',
    "Sec-Fetch-Dest":    "document",
    "Sec-Fetch-Mode":    "navigate",
    "Sec-Fetch-Site":    "none",
    "Sec-Fetch-User":    "?1",
}

# Kaynak engelle (Playwright fallback)
_BLOCKED_TYPES = {"image", "stylesheet", "font", "media"}
_BLOCKED_EXTS  = re.compile(r'\.(png|jpg|jpeg|gif|svg|webp|ico|woff2?|ttf|eot|mp4|pdf)(\?|$)', re.I)

# ── Session pool (3 paralel iOS session) ────────────────────────
_POOL_SIZE          = 3
_SESSIONS: list     = []
_SESSIONS_LOCK      = asyncio.Lock()
_session_idx        = 0

# ── Exponential backoff circuit breaker ─────────────────────────
_fail_streak        = 0
_disabled_until     = 0.0
_cooldown_mult      = 1
_MAX_FAIL           = 5
_COOLDOWN_BASE      = 60   # 60s → 120s → 240s → max 300s

import time as _time

def _curl_enabled() -> bool:
    return _CURL_OK and _time.time() > _disabled_until

def _on_fail() -> None:
    global _fail_streak, _disabled_until, _cooldown_mult
    _fail_streak += 1
    if _fail_streak >= _MAX_FAIL:
        cooldown = min(_COOLDOWN_BASE * _cooldown_mult, 300)
        _disabled_until = _time.time() + cooldown
        _cooldown_mult  = min(_cooldown_mult * 2, 5)
        _fail_streak    = 0
        log.warning(f"[HB/curl] {_MAX_FAIL} ardışık hata → {cooldown}s askı (çarpan={_cooldown_mult})")

def _on_success() -> None:
    global _fail_streak, _cooldown_mult
    _fail_streak  = 0
    _cooldown_mult = 1

_HB_SEM = None
def _get_sem():
    global _HB_SEM
    if _HB_SEM is None:
        _HB_SEM = asyncio.Semaphore(3)
    return _HB_SEM


async def _get_session() -> tuple:
    """3 session pool — proxy destekli, amazon.py ile aynı pattern."""
    global _SESSIONS, _session_idx
    new_entry = None
    async with _SESSIONS_LOCK:
        if len(_SESSIONS) < _POOL_SIZE:
            try:
                imp = random.choice(_IOS_IMPERS)
                from scrapers.proxy_pool import get_proxy_pool
                _pp = get_proxy_pool()
                proxy = await _pp.get() if _pp.has_proxies else None
                proxies_dict = _pp.curl_dict(proxy) if proxy else None
                s = CurlSession(impersonate=imp, timeout=20,
                                proxies=proxies_dict if proxies_dict else None)
                _SESSIONS.append((s, imp, proxy))
                new_entry = (s, imp, proxy)
                log.info(f"[HB/curl] Yeni session #{len(_SESSIONS)}: {imp} proxy={'✔' if proxy else '✘'}")
            except Exception as e:
                log.warning(f"[HB/curl] Session oluşturulamadı: {e}")
                if not _SESSIONS:
                    raise
        if not _SESSIONS:
            raise RuntimeError("HB session pool boş")
        _session_idx = (_session_idx + 1) % len(_SESSIONS)
        result = _SESSIONS[_session_idx]

    # Warm-up — lock dışında
    if new_entry:
        s, imp, proxy = new_entry
        try:
            ua = random.choice(_IOS_UAS)
            warmup_hdrs = {**_MOBILE_IOS_HEADERS, "User-Agent": ua}
            r = await s.get("https://www.hepsiburada.com/", headers=warmup_hdrs, timeout=10, stream=True)
            await r.aclose()
        except Exception:
            pass
    return result


def _reset_session() -> None:
    global _SESSIONS, _session_idx
    if not _SESSIONS:
        return
    bad = _session_idx % len(_SESSIONS)
    _SESSIONS[bad] = None  # type: ignore
    _SESSIONS = [s for s in _SESSIONS if s is not None]
    log.info(f"[HB/curl] Session #{bad} sıfırlandı, kalan={len(_SESSIONS)}")


# ==========================================
# ENTRY POINT
# ==========================================
async def scrape_hepsiburada(url: str, pool=None,
                              price_only: bool = False,
                              cached_image: str = None) -> Optional[dict]:
    async with _get_sem():
        result = await _scrape(url, pool, price_only=price_only)

    if not result:
        return None
    if result.get("dead_url"):
        return result
    if price_only:
        out = {k: result[k] for k in ("price", "stock", "cart_discount", "coupon") if k in result}
        if cached_image:
            out["image_url"] = cached_image
        return out
    return result


# ==========================================
# CORE — iOS Safari önce, Playwright fallback
# ==========================================
async def _scrape(url: str, pool, price_only: bool = False) -> Optional[dict]:
    await (_limiter_fast if price_only else _limiter).wait()

    # Primary: iOS Safari curl_cffi
    if _curl_enabled():
        result = await _via_ios_safari(url)
        if result and result.get("dead_url"):
            return result
        if result and result.get("price"):
            _on_success()
            return result
        _on_fail()
        log.info(f"[HB] curl başarısız ({_fail_streak}/{_MAX_FAIL}) → Playwright deneniyor")
    else:
        remain = max(0, _disabled_until - _time.time())
        log.info(f"[HB/curl] circuit breaker aktif — {remain:.0f}s kaldı")

    # Fallback: Playwright + stealth + kaynak engelleme
    if pool is not None:
        result = await _via_playwright(url, pool)
        if result and result.get("price"):
            _on_success()
        return result

    log.warning("[HB] pool yok, curl başarısız — atlanıyor")
    return None


# ==========================================
# PRIMARY — iOS Safari TLS impersonation
# ==========================================
async def _via_ios_safari(url: str) -> Optional[dict]:
    try:
        session, imp, proxy = await _get_session()
        ua    = random.choice(_ALL_MOBILE_UAS)
        is_ios = "iPhone" in ua or "iPad" in ua

        headers = {**_MOBILE_IOS_HEADERS, "User-Agent": ua}
        if not is_ios:
            v  = re.search(r"Chrome/(\d+)", ua)
            cv = v.group(1) if v else "136"
            headers.update({
                **_ANDROID_EXTRA,
                "sec-ch-ua": f'"Google Chrome";v="{cv}", "Chromium";v="{cv}", "Not/A)Brand";v="8"',
            })

        r = await session.get(url, headers=headers, allow_redirects=True, timeout=20)

        if r.status_code in (404, 410):
            return {"dead_url": True}
        if r.status_code in (403, 429, 503):
            log.warning(f"[HB/curl] HTTP {r.status_code} — blok, session sıfırlanıyor")
            _reset_session()
            return None
        if r.status_code != 200:
            log.debug(f"[HB/curl] HTTP {r.status_code}")
            _reset_session()
            return None

        html = r.text
        if not html or len(html) < 3000:
            log.warning(f"[HB/curl] Kısa yanıt ({len(html or '')}B) — blok")
            _reset_session()
            return None

        # Redirect tespiti: ürün sayfasından başka bir yere gidildiyse dead/blok
        final_url = str(r.url) if hasattr(r, "url") else url
        if final_url and final_url.rstrip("/") != url.rstrip("/"):
            # Ana sayfa, arama veya kategori sayfasına yönlendirildiyse ürün yok
            fu_lower = final_url.lower()
            if not any(p in fu_lower for p in ("/pm-", "/p/hb", "-pm-", "/p/hbc")):
                log.info(f"[HB/curl] Ürün dışı redirect: {final_url[:80]} — dead_url")
                return {"dead_url": True}

        # Büyük sayfalar (>500KB) genellikle ana sayfa / kategori redirect'i
        if len(html) > 500_000:
            log.warning(f"[HB/curl] Yanıt çok büyük ({len(html)//1024}KB) — redirect/blok")
            _reset_session()
            return None

        # Blok/challenge tespiti — sadece kesin blok sinyalleri
        low = html[:5000].lower()
        if any(k in low for k in ("captcha", "cf-challenge", "just a moment", "access denied")):
            log.info(f"[HB/curl] Challenge sayfası (imp={imp}) — session sıfırlanıyor")
            _reset_session()
            return None

        # Temel ürün göstergesi yok — boş/hatalı sayfa (yeni format dahil)
        has_product_data = (
            "productstate" in html.lower()
            or "__next_data__" in html.lower()
            or "data-test-id" in html.lower()
        )
        if not has_product_data:
            log.info(f"[HB/curl] Ürün verisi bulunamadı — blok?")
            _reset_session()
            return None

        result = _parse_html(html)
        if result and result.get("price"):
            log.info(f"[HB/curl] ✔ imp={imp} ua={'iOS' if is_ios else 'Android'} | "
                     f"{(result.get('title') or '')[:50]} | {result.get('price')} ₺")
            if proxy:
                from scrapers.proxy_pool import get_proxy_pool
                await get_proxy_pool().mark_ok(proxy)
        return result

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.debug(f"[HB/curl] Hata: {e}")
        if proxy:
            from scrapers.proxy_pool import get_proxy_pool
            await get_proxy_pool().mark_failed(proxy)
        _reset_session()
        return None


# ==========================================
# FALLBACK — Playwright + stealth
# ==========================================
async def _via_playwright(url: str, pool) -> Optional[dict]:
    page = await pool.acquire()

    async def _route(route):
        req = route.request
        if req.resource_type in _BLOCKED_TYPES or _BLOCKED_EXTS.search(req.url):
            await route.abort()
        else:
            await route.continue_()

    try:
        await page.add_init_script(STEALTH_SCRIPT)
        await page.route("**/*", _route)

        log.info(f"[HB/pw] → {url[:80]}")
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)

        try:
            await page.wait_for_selector(
                '[data-test-id="default-price"],[data-test-id="checkout-price"]',
                timeout=7000
            )
        except Exception:
            await page.wait_for_timeout(2000)

        html = await page.content()

        # Redirect / blok tespiti — Playwright için
        pw_url = page.url
        if pw_url and pw_url.rstrip("/") != url.rstrip("/"):
            fu_lower = pw_url.lower()
            if not any(p in fu_lower for p in ("/pm-", "/p/hb", "-pm-", "/p/hbc")):
                log.info(f"[HB/pw] Ürün dışı redirect: {pw_url[:80]}")
                return {"dead_url": True}

        if len(html) > 500_000:
            log.warning(f"[HB/pw] Sayfa çok büyük ({len(html)//1024}KB) — redirect/blok")
            return None

        low8 = html[:5000].lower()
        if any(k in low8 for k in ("captcha", "cf-challenge", "just a moment", "access denied")):
            log.warning(f"[HB/pw] Challenge sayfası tespit edildi")
            return None

        data = _parse_html(html)

        if not data or not data.get("price"):
            js_p = await _js_price(page)
            if js_p:
                data = data or {}
                data["price"] = js_p

        if not data or not data.get("price"):
            dom_p = await _dom_price(page)
            if dom_p:
                data = data or {}
                data["price"] = dom_p

        if data and not data.get("title"):
            try:
                el = await page.query_selector('meta[property="og:title"]')
                if el:
                    data["title"] = await el.get_attribute("content")
            except Exception:
                pass

        if data and data.get("price"):
            log.info(f"[HB/pw] ✔ {(data.get('title') or '')[:50]} | {data.get('price')} ₺")
        else:
            log.warning(f"[HB/pw] ✘ fiyat yok — {url[:60]}")
        return data

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.error(f"[HB/pw] Hata: {e}")
        return None
    finally:
        try:
            await page.unroute("**/*", _route)
        except Exception:
            pass
        await pool.release(page)


# ==========================================
# HTML PARSE — 5 katmanlı, data-test-id öncelikli
# ==========================================
def _parse_html(html: str) -> Optional[dict]:
    title = price = image = None
    rating = review_count = None
    stock = "Bilinmiyor"
    cart_discount = False
    coupon = variants = None
    ps = None

    # ── Layer 1: productState — eski (window.productState=) ve yeni ({accountState:...,productState:...}) format ──
    def _extract_json_at(html: str, start: int) -> dict | None:
        """start pozisyonundaki '{' ile başlayan JSON nesnesini çıkar."""
        depth = in_str = esc = 0
        end = start
        for i, ch in enumerate(html[start:], start):
            if esc:              esc = False; continue
            if ch == '\\' and in_str: esc = True; continue
            if ch == '"':        in_str = not in_str; continue
            if in_str:           continue
            if ch == '{':        depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:   end = i + 1; break
        try:
            return json.loads(html[start:end])
        except Exception:
            return None

    # Format A: window.productState = {...}
    m = re.search(r'window\.productState\s*=\s*(\{)', html)
    if m:
        obj = _extract_json_at(html, m.start(1))
        if obj:
            ps = obj

    # Format B: <script>{"accountState":...,"productState":{...}}</script>  (yeni HB frontend)
    if not ps:
        m2 = re.search(r'<script[^>]*>\s*(\{"accountState"\s*:)', html)
        if m2:
            brace = html.find('{', m2.start())
            obj = _extract_json_at(html, brace)
            if obj and "productState" in obj:
                ps = obj.get("productState")

    if ps:
        try:
            product  = ps.get("product", {})
            title    = product.get("name")
            price    = _price_from_state(ps)
            stock    = _stock_from_state(ps)
            image    = _image_from_state(ps)
            coupon   = _coupon_from_state(ps)
            variants = _variants_from_state(ps)
        except Exception:
            pass

    # ── Layer 2: __NEXT_DATA__ (genişletilmiş) ───────────────────
    if not title or not price:
        m2 = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if m2:
            try:
                j = json.loads(m2.group(1))
                nd = _parse_next_data_full(j)
                if nd:
                    if not title and nd.get("title"):        title = nd["title"]
                    if not price and nd.get("price"):        price = nd["price"]
                    if stock == "Bilinmiyor" and nd.get("stock"): stock = nd["stock"]
                    if not image and nd.get("image"):        image = nd["image"]
                    if not rating and nd.get("rating"):      rating = nd["rating"]
                    if not review_count and nd.get("review_count"): review_count = nd["review_count"]
            except Exception:
                pass

    # ── Layer 3: BeautifulSoup data-test-id seçicileri ──────────
    soup = BeautifulSoup(html, "html.parser")

    # Başlık
    if not title:
        for sel in ['[data-test-id="title"]', '[data-test-id^="title-"]']:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) > 5:
                    title = t; break

    # Fiyat — indirimli (sepete özel) önce, sonra normal fiyat
    if not price:
        for sel in [
            '[data-test-id="price-current-price"]',
            '[data-test-id^="final-price-"]',
        ]:
            el = soup.select_one(sel)
            if el:
                v = parse_price_tr_clean(el.get_text(strip=True))
                if v and v > 0:
                    price = v; cart_discount = True; break
    if not price:
        el = soup.select_one('[data-test-id="default-price"]')
        if el:
            v = parse_price_tr_clean(el.get_text(strip=True))
            if v and v > 0: price = v

    # checkout-price regex (eski sepete özel format)
    if not price:
        m_co = re.search(
            r'data-test-id=["\']checkout-price["\'][^>]*>.*?([\d]{1,3}(?:\.[\d]{3})*,[\d]{2})\s*TL',
            html, re.DOTALL | re.IGNORECASE
        )
        if m_co:
            v = parse_price_tr_clean(m_co.group(1))
            if v and v > 0:
                price = v; cart_discount = True

    # Stok — addToCart butonu varlığı en güvenilir gösterge
    if stock == "Bilinmiyor":
        for sel in ['[data-test-id="addToCart"]', '[data-test-id^="add-to-cart-button-"]']:
            if soup.select_one(sel):
                stock = "Stokta Var"; break

    # Puan / Yorum sayısı
    if not rating or not review_count:
        for sel in ['[data-test-id="has-review"]', '[data-test-id^="rating-"]']:
            el = soup.select_one(sel)
            if not el: continue
            txt = el.get_text(strip=True)
            if not rating:
                mr = re.search(r'([\d]+[.,][\d]+)', txt)
                if mr:
                    try:
                        r = float(mr.group(1).replace(',', '.'))
                        if 0 < r <= 5: rating = r
                    except Exception: pass
            if not review_count:
                mc = re.search(r'\((\d[\d.]*)\)', txt)
                if mc:
                    try: review_count = int(mc.group(1).replace('.', ''))
                    except Exception: pass
            if rating and review_count: break

    # Varyantlar — data-test-id^="variant-box-"
    if not variants:
        var_els = soup.select('[data-test-id^="variant-box-"]')
        if var_els:
            parsed_vars, seen = [], set()
            for el in var_els[:50]:
                name = el.get_text(strip=True)
                if not name or name in seen: continue
                seen.add(name)
                cls_str = " ".join(el.get("class") or [])
                in_stock = "disabled" not in cls_str.lower()
                parsed_vars.append({"name": name[:100], "price": None, "in_stock": in_stock})
            if parsed_vars: variants = parsed_vars

    # ── Layer 4: JSON-LD (başlık, resim fallback) ─────────────────
    if not title or not image:
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                j = json.loads(sc.string or sc.text)
                if isinstance(j, list): j = j[0]
                if isinstance(j, dict):
                    if not title: title = j.get("name")
                    if not image:
                        img = j.get("image")
                        if isinstance(img, list) and img:
                            image = img[0] if isinstance(img[0], str) else img[0].get("url")
                        elif isinstance(img, str): image = img
                    if not price:
                        offers = j.get("offers", {})
                        if isinstance(offers, dict):
                            p = offers.get("price")
                            if p: price = parse_price_tr_clean(str(p))
            except Exception: pass

    # ── Layer 5: og meta fallbacks ────────────────────────────────
    if not image:
        og = soup.find("meta", property="og:image")
        if og: image = og.get("content")
    if not title:
        og = soup.find("meta", property="og:title")
        if og: title = og.get("content")

    if not price or not title:
        return None
    if "güvenlik" in (title or "").lower() or "hata" in (title or "").lower():
        return None

    return {
        "title": title, "price": price, "image_url": image,
        "stock": stock, "cart_discount": cart_discount,
        "rating": rating, "review_count": review_count,
        "coupon": coupon, "variants": variants,
    }


# ==========================================
# JS / DOM PRICE — Playwright fallback içinde
# ==========================================
async def _js_price(page) -> Optional[float]:
    try:
        v = await page.evaluate("""
        () => {
            const paths = [
                () => window.productState?.product?.price?.finalPrice,
                () => window.productState?.product?.price?.currentPrice,
                () => window.productState?.product?.buyBoxInfo?.priceInfo?.finalPrice,
                () => window.productState?.product?.currentListing?.price?.finalPrice,
                () => window.productState?.product?.currentListing?.price?.amount,
                () => window.productModel?.price?.finalPrice,
                () => window.productModel?.price?.currentPrice,
            ];
            for (const fn of paths) {
                try {
                    const v = fn();
                    if (v && typeof v === 'number' && v > 10) return v;
                } catch(e) {}
            }
            try {
                const disc = window.productState?.product?.price?.discountedPrice;
                const orig = window.productState?.product?.price?.value;
                if (disc && typeof disc === 'number' && disc > 10) {
                    if (!orig || (disc >= orig * 0.15 && disc <= orig)) return disc;
                }
            } catch(e) {}
            try {
                const v = window.productState?.product?.price?.value
                    || window.productModel?.price?.value;
                if (v && typeof v === 'number' && v > 10) return v;
            } catch(e) {}
            return null;
        }
        """)
        return float(v) if v and float(v) > 10 else None
    except Exception:
        return None


async def _dom_price(page) -> Optional[float]:
    try:
        await page.evaluate("""
            ['[data-test-id="see-earnings"]','[data-test-id="payment-options"]',
             '[data-test-id="prev-price"]'
            ].forEach(s => document.querySelectorAll(s).forEach(e => e.remove()));
        """)
    except Exception:
        pass

    for sel in [
        '[data-test-id="price-current-price"]',
        '[data-test-id^="final-price-"]',
        '[data-test-id="checkout-price"]',
        '[data-test-id="default-price"]',
        '[data-test-id="price-value"]',
        'span[itemprop="price"]',
        'meta[itemprop="price"]',
    ]:
        try:
            el = await page.query_selector(sel)
            if not el: continue
            content = await el.get_attribute("content") if sel.startswith("meta") else None
            text = content or (await el.inner_text()).strip().split('\n')[0]
            v = parse_price_tr_clean(text)
            if v and v > 0:
                return v
        except Exception:
            continue

    try:
        pb = await page.query_selector('[data-test-id="price"]')
        if pb:
            text = await pb.inner_text()
            candidates = [parse_price_tr_clean(r) for r in re.findall(r'([\d\.]+[,]\d{2})', text)]
            candidates = [v for v in candidates if v and v > 0]
            if candidates:
                return min(candidates)
    except Exception:
        pass
    return None


# ==========================================
# PRICE / STOCK / IMAGE HELPERS
# ==========================================
def _get_nested(d, path: list):
    for k in path:
        if isinstance(d, dict):   d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and len(d) > k: d = d[k]
        else: return None
    if isinstance(d, (int, float)) and d > 10: return float(d)
    if isinstance(d, str): return parse_price_tr_clean(d)
    return None


def _price_from_state(state: dict) -> Optional[float]:
    product = state.get("product", {})

    # ── Yeni HB format (2025+): listings[0].minimumPrice / prices[].value ──
    for listing in (product.get("listings") or [])[:1]:
        if not isinstance(listing, dict): continue
        for k in ("minimumPrice", "originalPrice", "salePrice"):
            v = listing.get(k)
            if isinstance(v, (int, float)) and v > 10:
                return float(v)

    for price_item in (product.get("prices") or []):
        if not isinstance(price_item, dict): continue
        v = price_item.get("value")
        if isinstance(v, (int, float)) and v > 10:
            return float(v)

    for variant in (product.get("variants") or [])[:1]:
        if not isinstance(variant, dict): continue
        v = variant.get("price")
        if isinstance(v, (int, float)) and v > 10:
            return float(v)

    # ── Eski HB format ──────────────────────────────────────────────────────
    for path in [
        ["price", "finalPrice"], ["price", "currentPrice"],
        ["buyBoxInfo", "priceInfo", "finalPrice"], ["buyBoxInfo", "price"],
        ["currentListing", "price", "finalPrice"],
        ["currentListing", "price", "amount"],
        ["currentListing", "price", "currentPrice"],
    ]:
        v = _get_nested(product, path)
        if v: return v

    for listing in (product.get("currentListings") or [])[:3]:
        if not isinstance(listing, dict): continue
        for lp in [["price", "finalPrice"], ["price", "amount"], ["price", "currentPrice"]]:
            v = _get_nested(listing, lp)
            if v: return v

    disc = _get_nested(product, ["price", "discountedPrice"])
    orig = (_get_nested(product, ["price", "value"]) or
            _get_nested(product, ["price", "originalPrice"]))
    if disc:
        if orig:
            if orig * 0.15 <= disc <= orig: return disc
        else:
            return disc

    v = _get_nested(product, ["buyBoxInfo", "priceInfo", "value"])
    if v: return v
    return orig


def _parse_next_data_full(j: dict) -> Optional[dict]:
    """__NEXT_DATA__ JSON'undan tüm ürün alanlarını çıkarır."""
    result: dict = {}

    def _dig(d, depth=0):
        if depth > 8 or not isinstance(d, dict): return
        if not result.get("price"):
            for key in ("finalPrice", "currentPrice", "salePrice", "discountedPrice"):
                v = d.get(key)
                if isinstance(v, (int, float)) and v > 10:
                    result["price"] = float(v); break
                if isinstance(v, str):
                    pv = parse_price_tr_clean(v)
                    if pv: result["price"] = pv; break
        if not result.get("title"):
            for key in ("name", "displayName", "title", "productName"):
                v = d.get(key)
                if isinstance(v, str) and len(v) > 5:
                    result["title"] = v; break
        if not result.get("image"):
            for key in ("imageUrl", "mainImageUrl", "thumbnailUrl"):
                v = d.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    result["image"] = v; break
        if not result.get("stock"):
            salable = d.get("isSalable") or d.get("isSaleable") or d.get("available")
            if salable is False: result["stock"] = "Stok Yok"
            elif salable is True: result["stock"] = "Stokta Var"
            qty = d.get("stockQty") or d.get("stockCount") or d.get("quantity")
            if isinstance(qty, (int, float)):
                result["stock"] = "Stok Yok" if qty == 0 else "Stokta Var"
        if not result.get("rating"):
            for key in ("rating", "averageRating", "ratingScore", "score"):
                v = d.get(key)
                if isinstance(v, (int, float)) and 0 < v <= 5:
                    result["rating"] = float(v); break
                if isinstance(v, dict):
                    inner = v.get("average") or v.get("averageScore") or v.get("value")
                    if isinstance(inner, (int, float)) and 0 < inner <= 5:
                        result["rating"] = float(inner); break
                    if isinstance(inner, (int, float)) and 0 < inner <= 100:
                        result["rating"] = round(inner / 20, 1); break
        if not result.get("review_count"):
            for key in ("reviewCount", "ratingCount", "totalReviews", "commentCount"):
                v = d.get(key)
                if isinstance(v, int) and v >= 0:
                    result["review_count"] = v; break
        for key in ("product", "pageProps", "productDetail", "data", "price", "listing"):
            sub = d.get(key)
            if isinstance(sub, dict): _dig(sub, depth + 1)

    _dig(j)
    return result if (result.get("price") or result.get("title")) else None


def _stock_from_state(state: dict) -> str:
    product = state.get("product", {})
    # Yeni format: isAvailableProduct
    avail = product.get("isAvailableProduct")
    if avail is False: return "Stok Yok"
    if avail is True:  return "Stokta Var"
    # Eski format
    salable = product.get("isSalable") or product.get("isSaleable")
    if salable is False: return "Stok Yok"
    qty = product.get("stockQty") or product.get("stock")
    if isinstance(qty, (int, float)): return "Stok Yok" if qty == 0 else "Stokta Var"
    if salable is True: return "Stokta Var"
    # listings[0] stok kontrolü
    for listing in (product.get("listings") or [])[:1]:
        if isinstance(listing, dict):
            if listing.get("available") is False: return "Stok Yok"
            if listing.get("available") is True:  return "Stokta Var"
    return "Bilinmiyor"


def _image_from_state(state: dict) -> Optional[str]:
    product = state.get("product", {})
    for path in [["mainImageUrl"], ["imageUrl"], ["images", 0, "url"], ["images", 0], ["image"]]:
        d = product
        for k in path:
            if isinstance(d, dict): d = d.get(k)
            elif isinstance(d, list) and isinstance(k, int) and len(d) > k: d = d[k]
            else: d = None; break
        if isinstance(d, str) and d.startswith("http"): return d
    return None


def _coupon_from_state(state: dict) -> Optional[str]:
    product = state.get("product", {}) or {}
    for key in ("couponText", "couponTitle", "couponName", "merchantCoupon"):
        val = product.get(key)
        if val and isinstance(val, str) and val.strip(): return val.strip()[:200]
    for c in (product.get("coupons") or product.get("merchantCoupons") or []):
        if isinstance(c, dict):
            t = c.get("title") or c.get("text") or c.get("name") or ""
            if t: return str(t)[:200]
    return None


def _variants_from_state(state: dict) -> Optional[list]:
    product = state.get("product", {}) or {}
    raw = (product.get("variants") or product.get("listings") or
           product.get("skus") or product.get("variantList") or [])
    if not raw or not isinstance(raw, list): return None
    parsed, seen = [], set()
    for var in raw:
        if not isinstance(var, dict): continue
        name_parts = []
        for attr in ("color", "colorName", "size", "sizeName", "attribute", "attributeValue"):
            v = (var.get(attr) or "").strip()
            if v and v not in name_parts: name_parts.append(v)
        name = " / ".join(name_parts) or (var.get("id") or var.get("sku") or "")
        if not name or name in seen: continue
        seen.add(name)
        vp = None
        for pk in ("price", "finalPrice", "discountedPrice", "currentPrice"):
            pv = var.get(pk)
            if isinstance(pv, dict): pv = pv.get("value") or pv.get("finalPrice")
            if isinstance(pv, (int, float)) and pv > 0: vp = float(pv); break
        in_stock = not (var.get("outOfStock") or (var.get("stockQty") or 1) == 0)
        parsed.append({"name": str(name)[:100], "price": vp, "in_stock": in_stock})
        if len(parsed) >= 50: break
    return parsed or None
