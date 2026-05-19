#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import json
import asyncio
import random
import logging
from typing import Optional
from bs4 import BeautifulSoup

import httpx
from scrapers.utils import (
    UA_POOL,
    MOBILE_UA_POOL,
    MOBILE_VIEWPORTS,
    MOBILE_STEALTH_SCRIPT,
    RateLimiter,
    STEALTH_SCRIPT,
    parse_price_tr_clean,
    get_stealth_headers,
)

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CurlSession = None
    CURL_CFFI_AVAILABLE = False

log = logging.getLogger("hb_v11")

# ==========================================
# CONFIG
# ==========================================
_limiter      = RateLimiter(min_delay=3.0, max_delay=7.0)
_limiter_fast = RateLimiter(min_delay=0.4, max_delay=1.0)
_HB_LOCK = None

def _get_hb_lock() -> asyncio.Semaphore:
    global _HB_LOCK
    if _HB_LOCK is None:
        _HB_LOCK = asyncio.Semaphore(5)
    return _HB_LOCK

# HB'ye özel BrowserPool — paylaşımlı context'ten ayrı (security sayfasını önler)
_HB_POOL = None
_HB_POOL_LOCK = None

def _get_hb_pool_lock() -> asyncio.Lock:
    global _HB_POOL_LOCK
    if _HB_POOL_LOCK is None:
        _HB_POOL_LOCK = asyncio.Lock()
    return _HB_POOL_LOCK

async def _get_or_create_hb_pool():
    global _HB_POOL
    async with _get_hb_pool_lock():
        if _HB_POOL is None or not _HB_POOL._started:
            from scrapers.cdp_base import BrowserPool
            _HB_POOL = BrowserPool(max_pages=5)
            await _HB_POOL.start()
            log.info("[HB] Dedicated BrowserPool başlatıldı")
    return _HB_POOL

_CURL_SESSION = None
_CURL_LOCK = asyncio.Lock()

IMPERSONATE_POOL = ["chrome136", "chrome131", "chrome124", "chrome120"]


async def _get_curl_session():
    global _CURL_SESSION
    async with _CURL_LOCK:
        if _CURL_SESSION is None:
            imp = random.choice(IMPERSONATE_POOL)
            _CURL_SESSION = CurlSession(impersonate=imp, timeout=12)
            log.info(f"[HB] curl session oluşturuldu: {imp}")
        return _CURL_SESSION


def _reset_curl_session():
    global _CURL_SESSION
    _CURL_SESSION = None


def _parse_html_only(html: str) -> Optional[dict]:
    """Playwright gerektirmeden HTML'den parse — curl_cffi layer için."""
    soup = BeautifulSoup(html, "html.parser")
    title = price = image = None
    stock = "Bilinmiyor"
    cart_discount = False

    # 1) window.productState
    m = re.search(r'window\.productState\s*=\s*(\{)', html)
    ps = None
    if m:
        try:
            start = m.start(1)
            depth, in_str, esc, end = 0, False, False, start
            for i, ch in enumerate(html[start:], start):
                if esc: esc = False; continue
                if ch == '\\' and in_str: esc = True; continue
                if ch == '"': in_str = not in_str; continue
                if in_str: continue
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0: end = i + 1; break
            ps = json.loads(html[start:end])
            product = ps.get("product", {})
            title = product.get("name")
            price = _extract_price_from_state(ps)
            stock = _extract_stock_from_state(ps)
            image = _extract_image_from_state(ps)
        except Exception:
            pass

    # 2) __NEXT_DATA__
    if not title or not price:
        m2 = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if m2:
            try:
                j = json.loads(m2.group(1))
                txt = json.dumps(j, ensure_ascii=False)
                if not title:
                    tm = re.search(r'"name"\s*:\s*"([^"]{10,})"', txt)
                    if tm: title = tm.group(1)
                if not price:
                    price = _extract_price_from_next_data(j)
            except Exception:
                pass

    # 3) JSON-LD
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
                        elif isinstance(img, str):
                            image = img
                    if not price:
                        offers = j.get("offers", {})
                        p = offers.get("price") if isinstance(offers, dict) else None
                        if p: price = parse_price_tr_clean(str(p))
            except Exception:
                pass

    # 4) og fallbacks
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

    # Kupon ve varyantlar — zaten parse edilmiş ps'den al
    coupon = None
    variants_list = None
    if ps:
        coupon = _extract_coupon_from_state(ps)
        variants_list = _extract_variants_from_state(ps)

    return {
        "title": title, "price": price, "image_url": image,
        "stock": stock, "cart_discount": cart_discount,
        "coupon": coupon, "variants": variants_list,
    }


