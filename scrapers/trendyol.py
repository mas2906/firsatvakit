#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trendyol scraper — çok katmanlı yaklaşım (v3).

Katmanlar:
1) httpx (hızlı, hafif — __envoy_ JSON + og:image parse)
2) curl_cffi (Chrome TLS fingerprint — anti-bot bypass)
3) Playwright (son çare — Python 3.14'te çalışmayabilir)
"""

import re
import logging
import json
import random
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from scrapers.utils import CLOUDFLARE_TITLES, STEALTH_SCRIPT, UA_POOL, parse_price_tr_clean, detect_cart_discount, get_playwright_sem, RateLimiter, stream_fetch, get_stealth_headers

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CurlSession = None
    CURL_AVAILABLE = False

import httpx

log = logging.getLogger("trendyol")

_limiter      = RateLimiter(0.0, 0.05)
_limiter_fast = RateLimiter(0.0, 0.02)

_TY_API = "https://public.trendyol.com/discovery-web-productgw-service/api/productDetail/{content_id}"

IMPERSONATE_POOL = ["chrome146", "chrome142", "chrome136", "chrome131", "chrome124", "chrome120"]

# Global persistent sessions — her scrape'de yeni TCP/TLS kurmayı önler
_CURL_SESSION: Optional[CurlSession] = None
_CURL_LOCK = asyncio.Lock()
_HTTPX_CLIENT: Optional[httpx.AsyncClient] = None
_HTTPX_LOCK = asyncio.Lock()


async def _get_curl_session() -> CurlSession:
    global _CURL_SESSION
    async with _CURL_LOCK:
        if _CURL_SESSION is None:
            imp = random.choice(IMPERSONATE_POOL)
            _CURL_SESSION = CurlSession(impersonate=imp, timeout=20)
            # Warm-up tek seferlik — cookie ve TLS session oluştur
            try:
                await _CURL_SESSION.get("https://www.trendyol.com/", timeout=8)
            except Exception:
                pass
            log.info(f"[trendyol] curl session oluşturuldu: {imp}")
        return _CURL_SESSION


def _reset_curl_session() -> None:
    global _CURL_SESSION
    _CURL_SESSION = None


async def _get_httpx_client() -> httpx.AsyncClient:
    global _HTTPX_CLIENT
    async with _HTTPX_LOCK:
        if _HTTPX_CLIENT is None or _HTTPX_CLIENT.is_closed:
            _HTTPX_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                follow_redirects=True,
            )
        return _HTTPX_CLIENT


def _extract_content_id(url: str) -> Optional[str]:
    m = re.search(r'-p-(\d+)', url)
    return m.group(1) if m else None


async def trendyol_api_fetch(url: str) -> Optional[dict]:
    """Trendyol public API — tarayıcı gerektirmez, en hızlı katman."""
    content_id = _extract_content_id(url)
    if not content_id:
        return None
    api_url = _TY_API.format(content_id=content_id)
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "tr-TR,tr;q=0.9",
        "Referer": url,
        "Origin": "https://www.trendyol.com",
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(api_url, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("result") or data
        if not isinstance(result, dict):
            return None

        price_info = result.get("price") or {}
        price = None
        for pk in ("sellingPrice", "discountedPrice", "campaignPrice"):
            pv = price_info.get(pk)
            if isinstance(pv, dict):
                price = pv.get("value")
            elif isinstance(pv, (int, float)):
                price = pv
            if price:
                break
        if not price:
            price = price_info.get("value") or result.get("displayedPrice")
        if not price:
            return None

        images = result.get("images") or []
        image_url = None
        if images:
            raw = str(images[0])
            image_url = raw if raw.startswith("http") else f"https://cdn.dsmcdn.com{raw}"

        brand = (result.get("brand") or {}).get("name") or result.get("brandName")
        stock = "Stokta Var" if result.get("hasStock") or result.get("isAvailable") else "Stok Yok"
        name = result.get("name") or result.get("title") or ""
        if brand and brand not in name:
            name = f"{brand} {name}".strip()

        return {
            "title": name,
            "price": float(price),
            "image_url": image_url,
            "brand": brand,
            "stock": stock,
            "cart_discount": False,
            "url": url.split("?")[0],
        }
    except Exception as e:
        log.debug(f"[trendyol/api] Hata: {e}")
        return None


async def scrape_trendyol(url: str, pool=None, price_only: bool = False) -> Optional[dict]:
    """Çok katmanlı Trendyol scraper."""
    await (_limiter_fast if price_only else _limiter).wait()

    # NOT: public.trendyol.com NXDOMAIN — Layer 0 API devre dışı

    # ── Katman 1: curl_cffi (stream_fetch + __envoy__PROPS) ──
    if CURL_AVAILABLE:
        data = await _via_curl_cffi(url)
        if data and data.get("title") and data.get("price"):
            log.info(f"[trendyol/curl_cffi] ✔ title={data['title']!r:.50} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
            return _price_only_filter(data, price_only)

        # ── Katman 2: httpx (curl_cffi varken fallback) ──────
        data = await _via_httpx(url)
        if data and data.get("title") and data.get("price"):
            log.info(f"[trendyol/httpx] ✔ title={data['title']!r:.50} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
            return _price_only_filter(data, price_only)
    else:
        log.warning("[trendyol] curl_cffi yüklü değil, direkt Playwright kullanılıyor")

    # ── Katman 3: Playwright (son çare) ──────────────────────
    log.info(f"[trendyol] Playwright deneniyor...")
    data = await _via_playwright(url, pool=pool)
    if data and data.get("title"):
        log.info(f"[trendyol/playwright] ✔ title={data['title']!r:.50} price={data.get('price')} image={'✔' if data.get('image_url') else '✘'}")
        return _price_only_filter(data, price_only)

    log.error(f"[trendyol] ✗ Tüm yöntemler başarısız: {url}")
    return None


def _price_only_filter(data: Optional[dict], price_only: bool) -> Optional[dict]:
    if not price_only or not data:
        return data
    return {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}


# ═══════════════════════════════════════════════════════════════
# KATMAN 1: httpx
# ═══════════════════════════════════════════════════════════════

async def _via_httpx(url: str) -> Optional[dict]:
    _ua = random.choice(UA_POOL)
    headers = {
        "User-Agent": _ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.trendyol.com/",
        **get_stealth_headers(_ua),
    }
    try:
        client = await _get_httpx_client()
        r = await client.get(url, headers=headers)

        if r.status_code != 200:
            log.info(f"[trendyol/httpx] status={r.status_code}")
            return None

        html = r.text
        if _is_blocked(html):
            log.info("[trendyol/httpx] Cloudflare/block algılandı")
            return None

        return await asyncio.to_thread(_parse, html, url)
    except Exception as e:
        log.error(f"[trendyol/httpx] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 2: curl_cffi
# ═══════════════════════════════════════════════════════════════

async def _via_curl_cffi(url: str) -> Optional[dict]:
    try:
        session = await _get_curl_session()
        html = await stream_fetch(
            session, url,
            json_markers=["__envoy__PROPS"],
            max_kb=700,
            timeout=15,
            headers={
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Referer": "https://www.trendyol.com/",
            },
            validate_fn=lambda text: bool(_extract_envoy(text)),
        )

        if not html:
            log.info("[trendyol/curl_cffi] Başarısız")
            return None

        if _is_blocked(html):
            log.info("[trendyol/curl_cffi] Block algılandı — session sıfırlanıyor")
            _reset_curl_session()
            return None

        return await asyncio.to_thread(_parse, html, url)
    except Exception as e:
        log.error(f"[trendyol/curl_cffi] Hata: {e}")
        _reset_curl_session()
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 3: Playwright (Python 3.14'te çalışmayabilir)
# ═══════════════════════════════════════════════════════════════

async def _via_playwright(url: str, pool=None) -> Optional[dict]:
    if pool:
        page = await pool.acquire()
        try:
            from scrapers.cdp_base import setup_resource_blocking
            await setup_resource_blocking(page)
            await page.add_init_script(STEALTH_SCRIPT)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(2.0, 3.5))
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.5, 1.0))
            html = await page.content()
            if html and not _is_blocked(html):
                return await asyncio.to_thread(_parse, html, url)
            return None
        except Exception as e:
            log.error(f"[trendyol/playwright+pool] Hata: {e}")
            return None
        finally:
            await pool.release(page)
    async with get_playwright_sem():
        return await _launch_playwright(url)


async def _launch_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        html = None
        current_url = url
        async with async_playwright() as p:
            browser = None
            try:
                try:
                    from playwright_stealth import Stealth
                    _stealth = Stealth()
                except Exception:
                    _stealth = None

                browser = await p.chromium.launch(headless=True, args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ])
                _ua = random.choice(UA_POOL)
                context = await browser.new_context(
                    user_agent=_ua,
                    locale="tr-TR",
                    timezone_id="Europe/Istanbul",
                    viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
                    extra_http_headers=get_stealth_headers(_ua),
                )
                await context.add_init_script(STEALTH_SCRIPT)
                page = await context.new_page()
                if _stealth:
                    await _stealth.apply_stealth_async(page)
                await page.route("**/*.{gif,svg,ico}", lambda r: r.abort())
                # Ana sayfadan giriş
                try:
                    await page.goto("https://www.trendyol.com/", wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    await page.mouse.move(random.randint(100, 700), random.randint(100, 400))
                except Exception:
                    pass
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(2.5, 4.0))
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(0.5, 1.0))

                page_title = await page.title()
                if any(cf.lower() in page_title.lower() for cf in CLOUDFLARE_TITLES):
                    log.info(f"[trendyol/playwright] Cloudflare engeli")
                    return None

                html = await page.content()
                current_url = page.url.split("?")[0].split("#")[0]
            finally:
                if browser:
                    await browser.close()

        if html:
            return await asyncio.to_thread(_parse, html, current_url)
        return None
    except NotImplementedError:
        log.info("[trendyol/playwright] Python sürümü desteklenmiyor (3.14+)")
        return None
    except Exception as e:
        log.error(f"[trendyol/playwright] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# BLOCK TESPİT
# ═══════════════════════════════════════════════════════════════

def _is_blocked(html: str) -> bool:
    if not html:
        return True
    t = html[:5000].lower()
    return any(m in t for m in ["cloudflare", "captcha", "challenge-platform", "just a moment", "attention required"])


def _pv(obj) -> Optional[float]:
    """price objesinden değer çıkar — hem {value: X} hem düz sayı formatını destekler."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get("value") or obj.get("amount") or obj.get("price")
        if v is not None:
            try: return float(v)
            except (ValueError, TypeError): pass
        return None
    try:
        return float(obj)
    except (ValueError, TypeError):
        return None


# Sepet/kampanya fiyatı anahtar isimleri — öncelik sırasıyla
_BASKET_KEYS = (
    "basketPrice", "promotionPrice", "basketSellingPrice",
    "plusPrice", "memberPrice", "loyaltyDiscountedPrice", "campaignSellingPrice",
    "basketDiscountedPrice", "promotionalPrice", "discountedBasketPrice",
)
# NOT: "campaignPrice" kasıtlı olarak çıkarıldı.
# Trendyol bazı ürünlerde campaignPrice'ı "2000 TL'ye 200 TL İndirim" gibi
# kampanya eşik değeri (ürün fiyatı değil) olarak doldurur. Bunu sepet fiyatı
# sanarak yanlış fiyat çekiyorduk. HTML'deki .new-price her zaman daha güvenilir.


def _parse(html: str, current_url: str = "") -> dict:
    current_url = current_url.split("?")[0].split("#")[0]
    envoy = _extract_envoy(html)
    product = envoy.get("product") if envoy else None
    title = price = brand = seller = image_url = rating = reviews = stock = barcode = None
    cart_discount = False
    soup = None

    if product:
        title = product.get("name")

        ml = product.get("merchantListing") or {}
        winner = ml.get("winnerVariant") or product.get("winnerVariant") or {}
        p_obj = winner.get("price", {}) or {}

        # Sepette/kampanya indirimli fiyat — {value: X} veya düz sayı her ikisini de destekler
        for _basket_key in _BASKET_KEYS:
            _bv = _pv(p_obj.get(_basket_key))
            if _bv is not None and _bv > 0:
                price = _bv
                cart_discount = True
                break

        # Gerçek fiyat: discountedPrice ve sellingPrice'ın minimuğunu al.
        # Trendyol'da sellingPrice üstü çizili (orijinal) fiyat olabilir;
        # discountedPrice fiilen ödenen fiyattır. İkisi varsa küçüğü doğrudur.
        if price is None:
            v_disc = _pv(p_obj.get("discountedPrice")) or _pv(p_obj.get("originalPrice"))
            v_sell = _pv(p_obj.get("sellingPrice"))
            if v_disc and v_sell:
                price = min(v_disc, v_sell)
            elif v_disc:
                price = v_disc
            elif v_sell:
                price = v_sell

        # Fallback: variants listesi
        if price is None:
            for var in (product.get("variants", []) or []):
                vp = var.get("price") or {}
                _bv = None
                for _bk in _BASKET_KEYS:
                    _bv = _pv(vp.get(_bk))
                    if _bv is not None and _bv > 0:
                        cart_discount = True
                        break
                raw = _bv or _pv(vp.get("sellingPrice")) or _pv(vp.get("discountedPrice")) or \
                      (_pv(vp) if not isinstance(vp, dict) else None)
                if raw is not None:
                    price = raw
                    break

        # Barkod: winnerVariant önce, sonra variants
        barcode = _normalize_barcode(winner.get("barcode"))
        if not barcode:
            for var in (product.get("variants", []) or []):
                barcode = _normalize_barcode(var.get("barcode"))
                if barcode:
                    break

        b = product.get("brand", {}) or {}
        brand = b.get("name")

        merchant = ml.get("merchant") or {}
        seller = merchant.get("name")

        image_url = _extract_image_from_json(product)

        rs = product.get("ratingScore") or {}
        rating = rs.get("averageRating")
        reviews = rs.get("totalCount")

        stock = _detect_stock_trendyol(product, winner)

        # ── Kupon / sepette indirim tespiti (envoy'dan) ─────────
        if envoy and envoy.get("hasCollectableCoupon"):
            cart_discount = True

    # Fallback: HTML'den çek
    if not title:
        soup = BeautifulSoup(html, "html.parser")
        for sel in [
            "h1.pr-new-br span", "h1.pr-new-br",
            "[data-testid='product-name']",
            "h1[class*='product-name']", "h1[class*='title']", "h1",
        ]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) > 5:
                    title = t
                    break
        if not title:
            m = re.search(r'<title[^>]*>([^<]+)</title>', html)
            if m:
                t = m.group(1).strip()
                title = t.split("|")[0].strip() or t.split("-")[0].strip()

    # HTML'den sepet fiyatı — CSS ile bulunanlar her zaman öncelikli.
    # Güvenlik: JSON fiyatının %40'ından düşük değerleri yanlış pozitif olarak reddet.
    soup = soup or BeautifulSoup(html, "html.parser")
    _basket_html_price, _basket_html_hc = _extract_basket_price_html(soup, html)
    if _basket_html_price:
        _json_ref = price or 0
        _too_low = _json_ref > 0 and _basket_html_price < _json_ref * 0.40
        if not _too_low:
            cart_discount = True
            # Yüksek güvenilirlik (CSS selektör): JSON basket key fiyatını her zaman geçersiz kılar.
            # Düşük güvenilirlik (metin arama): yalnızca mevcut fiyattan düşükse kullan.
            if _basket_html_hc or not price or _basket_html_price < price:
                price = _basket_html_price
        else:
            log.warning(
                f"[trendyol] basket HTML fiyatı ({_basket_html_price}) JSON fiyatının "
                f"({_json_ref}) %40'ından düşük — yanlış pozitif olarak reddedildi"
            )

    if not price:
        for sel in [
            "[data-testid='price-current-price']", ".prc-dsc",
            ".product-price-container span", ".prc-slg",
            "span.discounted", "p.new-price", "[class*='new-price']",
        ]:
            el = soup.select_one(sel)
            if el:
                price = parse_price_tr_clean(el.get_text())
                if price:
                    break

    if not image_url:
        soup = soup or BeautifulSoup(html, "html.parser")
        image_url = _extract_image_from_html(soup, html)

    if not brand:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in ["[data-testid='product-brand-name']", ".product-brand-name",
                    "a[href*='/marka/']", "[class*='brand']"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) < 60:
                    brand = t
                    break

    if not seller:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in ["[data-testid='merchant-name']", ".merchant-text", "a[href*='/magaza/']"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) < 80:
                    seller = t
                    break

    if not stock:
        soup = soup or BeautifulSoup(html, "html.parser")
        body_text = soup.get_text().lower()
        if any(kw in body_text for kw in ["tükendi", "stokta yok", "stok yok", "satışta değil", "out of stock"]):
            stock = "Stok Yok"
        elif any(kw in body_text for kw in ["sepete ekle", "hemen al", "satın al"]):
            stock = "Stokta Var"
        else:
            stock = "Bilinmiyor"

    if not barcode:
        barcode = _extract_barcode_regex(html)

    # ── Sepette indirim tespiti ──────────────────────────────
    if not cart_discount:
        cart_discount = detect_cart_discount(html)
    if not cart_discount and product:
        _cart_kw = ["sepette", "2. ürün", "ikinci ürün", "çoklu", "kombinasyon"]
        for field in ["promotionText", "campaignText", "discountText"]:
            val = (product.get(field) or winner.get(field) or "").lower()
            if any(kw in val for kw in _cart_kw):
                cart_discount = True
                break
        if not cart_discount:
            for c in (product.get("campaigns") or product.get("promotions") or []):
                if isinstance(c, dict):
                    ct = (c.get("text") or c.get("name") or c.get("description") or "").lower()
                    if any(kw in ct for kw in _cart_kw):
                        cart_discount = True
                        break

    # ── Kupon ────────────────────────────────────────────────
    coupon = None
    if product:
        for key in ("couponTitle", "couponText", "couponName"):
            val = (product.get(key) or "").strip()
            if val:
                coupon = val[:200]
                break
        if not coupon and envoy and envoy.get("hasCollectableCoupon"):
            for key in ("couponTitle", "couponText"):
                val = (envoy.get(key) or "").strip()
                if val:
                    coupon = val[:200]
                    break
            if not coupon:
                for c in (product.get("coupons") or []):
                    t = c.get("title") or c.get("text") or ""
                    if t:
                        coupon = t[:200]
                        break
            if not coupon:
                coupon = "Kupon mevcut"
    if not coupon:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in ["[data-testid='coupon-banner']", ".coupon-container", "[class*='coupon']"]:
            el = soup.select_one(sel)
            if el:
                t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if t and len(t) > 2:
                    coupon = t[:200]
                    break

    # ── Varyantlar ───────────────────────────────────────────
    variants_list = None
    if product:
        raw_variants = product.get("variants") or []
        parsed_variants = []
        seen_names: set = set()
        for var in raw_variants:
            if not isinstance(var, dict):
                continue
            attr = (var.get("attributeValue") or var.get("value") or var.get("size") or "").strip()
            # Renk + beden birleştir
            color = (var.get("color") or var.get("colorName") or "").strip()
            size = (var.get("size") or var.get("sizeName") or "").strip()
            if color and size:
                name = f"{color} / {size}"
            elif color:
                name = color
            elif size:
                name = size
            elif attr:
                name = attr
            else:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            vp = var.get("price") or {}
            # Sepet fiyatı veya satış fiyatı
            var_price = None
            if isinstance(vp, dict):
                for _bk in _BASKET_KEYS:
                    _bv = _pv(vp.get(_bk))
                    if _bv:
                        var_price = _bv
                        break
                if not var_price:
                    var_price = _pv(vp.get("sellingPrice")) or _pv(vp.get("discountedPrice"))
            elif isinstance(vp, (int, float)):
                var_price = float(vp) if vp > 0 else None
            in_stock = bool(var.get("inStock") or var.get("hasStock") or (var.get("stock") or 0) > 0)
            parsed_variants.append({"name": name[:100], "price": var_price, "in_stock": in_stock})
        if parsed_variants:
            variants_list = parsed_variants[:50]

    # ── Sepette kampanya fiyatı — JSON fiyatından daha düşükse override ──
    soup = soup or BeautifulSoup(html, "html.parser")
    log.info(
        f"[trendyol] title={title} price={price} brand={brand} seller={seller} stock={stock} "
        f"cart_discount={cart_discount} coupon={'✔' if coupon else '✘'} variants={len(variants_list or [])}"
    )
    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "rating": rating,
        "review_count": reviews,
        "brand": brand,
        "seller": seller,
        "stock": stock,
        "barcode": barcode,
        "url": current_url,
        "cart_discount": cart_discount,
        "coupon": coupon,
        "variants": variants_list,
    }


