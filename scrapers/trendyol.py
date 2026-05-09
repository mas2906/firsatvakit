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
from scrapers.utils import CLOUDFLARE_TITLES, STEALTH_SCRIPT, UA_POOL, parse_price_tr_clean, detect_cart_discount, PLAYWRIGHT_SEM, RateLimiter, stream_fetch, get_stealth_headers

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

import httpx

log = logging.getLogger("trendyol")

_limiter      = RateLimiter(2.0, 4.0)   # tam scrape
_limiter_fast = RateLimiter(1.0, 2.0)   # price_only

_TY_API = "https://public.trendyol.com/discovery-web-productgw-service/api/productDetail/{content_id}"


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
    else:
        log.warning("[trendyol] curl_cffi yüklü değil, httpx deneniyor...")

    # ── Katman 2: httpx (fallback) ────────────────────────────
    data = await _via_httpx(url)
    if data and data.get("title") and data.get("price"):
        log.info(f"[trendyol/httpx] ✔ title={data['title']!r:.50} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
        return _price_only_filter(data, price_only)

    # ── Katman 3: Playwright (son çare) ──────────────────────
    log.info(f"[trendyol] curl_cffi başarısız → Playwright deneniyor...")
    data = await _via_playwright(url, pool=pool)
    if data and data.get("title"):
        log.info(f"[trendyol/playwright] ✔ title={data['title']!r:.50} price={data.get('price')} image={'✔' if data.get('image_url') else '✘'}")
        return _price_only_filter(data, price_only)

    log.error(f"[trendyol] ✗ Tüm yöntemler başarısız: {url}")
    return None


def _price_only_filter(data: Optional[dict], price_only: bool) -> Optional[dict]:
    if not price_only or not data:
        return data
    return {k: data[k] for k in ("price", "stock", "cart_discount") if k in data}


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
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)

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
        impersonate = random.choice(["chrome146", "chrome142", "chrome136", "chrome131", "chrome124", "chrome120"])
        async with CurlSession(impersonate=impersonate) as session:
            try:
                await session.get("https://www.trendyol.com/", timeout=10)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                pass

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
            log.info("[trendyol/curl_cffi] Block algılandı")
            return None

        return await asyncio.to_thread(_parse, html, url)
    except Exception as e:
        log.error(f"[trendyol/curl_cffi] Hata: {e}")
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
    async with PLAYWRIGHT_SEM:
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
    "basketPrice", "campaignPrice", "promotionPrice", "basketSellingPrice",
    "plusPrice", "memberPrice", "loyaltyDiscountedPrice", "campaignSellingPrice",
    "basketDiscountedPrice", "promotionalPrice", "discountedBasketPrice",
)


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
            if _bv is not None:
                price = _bv
                cart_discount = True
                break

        # sellingPrice → gerçek satış fiyatı (indirim dahil)
        if price is None:
            v = _pv(p_obj.get("sellingPrice")) or _pv(p_obj.get("discountedPrice"))
            if v is not None:
                price = v

        # Fallback: variants listesi
        if price is None:
            for var in (product.get("variants", []) or []):
                vp = var.get("price") or {}
                _bv = None
                for _bk in _BASKET_KEYS:
                    _bv = _pv(vp.get(_bk))
                    if _bv is not None:
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

    # HTML'den sepet fiyatı — JSON'dan fiyat gelse bile override eder (sepet fiyatı daha düşük olabilir)
    soup = soup or BeautifulSoup(html, "html.parser")
    _basket_html_price = _extract_basket_price_html(soup, html)
    if _basket_html_price and (not price or _basket_html_price < price):
        price = _basket_html_price
        cart_discount = True

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

    # ── Sepette kampanya fiyatı — JSON fiyatından daha düşükse override ──
    soup = soup or BeautifulSoup(html, "html.parser")
    log.info(f"[trendyol] title={title} price={price} brand={brand} seller={seller} stock={stock} cart_discount={cart_discount} image={'✔ '+image_url[:60] if image_url else '✘ YOK'}")
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
    }


def _extract_basket_price_html(soup, html: str) -> Optional[float]:
    """
    HTML'den sepet/kampanya fiyatını çıkarır.
    Hem CSS selektörleri hem regex ile "Sepette X TL" / "Kampanyalı Fiyat X TL" metinlerini tarar.
    """
    # 1) CSS selektörler — Trendyol'un bilinen basket/campaign price sınıfları
    for sel in [
        "[data-testid='basket-price']",
        "[data-testid='product-campaign-info-price']",
        ".basket-price-wrapper .prc-dsc",
        ".campaign-price-wrapper .prc-dsc",
        ".campaign-price-container .prc-dsc",
        "[class*='basket-price'] .prc-dsc",
        "[class*='campaign-price'] .prc-dsc",
        "[class*='basketPrice']",
        "[class*='campaign-price'] .new-price",
        ".campaign-price-wrapper .new-price",
    ]:
        el = soup.select_one(sel)
        if el:
            p = parse_price_tr_clean(el.get_text())
            if p:
                return p

    # 2) "Sepette" veya "Kampanyalı Fiyat" yazan elementin kardeş/ebeveyn fiyatını bul
    for tag in soup.find_all(string=re.compile(r"(Sepette|Kampanyalı Fiyat|Trendyol Plus)", re.I)):
        parent = tag.parent
        for _ in range(4):  # 4 seviye yukarı çık
            if parent is None:
                break
            text = parent.get_text(" ", strip=True)
            m = re.search(r'([\d.,]{4,})\s*TL', text)
            if m:
                p = parse_price_tr_clean(m.group(1))
                if p:
                    return p
            parent = parent.parent

    # 3) Regex: "Sepette X TL" / "Kampanyalı X TL" pattern — ham HTML'de
    for pattern in [
        r'[Ss]epette\D{0,30}?([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*TL',
        r'[Kk]ampanya\w*\D{0,30}?([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*TL',
    ]:
        m = re.search(pattern, html)
        if m:
            p = parse_price_tr_clean(m.group(1))
            if p:
                return p

    return None


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