async def _via_curl_cffi(url: str) -> Optional[dict]:
    """Layer 0: curl_cffi (Chrome TLS fingerprint) — Playwright gerektirmez."""
    if not CURL_CFFI_AVAILABLE:
        return None
    try:
        await _limiter_fast.wait()
        session = await _get_curl_session()
        ua = random.choice(MOBILE_UA_POOL + UA_POOL)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Referer": "https://www.hepsiburada.com/",
        }
        r = await session.get(url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text
        if "security" in html[:2000].lower() or "captcha" in html[:2000].lower():
            _reset_curl_session()
            return None
        result = _parse_html_only(html)
        if result:
            log.info(f"[HB/curl] ✔ {result.get('title','')[:50]} | {result.get('price')}")
        return result
    except Exception as e:
        log.debug(f"[HB/curl] Hata: {e}")
        _reset_curl_session()
        return None


# ==========================================
# ENTRY
# ==========================================
async def scrape_hepsiburada(url: str, pool=None, price_only: bool = False) -> Optional[dict]:
    # Layer 0: curl_cffi — browser gerektirmez, ~3x hızlı
    if CURL_CFFI_AVAILABLE:
        data = await _via_curl_cffi(url)
        if data and data.get("price"):
            if price_only:
                return {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}
            return data

    # Layer 1: Playwright fallback
    async with _get_hb_lock():
        result = await _run(url, pool)
        if price_only and result:
            return {k: result[k] for k in ("price", "stock", "cart_discount", "coupon") if k in result}
        return result


# ==========================================
# CORE
# ==========================================
async def _run(url: str, pool=None):

    async def worker(page):

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        if use_mobile:
            await page.add_init_script(MOBILE_STEALTH_SCRIPT)
        else:
            await page.add_init_script(STEALTH_SCRIPT)

        await _limiter.wait()

        try:
            log.info(f"[HB V11] Açılıyor: {url}")

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000
            )

            # Fiyat elementinin DOM'a gelmesini bekle (sabit timeout yerine)
            try:
                await page.wait_for_selector(
                    '[data-test-id="default-price"], [data-test-id="checkout-price"], [data-test-id="price"]',
                    timeout=8000
                )
            except Exception:
                await page.wait_for_timeout(3500)

            html = await page.content()

            data = await _parse(page, html)

            if data:
                log.info(f"[HB V11] ✔ {data.get('title', '')[:60]} | {data.get('price')}")
                return data

            return None

        except Exception as e:
            log.error(f"[HB V10] Hata: {e}")
            return None

    use_mobile = False  # pool modunda desktop (pool kendi context'ini yönetir)

    # pool verilmemişse HB'nin dedicated pool'unu kullan (her scrape'te yeni browser açmak yerine)
    if pool is None:
        pool = await _get_or_create_hb_pool()

    if pool:
        page = await pool.acquire()
        try:
            return await worker(page)
        finally:
            await pool.release(page)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth()
        except Exception:
            _stealth = None

        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # %35 ihtimalle mobil UA kullan
        use_mobile = random.random() < 0.35
        if use_mobile:
            _ua = random.choice(MOBILE_UA_POOL)
            vw, vh, dpr = random.choice(MOBILE_VIEWPORTS)
            context = await browser.new_context(
                user_agent=_ua,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": vw, "height": vh},
                device_scale_factor=dpr,
                is_mobile=True,
                has_touch=True,
                extra_http_headers=get_stealth_headers(_ua),
            )
            log.info(f"[HB] Mobil mod: {vw}x{vh} DPR={dpr}")
        else:
            _ua = random.choice(UA_POOL)
            context = await browser.new_context(
                user_agent=_ua,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers=get_stealth_headers(_ua),
            )

        page = await context.new_page()
        if _stealth:
            await _stealth.apply_stealth_async(page)

        try:
            return await worker(page)
        finally:
            await browser.close()