def _extract_basket_price_html(soup, html: str) -> tuple:
    """
    HTML'den sepet/kampanya fiyatını çıkarır.
    Returns (price, high_confidence):
      high_confidence=True  → CSS selektörlerle bulundu (güvenilir)
      high_confidence=False → metin aramasıyla bulundu (şüpheli)
    """
    # 0a) TY Plus fiyatı — data-testid="ty-plus-price" + .ty-plus-price-discounted-price
    for plus_sel in ["[data-testid='ty-plus-price']", ".ty-plus-price"]:
        plus_el = soup.select_one(plus_sel)
        if plus_el:
            disc = plus_el.select_one(".ty-plus-price-discounted-price")
            if disc:
                p = parse_price_tr_clean(disc.get_text())
                if p:
                    return (p, True)

    # 0b) Öncelikli: <div class="campaign-price-content"> yapısı
    #    <p class="text">Sepette</p> + <p class="new-price">7.051,85 TL</p>
    #    UYARI: .campaign-price-info altındaki "2000 TL'ye 200 TL İndirim" gibi
    #    kampanya eşik metinlerini fiyat olarak almamak için .new-price CSS'e güveniyoruz.
    for container_sel in [
        ".campaign-price-content",
        ".campaign-price-wrapper",
    ]:
        container = soup.select_one(container_sel)
        if not container:
            continue
        label = container.find(string=re.compile(r"Sepette|Kampanya", re.I))
        if not label:
            continue
        price_el = container.select_one(".new-price") or container.select_one("[class*='new-price']")
        if price_el:
            p = parse_price_tr_clean(price_el.get_text())
            if p:
                return (p, True)

    # 1) CSS selektörler — Trendyol'un bilinen basket/campaign price sınıfları
    for sel in [
        ".campaign-price-wrapper .new-price",
        ".campaign-price-content .new-price",
        "[data-testid='basket-price']",
        "[data-testid='product-campaign-info-price']",
        ".basket-price-wrapper .prc-dsc",
        ".campaign-price-wrapper .prc-dsc",
        ".campaign-price-container .prc-dsc",
        "[class*='basket-price'] .prc-dsc",
        "[class*='campaign-price'] .prc-dsc",
        "[class*='basketPrice']",
        "[class*='campaign-price'] .new-price",
    ]:
        el = soup.select_one(sel)
        if el:
            p = parse_price_tr_clean(el.get_text())
            if p:
                return (p, True)

    # 1b) "Sepette" badge (.prc-slg / .prc-badge / .ty-plus-price-badge) yanındaki fiyat
    # Örnek: <span class="prc-slg">Sepette</span><span class="prc-dsc">3.899 TL</span>
    # Örnek: <span class="ty-plus-price-badge">Sepette</span><span class="ty-plus-price-discounted-price">1.196,96 TL</span>
    for badge_sel in [".prc-slg", "[class*='prc-badge']", "[class*='basket-badge']",
                      "[class*='sepette-badge']", "[class*='sepette-label']",
                      ".ty-plus-price-badge"]:
        badge = soup.select_one(badge_sel)
        if not badge:
            continue
        if not re.search(r'sepette', badge.get_text(), re.I):
            continue
        # Aynı parent içindeki .prc-dsc / .ty-plus-price-discounted-price / .new-price fiyatına bak
        _parent = badge.parent
        for _ in range(3):
            if not _parent:
                break
            for price_sel in [".ty-plus-price-discounted-price", ".prc-dsc", ".new-price",
                               "[class*='new-price']", "[class*='prc-dsc']",
                               "[class*='discounted-price']", "ins"]:
                _pe = _parent.select_one(price_sel)
                if _pe and _pe != badge:
                    p = parse_price_tr_clean(_pe.get_text())
                    if p:
                        return (p, True)
            _parent = _parent.parent

    # 2) "Sepette" veya "Kampanyalı Fiyat" yazan elementin kardeş/ebeveyn fiyatını bul.
    # Sadece ürün fiyat container'ı içinde ara — tüm sayfada değil.
    _price_root = (
        soup.select_one(".campaign-price-content")
        or soup.select_one(".price-wrapper")
        or soup.select_one(".product-price-container")
        or soup.select_one(".pr-bx-rnr-pr-rnr")
        or soup.select_one("[class*='product-price']")
        or soup.select_one("[class*='price-box']")
    )
    _search_scope = _price_root if _price_root else None
    if _search_scope:
        for tag in _search_scope.find_all(string=re.compile(r"(Sepette|Kampanyalı Fiyat)", re.I)):
            parent = tag.parent
            for _ in range(3):
                if parent is None:
                    break
                text = parent.get_text(" ", strip=True)
                m = re.search(r'([\d.,]{4,})\s*TL', text)
                if m:
                    p = parse_price_tr_clean(m.group(1))
                    if p:
                        return (p, False)
                parent = parent.parent

    return (None, False)


