#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import json
import asyncio
import random
import logging
from typing import Optional
from bs4 import BeautifulSoup

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

log = logging.getLogger("hb_v11")

# ==========================================
# CONFIG
# ==========================================
_limiter = RateLimiter(min_delay=1.2, max_delay=3.5)
HB_LOCK = asyncio.Semaphore(2)


# ==========================================
# ENTRY
# ==========================================
async def scrape_hepsiburada(url: str, pool=None, price_only: bool = False) -> Optional[dict]:
    async with HB_LOCK:
        result = await _run(url, pool)
        if price_only and result:
            return {k: result[k] for k in ("price", "stock", "cart_discount") if k in result}
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
        js_price = await page.evaluate("""
            () => {
                const paths = [
                    () => window.productState?.product?.price?.finalPrice,
                    () => window.productState?.product?.price?.value,
                    () => window.productState?.product?.price?.discountedPrice,
                    () => window.productState?.product?.price?.currentPrice,
                    () => window.productState?.product?.buyBoxInfo?.priceInfo?.finalPrice,
                    () => window.productState?.product?.buyBoxInfo?.price,
                    () => window.__INITIAL_STATE__?.product?.price?.finalPrice,
                    () => window.__INITIAL_STATE__?.product?.price?.value,
                ];
                for (const fn of paths) {
                    try {
                        const v = fn();
                        if (v && typeof v === 'number' && v > 10) return v;
                        if (v && typeof v === 'string') {
                            const n = parseFloat(v.replace(/\\./g,'').replace(',','.'));
                            if (n > 10) return n;
                        }
                    } catch(e) {}
                }
                return null;
            }
        """)
        if js_price:
            price = float(js_price)
    except Exception:
        pass

    # DEBUG: hangi window değişkenleri var, DOM fiyat elementleri ne döndürüyor
    try:
        debug_info = await page.evaluate("""
            () => {
                const win_keys = Object.keys(window).filter(k =>
                    /state|product|price|hb|data/i.test(k) && typeof window[k] === 'object'
                );
                const price_els = {};
                [
                    '[data-test-id="default-price"]',
                    '[data-test-id="checkout-price"]',
                    '[data-test-id="price"]',
                    '[data-test-id="price-value"]',
                    '[class*="price"]',
                    'span[itemprop="price"]',
                    'meta[itemprop="price"]',
                ].forEach(sel => {
                    const el = document.querySelector(sel);
                    if (el) price_els[sel] = el.tagName === 'META'
                        ? el.getAttribute('content')
                        : el.innerText?.trim().slice(0, 80);
                });
                return { win_keys, price_els };
            }
        """)
        log.warning(f"[HB DEBUG] window keys: {debug_info.get('win_keys')}")
        log.warning(f"[HB DEBUG] price elements: {debug_info.get('price_els')}")
    except Exception as e:
        log.warning(f"[HB DEBUG] evaluate hatası: {e}")

    try:
        js_title = await page.evaluate("""
            () => window.productState?.product?.name || window.__INITIAL_STATE__?.product?.name || null
        """)
        if js_title:
            title = js_title
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
        log.info(f"[HB V11] title={title[:60]!r} price={price} stock={stock} cart_discount={cart_discount}")
        return {
            "title": title,
            "price": price,
            "image_url": image,
            "stock": stock,
            "cart_discount": cart_discount,
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