# ==========================================
# PARSER (V11 - JSON-FIRST PRICE ENGINE)
# ==========================================
async def _parse(page, html: str):

    soup = BeautifulSoup(html, "html.parser")

    title = None
    image = None
    price = None
    stock = "Bilinmiyor"
    cart_discount = False

    # ==================================================
    # 0. JS page evaluation — en güvenilir yöntem
    # ==================================================
    try:
        js_data = await page.evaluate("""
            () => {
                const pricePaths = [
                    () => window.productState?.product?.price?.finalPrice,
                    () => window.productState?.product?.price?.value,
                    () => window.productState?.product?.price?.discountedPrice,
                    () => window.productState?.product?.price?.currentPrice,
                    () => window.productState?.product?.buyBoxInfo?.priceInfo?.finalPrice,
                    () => window.productState?.product?.buyBoxInfo?.price,
                    () => window.productModel?.price?.finalPrice,
                    () => window.productModel?.price?.value,
                    () => window.productModel?.price?.discountedPrice,
                    () => window.__INITIAL_STATE__?.product?.price?.finalPrice,
                    () => window.__INITIAL_STATE__?.product?.price?.value,
                ];
                let price = null;
                for (const fn of pricePaths) {
                    try {
                        const v = fn();
                        if (v && typeof v === 'number' && v > 10) { price = v; break; }
                        if (v && typeof v === 'string') {
                            const n = parseFloat(v.replace(/\\./g,'').replace(',','.'));
                            if (n > 10) { price = n; break; }
                        }
                    } catch(e) {}
                }

                // Stock: productModel veya productState'den
                let stock = null;
                const pm = window.productModel || window.productState?.product || {};
                const salable = pm.isSalable ?? pm.isSaleable ?? pm.isAvailable ?? null;
                const qty = pm.stockQty ?? pm.stock ?? null;
                if (salable === false || qty === 0) stock = 'Stok Yok';
                else if (salable === true || (typeof qty === 'number' && qty > 0)) stock = 'Stokta Var';

                // Title: productModel veya productState'den
                const title = window.productModel?.name
                    || window.productState?.product?.name
                    || window.__INITIAL_STATE__?.product?.name
                    || null;

                return { price, stock, title };
            }
        """)
        if js_data:
            if js_data.get("price"):
                price = float(js_data["price"])
            if js_data.get("stock"):
                stock = js_data["stock"]
            if js_data.get("title"):
                title = js_data["title"]
    except Exception:
        pass

    # ==================================================
    # 1. window.productState — title, image ve fiyat (HTML regex)
    # ==================================================
    # Brace-balanced extraction (lazy {.*?} nested JSON'da bozulur)
    m = re.search(r'window\.productState\s*=\s*(\{)', html)
    productState = None
    if m:
        try:
            start = m.start(1)
            depth = 0
            in_str = False
            esc = False
            end = start
            for i, ch in enumerate(html[start:], start):
                if esc:
                    esc = False
                    continue
                if ch == '\\' and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            productState = json.loads(html[start:end])
            product = productState.get("product", {})
            if not title:
                title = product.get("name")
            if not price:
                price = _extract_price_from_state(productState)
            stock = _extract_stock_from_state(productState)
            if not image:
                image = _extract_image_from_state(productState)
        except Exception:
            pass

    # ==================================================
    # 2. __NEXT_DATA__ — title, image, fiyat için
    # ==================================================
    if not title or not price:
        # Script içeriğini tag sınırına kadar al (lazy {.*?} nested JSON'u keser)
        m2 = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>',
            html
        )
        if m2:
            try:
                j = json.loads(m2.group(1))
                txt = json.dumps(j, ensure_ascii=False)
                if not title:
                    title = _find(txt, [r'"name"\s*:\s*"([^"]{10,})"', r'"title"\s*:\s*"([^"]{10,})"'])
                if not price:
                    price = _extract_price_from_next_data(j)
            except:
                pass

    # ==================================================
    # 3. JSON-LD — title, image, fiyat için
    # ==================================================
    if not title or not image:
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                j = json.loads(sc.string or sc.text)
                if isinstance(j, list):
                    j = j[0]
                if isinstance(j, dict):
                    if not title:
                        title = j.get("name")
                    if not image:
                        img = j.get("image")
                        if isinstance(img, list) and img:
                            image = img[0] if isinstance(img[0], str) else img[0].get("url")
                        elif isinstance(img, str):
                            image = img
                    if not price:
                        offers = j.get("offers", {})
                        if isinstance(offers, dict):
                            p = offers.get("price")
                            if p:
                                price = parse_price_tr_clean(str(p))
            except:
                pass

    # ==================================================
    # 3b. og:image — JSON-LD'de yoksa fallback
    # ==================================================
    if not image:
        og_img = soup.find("meta", property="og:image")
        if og_img:
            image = og_img.get("content")

    # ==================================================
    # 4. DOM PRICE — JSON bulamazsa veya sepete özel varsa
    # ==================================================
    # checkout-price elementi varsa sepete özel indirim var
    try:
        co_el = await page.query_selector('[data-test-id="checkout-price"]')
        if co_el:
            cart_discount = True
    except:
        pass

    dom_price = await _extract_dom_price(page)
    if dom_price:
        # DOM fiyatı JSON fiyatından küçükse sepete özel indirim var
        if not price or dom_price < price:
            if price and dom_price < price:
                cart_discount = True
            price = dom_price

    # ==================================================
    # 5. FALLBACK TITLE
    # ==================================================
    if not title:
        og = soup.find("meta", property="og:title")
        title = og.get("content") if og else None

    if not title and soup.title:
        raw_title = soup.title.text.strip()
        # "Hepsiburada | Güvenlik" gibi hata sayfası başlıklarını reddet
        if "Hepsiburada" not in raw_title or len(raw_title) > 30:
            title = raw_title

    # ==================================================
    # RESULT
    # ==================================================
    if title and "güvenlik" not in title.lower() and "hata" not in title.lower():
        # Kupon ve varyantlar — zaten parse edilmiş productState'den al
        coupon = None
        variants_list = None
        if productState:
            coupon = _extract_coupon_from_state(productState)
            variants_list = _extract_variants_from_state(productState)

        if not coupon:
            try:
                co_coupon = await page.query_selector('[data-test-id="merchant-coupons"]')
                if co_coupon:
                    t = (await co_coupon.inner_text()).strip()
                    if t:
                        coupon = t[:200]
            except Exception:
                pass

        log.info(f"[HB V11] title={title[:60]!r} price={price} stock={stock} cart_discount={cart_discount} coupon={'✔' if coupon else '✘'} variants={len(variants_list or [])}")
        return {
            "title": title,
            "price": price,
            "image_url": image,
            "stock": stock,
            "cart_discount": cart_discount,
            "coupon": coupon,
            "variants": variants_list,
            "platform": "Hepsiburada",
            "success": True
        }

    log.warning(f"[HB V11] Geçersiz sayfa — title={title!r}")
    return None