def _detect_stock_trendyol(product: dict, winner: dict) -> Optional[str]:
    """Trendyol JSON'undan stok durumunu tespit eder — tüm olası alanları kontrol eder."""

    # 1) Açık boolean alanlar
    for field in ["inStock", "hasStock", "isAvailable", "isActive"]:
        val = product.get(field)
        if val is True:
            return "Stokta Var"
        if val is False:
            return "Stok Yok"

    # 2) winnerVariant yoksa veya boşsa → stok yok
    if not winner:
        return "Stok Yok"

    # 3) winnerVariant'taki stok alanları
    for field in ["inStock", "hasStock", "isAvailable"]:
        val = winner.get(field)
        if val is True:
            return "Stokta Var"
        if val is False:
            return "Stok Yok"

    # 4) stock sayısı
    for _s in [winner.get("stock"), winner.get("stockCount"), product.get("stockCount")]:
        if _s is not None:
            try:
                return "Stokta Var" if int(_s) > 0 else "Stok Yok"
            except (ValueError, TypeError):
                pass

    # 5) Tüm varyantlar stoksuzsa
    variants = product.get("variants") or []
    if variants:
        any_stock = any(
            v.get("inStock") or v.get("hasStock") or (v.get("stock", 0) or 0) > 0
            for v in variants if isinstance(v, dict)
        )
        if not any_stock:
            return "Stok Yok"

    # 6) Fiyat yoksa büyük ihtimalle stokta değil
    if not winner.get("price"):
        return "Stok Yok"

    return None  # Bilinmiyor — HTML fallback devreye girecek


