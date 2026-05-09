#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N11.com scraper — Katmanlar (öncelik sırasıyla):
  0) GraphQL API (en hızlı, en güvenilir fiyat)
  1) curl_cffi + window.model parse
  2) Playwright (Cloudflare varsa)
"""

import re
import json
import logging
import random
import asyncio
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from scrapers.utils import (
    UA_POOL, parse_price_tr_clean, normalize_image_url,
    detect_cart_discount, PLAYWRIGHT_SEM, CLOUDFLARE_TITLES,
    STEALTH_SCRIPT, stream_fetch, RateLimiter, get_stealth_headers
)

log = logging.getLogger("n11")

# N11 Storefront GraphQL endpoint — tarayıcı DevTools > Network > graphql ile doğrula
_GQL_ENDPOINT = "https://www.n11.com/nss/api/graphql"

_GQL_QUERY = """
query ProductDetail($contentId: Long!) {
  productDetail(contentId: $contentId) {
    displayName
    price {
      buyingPrice
      listPrice
      discountedPrice
      basketPrice
      hasCartDiscount
    }
    stock {
      quantity
      salable
    }
    productImageList {
      url
      order
    }
    ratingScore {
      averageScore
      totalCount
    }
    barcode
  }
}
"""

try:
    from curl_cffi.requests import AsyncSession as CurlSession

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

_limiter      = RateLimiter(2.0, 4.0)   # tam scrape
_limiter_fast = RateLimiter(1.0, 2.0)   # price_only
_limiter_gql  = RateLimiter(0.5, 1.5)   # graphql (API — daha az yük)


def _extract_content_id(url: str) -> Optional[int]:
    """
    N11 URL'inden contentId çıkarır.
    Desteklenen formatlar:
      .../urun/some-product-B1234567      → 1234567
      .../urun/some-product?contentId=... → URL'deki ID
      .../urun/some-product-1234567       → sondaki sayı
    """
    # ?contentId= query param
    m = re.search(r'[?&]contentId=(\d+)', url)
    if m:
        return int(m.group(1))
    # URL sonunda -B{id} formatı (N11 standart)
    m = re.search(r'-[Bb](\d{6,12})(?:[/?#]|$)', url)
    if m:
        return int(m.group(1))
    # URL sonunda düz sayı (en az 6 hane)
    m = re.search(r'/(\d{6,12})(?:[/?#]|$)', url)
    if m:
        return int(m.group(1))
    return None


def _parse_graphql_response(data: dict) -> Optional[dict]:
    """GraphQL yanıtından standart scraper dict üretir."""
    try:
        detail = data["data"]["productDetail"]
    except (KeyError, TypeError):
        return None

    if not detail:
        return None

    price_block = detail.get("price") or {}
    stock_block = detail.get("stock") or {}
    rating_block = detail.get("ratingScore") or {}
    images = detail.get("productImageList") or []

    # Fiyat — öncelik: basketPrice > discountedPrice > buyingPrice > listPrice
    raw_price = (
        price_block.get("basketPrice")
        or price_block.get("discountedPrice")
        or price_block.get("buyingPrice")
        or price_block.get("listPrice")
    )
    if not raw_price or not isinstance(raw_price, (int, float)) or raw_price <= 0:
        return None

    price = float(raw_price)

    # Sepet indirimi
    basket_price = price_block.get("basketPrice")
    list_price = price_block.get("listPrice") or price_block.get("buyingPrice") or 0
    has_cart_discount_flag = price_block.get("hasCartDiscount") or False
    cart_discount = bool(
        has_cart_discount_flag
        or (basket_price and list_price and float(basket_price) < float(list_price))
    )

    # Stok
    qty = stock_block.get("quantity")
    salable = stock_block.get("salable")
    if salable is False or (isinstance(qty, (int, float)) and qty == 0):
        stock = "Stok Yok"
    elif salable is True or (isinstance(qty, (int, float)) and qty > 0):
        stock = "Stokta Var"
    else:
        stock = "Bilinmiyor"

    # Resim — order=0 olan önce
    image_url = None
    if images:
        sorted_imgs = sorted(images, key=lambda x: x.get("order", 99))
        image_url = normalize_image_url(sorted_imgs[0].get("url"))

    return {
        "title": detail.get("displayName"),
        "price": price,
        "image_url": image_url,
        "rating": round(float(rating_block["averageScore"]) / 20, 1) if rating_block.get("averageScore") else None,
        "review_count": rating_block.get("totalCount"),
        "stock": stock,
        "barcode": detail.get("barcode"),
        "cart_discount": cart_discount,
    }


async def _via_graphql(url: str) -> Optional[dict]:
    """Layer 0: N11 GraphQL API — en temiz ve güvenilir fiyat kaynağı."""
    content_id = _extract_content_id(url)
    if not content_id:
        log.debug("[n11/graphql] contentId çıkarılamadı: %s", url)
        return None

    await _limiter_gql.wait()

    ua = random.choice(UA_POOL)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": ua,
        "Referer": url,
        "Origin": "https://www.n11.com",
        "Accept-Language": "tr-TR,tr;q=0.9",
        "x-requested-with": "XMLHttpRequest",
    }
    payload = {
        "query": _GQL_QUERY,
        "variables": {"contentId": content_id},
    }

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.post(_GQL_ENDPOINT, json=payload, headers=headers)

        if resp.status_code != 200:
            log.warning("[n11/graphql] HTTP %d — contentId=%s", resp.status_code, content_id)
            return None

        body = resp.json()
        if "errors" in body:
            log.warning("[n11/graphql] GQL hata: %s", body["errors"])
            return None

        result = _parse_graphql_response(body)
        if result:
            log.debug("[n11/graphql] OK — contentId=%s fiyat=%.2f", content_id, result["price"])
        return result

    except Exception as e:
        log.warning("[n11/graphql] İstek hatası: %s", e)
        return None


def _parse_window_model(html: str) -> tuple:
    """
    window.model JS objesinden fiyat, cart_discount ve stok çıkarır.
    DÜZELTME: personalizedData altındaki gerçek sepet fiyatı hiyerarşisi eklendi.
    """
    m = re.search(r'window\.model\s*=\s*\{', html)
    if not m:
        return None, False, None

    start = m.start() + len(m.group(0)) - 1
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        model = json.loads(html[start:end])
    except Exception:
        return None, False, None

    product = model.get('product') or {}

    # 1. Liste Fiyatı (Üstü çizili olmayan ana fiyat)
    price_float = product.get('priceFloat')
    if not isinstance(price_float, (int, float)) or price_float <= 0:
        pm = model.get('productMeta') or {}
        price_float = pm.get('price')

    if not isinstance(price_float, (int, float)) or price_float <= 0:
        return None, False, None

    # 2. Sepet İndirimi Tespiti (Geliştirilmiş Mantık)
    pd_data = product.get('personalizedData') or {}
    pd_product = pd_data.get('product') or {}

    # N11'de gerçek sepet fiyatı: önce product üstünde, sonra personalizedData altında ara
    basket_price = (
        product.get('basketPrice')
        or product.get('campaignPrice')
        or pd_product.get('basketPrice')
        or pd_product.get('finalPrice')
    )
    final_badge = pd_product.get('finalPriceBadge') or product.get('finalPriceBadge')
    coupons = pd_data.get('productCoupons') or []

    cart_discount = False

    # Eğer sepet fiyatı varsa ve liste fiyatından gerçekten düşükse indirim vardır
    if basket_price and isinstance(basket_price, (int, float)) and 0 < basket_price < price_float:
        price_float = basket_price
        cart_discount = True

    # Badge 'SEPETTE' ise veya kupon tanımlıysa fiyattan bağımsız işaretle
    if (final_badge and "SEPETTE" in str(final_badge).upper()) or bool(coupons):
        cart_discount = True

    # Orijinal HTML fallback'i
    if not cart_discount and ('price-badge' in html and 'SEPETTE' in html):
        cart_discount = True

    # 3. Stok (Orijinal hiyerarşi korunarak)
    stock_wm = None
    stock_count = product.get('stockCount')
    if isinstance(stock_count, (int, float)):
        stock_wm = "Stok Yok" if stock_count == 0 else "Stokta Var"

    if product.get('isOutOfStock') or product.get('outOfStock'):
        stock_wm = "Stok Yok"

    is_salable = product.get('isSalable') or product.get('isSalesable') or product.get('sellable')
    if is_salable is False:
        stock_wm = "Stok Yok"
    elif is_salable is True and stock_wm is None:
        stock_wm = "Stokta Var"

    return float(price_float), cart_discount, stock_wm


def _parse_html(html: str) -> dict:
    """Orijinal parse akışı: soup verisi window.model ile finalize edilir."""
    soup = BeautifulSoup(html, 'html.parser')
    data = _parse_soup(soup)
    wm_price, wm_cart_discount, wm_stock = _parse_window_model(html)

    # JSON verisi CSS'den daha güvenilirdir, çelişki varsa JSON kazanır
    if wm_price:
        if wm_cart_discount or not data.get('price') or wm_price < (data.get('price') or 999999):
            data['price'] = wm_price
            data['cart_discount'] = wm_cart_discount

    if wm_stock and data.get('stock') in (None, 'Bilinmiyor'):
        data['stock'] = wm_stock
    return data


def _parse_soup(soup: BeautifulSoup) -> dict:
    """Tüm orijinal CSS seçicilerini koruyan Soup parse işlemi."""
    # Başlık
    title = None
    for sel in ["h1.title.max-three-lines", "h1.title", "h1.proName", "h1"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 5 and not t.startswith("Ürün Bil"):
                title = t
                break

    # Fiyat & Sepet İndirimi
    price = None
    cart_discount_html = False

    # 0a) N11 SEPETTE Badge ve Fiyat
    _pw = soup.select_one(".price-wrapper")
    if _pw:
        pw_text = _pw.get_text(strip=True).upper()
        if "SEPETTE" in pw_text:
            _ni = _pw.select_one(".newPrice ins, .newPrice, ins")
            if _ni:
                price = parse_price_tr_clean(_ni.get_text(strip=True))
                cart_discount_html = True

    # 0b) Sepette indirimli fiyat (Orijinal alternatif seçiciler)
    if not price:
        for sel in [
            "[class*='basket-campaign'] ins", "[class*='basketPrice'] ins",
            ".basket-campaign-area ins", "[class*='sepetteIndirim'] ins",
        ]:
            el = soup.select_one(sel)
            if el:
                p = parse_price_tr_clean(el.get_text(strip=True))
                if p:
                    price = p
                    cart_discount_html = True
                    break

    # 1) Standart CSS Fiyat Seçicileri
    if not price:
        for sel in [".product-summary ins", ".price-wrapper ins", "ins.newPrice"]:
            el = soup.select_one(sel)
            if el:
                price = parse_price_tr_clean(el.get_text(strip=True))
                if price: break

    # Resim (Orijinal CDN filtreli)
    image_url = None
    for sel in ["img.swiper-image-0", "img#selectedProductImg", "meta[property='og:image']"]:
        el = soup.select_one(sel)
        if el:
            raw = el.get("src") or el.get("content") or el.get("data-src")
            url_candidate = normalize_image_url(raw)
            if url_candidate and "akamaized.net" in url_candidate:
                image_url = url_candidate
                break

    # Rating & Review
    rating = None
    stars_el = soup.select_one(".product-summary div.stars")
    if stars_el:
        style = stars_el.get("style", "")
        m = re.search(r"--rating\s*:\s*([\d.]+)", style)
        if m: rating = round(float(m.group(1)) / 20, 1)

    review_count = None
    for sel in [".ratingCount", "[class*='reviewCount']"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"(\d+)", el.get_text())
            if m: review_count = int(m.group(1))
            break

    # Stok
    stock = "Bilinmiyor"
    if soup.select_one(".outOfStock, .unf-oor, .outOfStockArea"):
        stock = "Stok Yok"
    elif soup.select_one(".addToCart, .addToBasket, .addBasket"):
        stock = "Stokta Var"

    # Barkod
    barcode = None
    bc_box = soup.select_one("#barcode-box strong")
    if bc_box:
        bc = re.sub(r"[^\d]", "", bc_box.get_text(strip=True))
        if 8 <= len(bc) <= 14: barcode = bc

    # Kupon/Sepet tespiti (Orijinal utils çağrıları)
    cart_discount = cart_discount_html or detect_cart_discount(str(soup))

    return {
        "title": title, "price": price, "image_url": image_url,
        "rating": rating, "review_count": review_count, "stock": stock,
        "barcode": barcode, "cart_discount": cart_discount,
    }


async def _via_curl_cffi(url: str) -> Optional[dict]:
    try:
        async with CurlSession(impersonate=random.choice(["chrome124", "chrome120"])) as s:
            html_text = await stream_fetch(
                s, url, json_markers=["window.model"], max_kb=1000, timeout=12,
                headers={"Referer": "https://www.n11.com/"}
            )
        if not html_text or len(html_text) < 5000: return None
        if any(t.lower() in html_text.lower() for t in CLOUDFLARE_TITLES): return None

        return await asyncio.to_thread(lambda: _parse_html(html_text))
    except Exception as e:
        log.error(f"[n11/curl_cffi] Hata: {e}")
        return None


async def _via_playwright(url: str, pool=None) -> Optional[dict]:
    if pool:
        page = await pool.acquire()
        try:
            from scrapers.cdp_base import setup_resource_blocking
            await setup_resource_blocking(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.mouse.wheel(0, 500)  # JS render'ı tetikle
            await asyncio.sleep(2)  # N11 Vue SSR için kritik bekleme
            html = await page.content()
            return _parse_html(html)
        finally:
            await pool.release(page)
    async with PLAYWRIGHT_SEM:
        return await _launch_playwright(url)


async def _launch_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth()
        except Exception:
            _stealth = None

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            _ua = random.choice(UA_POOL)
            ctx = await browser.new_context(
                user_agent=_ua,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
                extra_http_headers=get_stealth_headers(_ua),
            )
            await ctx.add_init_script(STEALTH_SCRIPT)
            page = await ctx.new_page()
            if _stealth:
                await _stealth.apply_stealth_async(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.mouse.wheel(0, 500)
            await asyncio.sleep(2)
            content = await page.content()
            await browser.close()
            return _parse_html(content)
    except Exception as e:
        log.error(f"[n11/playwright] Hata: {e}")
    return None


async def scrape_n11(url: str, pool=None, price_only: bool = False) -> Optional[dict]:
    # Layer 0: GraphQL — en hızlı, Cloudflare yok, gerçek fiyat
    data = await _via_graphql(url)
    if data and data.get("price"):
        return _price_only_filter(data, price_only)

    # Layer 1: curl_cffi + window.model
    await (_limiter_fast if price_only else _limiter).wait()
    if CURL_CFFI_AVAILABLE:
        data = await _via_curl_cffi(url)
        if data:
            return _price_only_filter(data, price_only)

    # Layer 2: Playwright
    result = await _via_playwright(url, pool=pool)
    return _price_only_filter(result, price_only)


def _price_only_filter(data: Optional[dict], price_only: bool) -> Optional[dict]:
    if not price_only or not data:
        return data
    return {k: data[k] for k in ("price", "stock", "cart_discount") if k in data}