def _extract_price_from_state(state: dict) -> Optional[float]:
    """window.productState'den fiyat çıkar — birden fazla path dener."""
    product = state.get("product", {})

    # Doğrudan fiyat objeleri
    for path in [
        ["price", "value"],
        ["price", "finalPrice"],
        ["price", "discountedPrice"],
        ["price", "currentPrice"],
        ["buyBoxInfo", "priceInfo", "finalPrice"],
        ["buyBoxInfo", "priceInfo", "value"],
        ["buyBoxInfo", "price"],
    ]:
        d = product
        for k in path:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                d = None
                break
        if isinstance(d, (int, float)) and d > 0:
            return float(d)
        if isinstance(d, str):
            v = parse_price_tr_clean(d)
            if v:
                return v

    # promotions / campaigns
    for promo in product.get("promotions", []) or []:
        if isinstance(promo, dict):
            fp = promo.get("finalPrice") or promo.get("discountedPrice")
            if isinstance(fp, (int, float)) and fp > 0:
                return float(fp)

    return None


def _extract_image_from_state(state: dict) -> Optional[str]:
    product = state.get("product", {})
    for path in [
        ["mainImageUrl"],
        ["imageUrl"],
        ["images", 0, "url"],
        ["images", 0],
        ["image"],
        ["thumbnailUrl"],
    ]:
        d = product
        for k in path:
            if isinstance(d, dict):
                d = d.get(k)
            elif isinstance(d, list) and isinstance(k, int) and len(d) > k:
                d = d[k]
            else:
                d = None
                break
        if isinstance(d, str) and d.startswith("http"):
            return d
    return None