def _extract_envoy(html: str) -> Optional[dict]:
    """window["__envoy__PROPS"] objesini parse eder — tam envoy döner."""
    for marker_match in re.finditer(r'window\["__envoy[^"]*PROPS"\]\s*=\s*\{', html):
        start = marker_match.end() - 1
        depth, end = 0, None
        limit = min(start + 800000, len(html))
        for i in range(start, limit):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            continue
        try:
            data = json.loads(html[start:end])
            if data.get("product"):
                return data
        except Exception:
            continue
    return None



def _extract_barcode_regex(html: str) -> Optional[str]:
    m = re.search(r'"winnerVariant"\s*:\s*\{[^}]*"barcode"\s*:\s*"(\d{8,14})"', html)
    if m:
        return _normalize_barcode(m.group(1))
    m = re.search(r'"barcode"\s*:\s*"(\d{8,14})"', html)
    if m:
        return _normalize_barcode(m.group(1))
    return None


def _normalize_barcode(value) -> Optional[str]:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value).strip())
    if not digits or len(digits) < 8 or len(digits) > 14:
        return None
    if len(set(digits)) == 1:
        return None
    return digits


# ═══════════════════════════════════════════════════════════════
# GÖRSEL ÇEKME — Çoklu Fallback Sistemi
# ═══════════════════════════════════════════════════════════════

def _to_full_url(path: str) -> Optional[str]:
    """Trendyol CDN path'ini tam URL'ye çevir."""
    if not path or not path.strip():
        return None
    path = path.strip()
    if path.startswith("http"):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://cdn.dsmcdn.com{path}"


def _extract_image_from_json(product: dict) -> Optional[str]:
    """JSON product objesinden görsel URL çıkar — 7 farklı path dener."""
    if not product:
        return None

    # 1) product.images — klasik yol
    images = product.get("images") or []
    if images and isinstance(images, list):
        url = _to_full_url(str(images[0]))
        if url:
            return url

    # 2) winnerVariant içindeki görseller
    winner = product.get("winnerVariant") or {}
    for key in ("images", "imageUrls", "galleryImages"):
        imgs = winner.get(key) or []
        if imgs and isinstance(imgs, list):
            url = _to_full_url(str(imgs[0]))
            if url:
                return url

    # 3) winnerVariant.image / imageUrl (tekil)
    for key in ("image", "imageUrl", "listingImage"):
        val = winner.get(key)
        if val and isinstance(val, str):
            url = _to_full_url(val)
            if url:
                return url

    # 4) galleryImages / imageUrls (üst seviye)
    for key in ("galleryImages", "imageUrls", "productImages"):
        imgs = product.get(key) or []
        if imgs and isinstance(imgs, list):
            url = _to_full_url(str(imgs[0]))
            if url:
                return url

    # 5) contentDescriptions içindeki görseller
    for desc in (product.get("contentDescriptions") or []):
        for key in ("imageUrl", "image", "url"):
            val = desc.get(key)
            if val and isinstance(val, str) and ("cdn" in val or "/" in val):
                url = _to_full_url(val)
                if url:
                    return url

    # 6) variants listesinden
    for var in (product.get("variants") or []):
        for key in ("images", "imageUrls"):
            imgs = var.get(key) or []
            if imgs and isinstance(imgs, list):
                url = _to_full_url(str(imgs[0]))
                if url:
                    return url
        for key in ("image", "imageUrl", "listingImage"):
            val = var.get(key)
            if val and isinstance(val, str):
                url = _to_full_url(val)
                if url:
                    return url

    # 7) color.imageUrl
    color = product.get("color") or {}
    val = color.get("imageUrl") or color.get("image")
    if val and isinstance(val, str):
        url = _to_full_url(val)
        if url:
            return url

    # 8) Derin arama — herhangi bir *image* key'i bul (son çare)
    url = _deep_find_image(product, depth=0)
    if url:
        return url

    return None