def _extract_stock_from_state(state: dict) -> str:
    product = state.get("product", {})
    is_salable = product.get("isSalable") or product.get("isSaleable")
    if is_salable is False:
        return "Stok Yok"
    stock_qty = product.get("stockQty") or product.get("stock")
    if isinstance(stock_qty, (int, float)):
        return "Stok Yok" if stock_qty == 0 else "Stokta Var"
    if is_salable is True:
        return "Stokta Var"
    return "Bilinmiyor"


def _extract_coupon_from_state(state: dict) -> Optional[str]:
    """window.productState'den kupon bilgisini çıkar."""
    product = state.get("product", {}) or {}
    for key in ("couponText", "couponTitle", "couponName", "merchantCoupon", "couponDescription"):
        val = product.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()[:200]
    coupons = product.get("coupons") or product.get("merchantCoupons") or []
    for c in (coupons if isinstance(coupons, list) else []):
        if isinstance(c, dict):
            t = c.get("title") or c.get("text") or c.get("name") or c.get("description") or ""
            if t:
                return str(t)[:200]
    return None


def _extract_variants_from_state(state: dict) -> Optional[list]:
    """window.productState'den varyantları çıkar."""
    product = state.get("product", {}) or {}
    raw = (product.get("variants") or product.get("listings") or
           product.get("skus") or product.get("variantList") or [])
    if not raw or not isinstance(raw, list):
        return None
    parsed = []
    seen: set = set()
    for var in raw:
        if not isinstance(var, dict):
            continue
        name_parts = []
        for attr_key in ("color", "colorName", "size", "sizeName", "attribute", "attributeValue", "description"):
            v = (var.get(attr_key) or "").strip()
            if v and v not in name_parts:
                name_parts.append(v)
        name = " / ".join(name_parts) if name_parts else (var.get("id") or var.get("sku") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        vp = None
        for pk in ("price", "finalPrice", "discountedPrice", "currentPrice"):
            pv = var.get(pk)
            if isinstance(pv, dict):
                pv = pv.get("value") or pv.get("finalPrice")
            if isinstance(pv, (int, float)) and pv > 0:
                vp = float(pv)
                break
        in_stock = not (var.get("outOfStock") or (var.get("stockQty") or 1) == 0)
        parsed.append({"name": str(name)[:100], "price": vp, "in_stock": in_stock})
        if len(parsed) >= 50:
            break
    return parsed if parsed else None


def _extract_price_from_next_data(j: dict) -> Optional[float]:
    """__NEXT_DATA__ içindeki fiyatı bul — recursive arama."""
    txt = json.dumps(j, ensure_ascii=False)
    for pattern in [
        r'"finalPrice"\s*:\s*([\d]+(?:\.\d+)?)',
        r'"discountedPrice"\s*:\s*([\d]+(?:\.\d+)?)',
        r'"currentPrice"\s*:\s*([\d]+(?:\.\d+)?)',
        r'"salePrice"\s*:\s*([\d]+(?:\.\d+)?)',
        r'"price"\s*:\s*([\d]+(?:\.\d+)?)',
    ]:
        m = re.search(pattern, txt)
        if m:
            try:
                v = float(m.group(1))
                if v > 0:
                    return v
            except:
                pass
    return None


# ==========================================
# DOM PRICE ENGINE V11
# Obfuscated class isimlerine bağımlılık kaldırıldı.
# data-test-id attributeları + geniş fallback'ler.
# ==========================================
async def _extract_dom_price(page) -> Optional[float]:

    # Kirletici elementleri DOM'dan kaldır
    try:
        await page.evaluate("""
            ['[data-test-id="see-earnings"]',
             '[data-test-id="see-earnings-tooltip"]',
             '[data-test-id="payment-options"]',
             '[data-test-id="PremiumBanner"]',
             '[data-test-id="merchant-coupons"]',
             '[data-test-id="prev-price"]'
            ].forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
        """)
    except:
        pass

    # --------------------------------------------------
    # 1. Sepete özel fiyat — checkout-price container
    # --------------------------------------------------
    try:
        el = await page.query_selector('[data-test-id="checkout-price"]')
        if el:
            text = (await el.inner_text()).strip()
            for raw in re.findall(r'([\d\.]+[,]\d{2}|\d+\.?\d*)\s*(?:TL|₺)', text):
                v = parse_price_tr_clean(raw)
                if v and v > 0:
                    log.debug(f"[HB DOM] checkout-price → {v}")
                    return v
    except:
        pass

    # --------------------------------------------------
    # 2. Normal fiyat — default-price
    # --------------------------------------------------
    try:
        el = await page.query_selector('[data-test-id="default-price"]')
        if el:
            text = (await el.inner_text()).strip().split('\n')[0]
            v = parse_price_tr_clean(text)
            if v and v > 0:
                log.debug(f"[HB DOM] default-price → {v}")
                return v
    except:
        pass

    # --------------------------------------------------
    # 3. price-box / price-value attribute'ları (alternatif)
    # --------------------------------------------------
    try:
        for sel in [
            '[data-test-id="price-value"]',
            '[data-test-id="buybox-price"]',
            '[data-test-id="price"] [data-test-id="default-price"]',
            '[class*="price-value"]',
            '[class*="priceValue"]',
            '[class*="finalPrice"]',
            '[class*="product-price"]',
            'span[itemprop="price"]',
        ]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                v = parse_price_tr_clean(text)
                if v and v > 0:
                    log.debug(f"[HB DOM] {sel} → {v}")
                    return v
    except:
        pass

    # --------------------------------------------------
    # 4. price bloğu — TL değerlerini tara (en küçük = satış fiyatı)
    # --------------------------------------------------
    try:
        price_block = await page.query_selector('[data-test-id="price"]')
        if price_block:
            text = (await price_block.inner_text()).strip()
            candidates = []
            for raw in re.findall(r'([\d\.]+[,]\d{2})', text):
                v = parse_price_tr_clean(raw)
                if v and v > 0:
                    candidates.append(v)
            if candidates:
                result = min(candidates)
                log.debug(f"[HB DOM] price-block min → {result}")
                return result
    except:
        pass

    # --------------------------------------------------
    # 5. itemprop="price" meta tag
    # --------------------------------------------------
    try:
        el = await page.query_selector('meta[itemprop="price"]')
        if el:
            content = await el.get_attribute("content")
            if content:
                v = parse_price_tr_clean(content)
                if v and v > 0:
                    log.debug(f"[HB DOM] itemprop meta → {v}")
                    return v
    except:
        pass

    log.warning("[HB DOM] Hiçbir DOM seçici fiyat bulamadı")
    return None


# ==========================================
# HELPERS
# ==========================================
def _extract_price_dict(d):
    if not isinstance(d, dict):
        return None

    keys = [
        "sortPrice",
        "priceValue",
        "discountedPrice",
        "finalPrice",
        "currentPrice",
        "salePrice",
        "price",
        "value"
    ]

    for k in keys:
        v = d.get(k)
        if v:
            return parse_price_tr_clean(str(v))

    return None


def _extract_price_text(text):

    patterns = [
        r'"sortPrice":("?[\d\.,]+"?)',
        r'"priceValue":("?[\d\.,]+"?)',
        r'"finalPrice":("?[\d\.,]+"?)',
        r'"currentPrice":("?[\d\.,]+"?)',
        r'"price":("?[\d\.,]+"?)',
        r'([\d\.\,]+)\s*(TL|₺)'
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            return parse_price_tr_clean(m.group(1))

    return None


def _find(text, patterns):
    for p in patterns:
        m = re.search(p, text, re.DOTALL)
        if m:
            return m.group(1)
    return None