def _deep_find_image(obj, depth=0) -> Optional[str]:
    """JSON objesinde recursive olarak ilk görsel URL'yi bul."""
    if depth > 3:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if ("image" in kl or "img" in kl) and isinstance(v, str) and len(v) > 10:
                url = _to_full_url(v)
                if url and "cdn.dsmcdn.com" in url:
                    return url
            if ("image" in kl or "img" in kl) and isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, str) and len(first) > 5:
                    url = _to_full_url(first)
                    if url and "cdn.dsmcdn.com" in url:
                        return url
        # Daha derine in
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                result = _deep_find_image(v, depth + 1)
                if result:
                    return result
    elif isinstance(obj, list):
        for item in obj[:3]:  # İlk 3 elemanla sınırla
            result = _deep_find_image(item, depth + 1)
            if result:
                return result
    return None


def _extract_image_from_html(soup, html: str) -> Optional[str]:
    """HTML'den fallback olarak görsel URL çıkar."""

    # 1) og:image (en güvenilir)
    og = soup.find("meta", {"property": "og:image"})
    if og and og.get("content"):
        url = og["content"].strip()
        if url and len(url) > 10:
            return url

    # 2) twitter:image
    tw = soup.find("meta", {"name": "twitter:image"})
    if tw and tw.get("content"):
        url = tw["content"].strip()
        if url and len(url) > 10:
            return url

    # 3) JSON-LD structured data
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            ld = json.loads(script.string or "")
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            img = ld.get("image")
            if img:
                if isinstance(img, list) and img:
                    return str(img[0])
                if isinstance(img, str) and len(img) > 10:
                    return img
        except Exception:
            continue

    # 4) Ürün görseli CSS selektörleri (src + data-src + data-original)
    for sel in [
        "img.detail-section-img",
        "img[data-testid='product-image']",
        ".gallery-modal-content img",
        ".base-product-image img",
        "img[class*='product']",
        ".product-slide img",
        "img.ph-gl-img",
        ".slick-slide img",
        "img[class*='detail']",
        "img[class*='gallery']",
    ]:
        el = soup.select_one(sel)
        if el:
            for attr in ("src", "data-src", "data-original", "content"):
                src = el.get(attr, "")
                if src and "cdn.dsmcdn.com" in src:
                    return _to_full_url(src)

    # 5) Herhangi bir img tag'i cdn.dsmcdn.com içeren
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original"):
            src = img.get(attr, "")
            if src and "cdn.dsmcdn.com" in src and "/ty" in src:
                return _to_full_url(src)

    # 6) Regex: HTML'deki ilk cdn.dsmcdn.com görsel URL'si
    m = re.search(r'(https?://cdn\.dsmcdn\.com/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp))', html)
    if m:
        return m.group(1)

    # 7) Regex: tırnaklar arasındaki cdn path'i
    m = re.search(r'["\'](/ty\d+/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp))["\']', html)
    if m:
        return f"https://cdn.dsmcdn.com{m.group(1)}"

    return None

