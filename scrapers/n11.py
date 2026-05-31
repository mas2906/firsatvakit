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
import time as _time
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from scrapers.utils import (
    UA_POOL, parse_price_tr_clean, normalize_image_url,
    detect_cart_discount, get_playwright_sem, CLOUDFLARE_TITLES,
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
    CurlSession = None
    CURL_CFFI_AVAILABLE = False

# N11: %38 timeout vardı — GQL 0.3-0.7s çok agresifti
# GQL API rate limit: ~60 req/dk max (N11 CDN kısıtlaması)
# Beklenen: ~20 istek/dk → 1984 ürün → ~7 tur/gün
_limiter      = RateLimiter(1.2, 2.5)   # HTML fallback (eskiden 0.8-1.8)
_limiter_fast = RateLimiter(0.6, 1.2)   # price_only (eskiden 0.4-0.9)
_limiter_gql  = RateLimiter(0.8, 2.0)   # GraphQL birincil (eskiden 0.3-0.7 — blok yiyordu!)

# ── Session havuzları — Amazon gibi paralel fingerprint ──────────
IMPERSONATE_POOL        = ["chrome136", "chrome131", "chrome124", "chrome120"]
MOBILE_IMPERSONATE_POOL = ["safari18_0", "safari17_5", "safari17_0", "safari16"]
_POOL_SIZE        = 2
_MOBILE_POOL_SIZE = 2

_SESSIONS: list = []
_SESSIONS_LOCK: Optional[asyncio.Lock] = None
_session_idx = 0

_MOBILE_SESSIONS: list = []
_MOBILE_SESSIONS_LOCK: Optional[asyncio.Lock] = None
_mobile_session_idx = 0

_N11_MOBILE_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
]

# ── Arama oturumu sayacı ─────────────────────────────────────────
_search_counter = 0
_SEARCH_RESET_EVERY = 40

# ── Circuit breaker ──────────────────────────────────────────────
_curl_block_streak   = 0
_curl_disabled_until = 0.0
_CURL_BLOCK_LIMIT    = 4
_CURL_COOLDOWN       = 90

def _curl_ok() -> bool:
    return CURL_CFFI_AVAILABLE and _time.time() > _curl_disabled_until

def _on_curl_block() -> None:
    global _curl_block_streak, _curl_disabled_until
    _curl_block_streak += 1
    if _curl_block_streak >= _CURL_BLOCK_LIMIT:
        _curl_disabled_until = _time.time() + _CURL_COOLDOWN
        _curl_block_streak = 0
        log.warning(f"[n11/curl] {_CURL_BLOCK_LIMIT} ardışık block → {_CURL_COOLDOWN}s askıya alındı")

def _on_curl_success() -> None:
    global _curl_block_streak
    _curl_block_streak = 0

# ── httpx client ─────────────────────────────────────────────────
_HTTPX_CLIENT: Optional[httpx.AsyncClient] = None
_HTTPX_LOCK: Optional[asyncio.Lock] = None


def _get_sessions_lock() -> asyncio.Lock:
    global _SESSIONS_LOCK
    if _SESSIONS_LOCK is None:
        _SESSIONS_LOCK = asyncio.Lock()
    return _SESSIONS_LOCK


def _get_mobile_lock() -> asyncio.Lock:
    global _MOBILE_SESSIONS_LOCK
    if _MOBILE_SESSIONS_LOCK is None:
        _MOBILE_SESSIONS_LOCK = asyncio.Lock()
    return _MOBILE_SESSIONS_LOCK


def _get_httpx_lock() -> asyncio.Lock:
    global _HTTPX_LOCK
    if _HTTPX_LOCK is None:
        _HTTPX_LOCK = asyncio.Lock()
    return _HTTPX_LOCK


async def _get_or_create_session() -> tuple:
    global _SESSIONS, _session_idx
    new_entry = None
    async with _get_sessions_lock():
        if len(_SESSIONS) < _POOL_SIZE:
            imp = random.choice(IMPERSONATE_POOL)
            s = CurlSession(impersonate=imp, timeout=12)
            _SESSIONS.append((s, imp))
            new_entry = (s, imp)
            log.info(f"[n11] Yeni curl session: {imp}")
        _session_idx = (_session_idx + 1) % len(_SESSIONS)
        result = _SESSIONS[_session_idx]
    if new_entry:
        s, imp = new_entry
        try:
            await s.get("https://www.n11.com/", headers={"User-Agent": random.choice(UA_POOL)}, timeout=8)
        except Exception:
            pass
    return result


def _reset_curl_session() -> None:
    global _SESSIONS, _session_idx
    if _SESSIONS:
        bad = _session_idx % len(_SESSIONS)
        _SESSIONS[bad] = None  # type: ignore
        _SESSIONS = [s for s in _SESSIONS if s is not None]


async def _get_mobile_session() -> tuple:
    global _MOBILE_SESSIONS, _mobile_session_idx
    new_entry = None
    async with _get_mobile_lock():
        if len(_MOBILE_SESSIONS) < _MOBILE_POOL_SIZE:
            imp = random.choice(MOBILE_IMPERSONATE_POOL)
            s = CurlSession(impersonate=imp, timeout=12)
            _MOBILE_SESSIONS.append((s, imp))
            new_entry = (s, imp)
            log.info(f"[n11/mobile] Yeni session: {imp}")
        _mobile_session_idx = (_mobile_session_idx + 1) % len(_MOBILE_SESSIONS)
        result = _MOBILE_SESSIONS[_mobile_session_idx]
    if new_entry:
        s, imp = new_entry
        try:
            await s.get("https://www.n11.com/", headers={"User-Agent": random.choice(_N11_MOBILE_UAS)}, timeout=8)
        except Exception:
            pass
    return result


def _reset_mobile_session() -> None:
    global _MOBILE_SESSIONS, _mobile_session_idx
    if _MOBILE_SESSIONS:
        bad = _mobile_session_idx % len(_MOBILE_SESSIONS)
        _MOBILE_SESSIONS[bad] = None  # type: ignore
        _MOBILE_SESSIONS = [s for s in _MOBILE_SESSIONS if s is not None]


async def _get_httpx_client() -> httpx.AsyncClient:
    global _HTTPX_CLIENT
    async with _get_httpx_lock():
        if _HTTPX_CLIENT is None or _HTTPX_CLIENT.is_closed:
            _HTTPX_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                limits=httpx.Limits(max_connections=6, max_keepalive_connections=3),
                follow_redirects=True,
            )
        return _HTTPX_CLIENT


def _extract_content_id(url: str) -> Optional[int]:
    """
    N11 URL'inden contentId çıkarır.
    Desteklenen formatlar:
      .../urun/some-product-B1234567      → 1234567
      .../urun/some-product?contentId=... → URL'deki ID
      .../urun/some-product-1234567       → slug sonundaki sayı (en yaygın)
      .../urun/some-product-1234567?magaza=... → query öncesi son sayı
    """
    # ?contentId= query param
    m = re.search(r'[?&]contentId=(\d+)', url)
    if m:
        return int(m.group(1))
    # URL sonunda -B{id} formatı
    m = re.search(r'-[Bb](\d{6,12})(?:[/?#]|$)', url)
    if m:
        return int(m.group(1))
    # En yaygın format: /urun/product-name-12345678 veya /urun/product-name-12345678?magaza=...
    # Query string ve fragment'ı çıkar, path'in sonundaki sayıyı al
    path = url.split('?')[0].split('#')[0]
    m = re.search(r'-(\d{5,12})$', path)
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
        client = await _get_httpx_client()
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
    window.model JS objesinden fiyat, cart_discount, stok, kupon ve varyantları çıkarır.
    Dönüş: (price_float, cart_discount, stock_wm, coupon, variants)
    """
    m = re.search(r'window\.model\s*=\s*\{', html)
    if not m:
        return None, False, None, None, None

    start = m.start() + len(m.group(0)) - 1
    depth, in_str, esc, end = 0, False, False, start
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

    try:
        model = json.loads(html[start:end])
    except Exception:
        return None, False, None, None, None

    product = model.get('product') or {}

    # 1. Liste Fiyatı (Üstü çizili olmayan ana fiyat)
    price_float = product.get('priceFloat')
    if not isinstance(price_float, (int, float)) or price_float <= 0:
        pm = model.get('productMeta') or {}
        price_float = pm.get('price')

    if not isinstance(price_float, (int, float)) or price_float <= 0:
        return None, False, None, None, None

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

    # Orijinal HTML fallback'i — SEPETTE badge'i JSON'da görünmeyince HTML'den çıkar
    if not cart_discount and ('price-badge' in html and 'SEPETTE' in html):
        cart_discount = True
        # JSON sepet fiyatı yoksa HTML'den regex ile çek
        for _pat in [
            r'price-badge[^>]*>[^<]*SEPETTE[^<]*</[^>]+>[\s\S]{0,300}?<ins[^>]*>([\d.,]+(?:\s*TL)?)',
            r'SEPETTE[\s\S]{0,400}?<ins[^>]*>([\d.,]+(?:\s*TL)?)',
            r'newPrice[\s\S]{0,200}?<ins[^>]*>([\d.,]+(?:\s*TL)?)',
        ]:
            _m = re.search(_pat, html, re.I)
            if _m:
                _bp = parse_price_tr_clean(_m.group(1))
                if _bp and 0 < _bp < price_float:
                    price_float = _bp
                    break

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

    # 4. Kupon
    coupon = None
    if coupons:
        for c in coupons:
            if isinstance(c, dict):
                t = c.get("title") or c.get("text") or c.get("name") or ""
                if t:
                    coupon = str(t)[:200]
                    break
        if not coupon:
            coupon = "Kupon mevcut"

    # 5. Varyantlar
    variants_list = None
    raw_variants = product.get('variants') or product.get('productVariants') or []
    if raw_variants:
        parsed = []
        seen: set = set()
        for var in raw_variants:
            if not isinstance(var, dict):
                continue
            attr_name = (
                var.get('attributeValue') or var.get('name') or
                var.get('value') or var.get('optionName') or ""
            ).strip()
            if not attr_name or attr_name in seen:
                continue
            seen.add(attr_name)
            vp = var.get('priceFloat') or var.get('price') or var.get('basketPrice')
            var_price = float(vp) if isinstance(vp, (int, float)) and vp > 0 else None
            in_stock = not (var.get('isOutOfStock') or var.get('outOfStock') or (var.get('stockCount') or 1) == 0)
            parsed.append({"name": attr_name[:100], "price": var_price, "in_stock": in_stock})
        if parsed:
            variants_list = parsed[:50]

    return float(price_float), cart_discount, stock_wm, coupon, variants_list


def _parse_html(html: str) -> dict:
    """Orijinal parse akışı: soup verisi window.model ile finalize edilir."""
    soup = BeautifulSoup(html, 'html.parser')
    data = _parse_soup(soup)
    wm_price, wm_cart_discount, wm_stock, wm_coupon, wm_variants = _parse_window_model(html)

    # Fiyat birleştirme: JSON daha düşükse veya CSS fiyatı yoksa JSON'u kullan.
    # SEPETTE senaryosu: window.model listPrice=4352 içerirken CSS'den newPrice=3899 geliyorsa
    # JSON fiyatı daha YÜKSEK olduğu için CSS fiyatını koruruz; sadece cart_discount flag'ini güncelle.
    if wm_price:
        css_price = data.get('price')
        if not css_price or wm_price < css_price:
            data['price'] = wm_price
            data['cart_discount'] = wm_cart_discount
        elif wm_cart_discount:
            data['cart_discount'] = True

    if wm_stock and data.get('stock') in (None, 'Bilinmiyor'):
        data['stock'] = wm_stock

    if wm_coupon:
        data['coupon'] = wm_coupon
    if wm_variants:
        data['variants'] = wm_variants

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

    # 0a2) .price-badge içindeki SEPETTE etiketi ile fiyat
    if not price:
        for badge_sel in [".price-badge", "[class*='price-badge']", "[class*='sepette']"]:
            badge = soup.select_one(badge_sel)
            if not badge:
                continue
            if "SEPETTE" not in badge.get_text(strip=True).upper():
                continue
            # Badge'in parent/grandparent'ında ins veya .newPrice ara
            _parent = badge.parent
            for _ in range(4):
                if not _parent:
                    break
                for price_sel in ["ins", ".newPrice ins", ".newPrice"]:
                    _pe = _parent.select_one(price_sel)
                    if _pe and _pe != badge:
                        _p = parse_price_tr_clean(_pe.get_text(strip=True))
                        if _p:
                            price = _p
                            cart_discount_html = True
                            break
                if price:
                    break
                _parent = _parent.parent

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
        "coupon": None, "variants": None,
    }


async def _via_mobile(url: str) -> Optional[dict]:
    """Layer 1: iOS Safari / Android Chrome TLS fingerprint — az bot tespiti."""
    if not CURL_CFFI_AVAILABLE:
        return None
    try:
        session, imp = await _get_mobile_session()
        ua = random.choice(_N11_MOBILE_UAS)
        is_ios = "iPhone" in ua or "iPad" in ua
        headers: dict = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.n11.com/",
        }
        if not is_ios:
            v = re.search(r"Chrome/(\d+)", ua)
            cv = v.group(1) if v else "136"
            headers.update({
                "sec-ch-ua": f'"Google Chrome";v="{cv}", "Chromium";v="{cv}", "Not/A)Brand";v="8"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            })
        r = await session.get(url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code == 404:
            return {"dead_url": True}
        if r.status_code in (403, 429) or r.status_code != 200:
            _reset_mobile_session()
            return None
        html = r.text
        if not html or len(html) < 3000:
            return None
        if any(t.lower() in html[:3000].lower() for t in CLOUDFLARE_TITLES):
            _reset_mobile_session()
            return None
        result = await asyncio.to_thread(lambda: _parse_html(html))
        if result and result.get("price"):
            log.info(f"[n11/mobile] ✔ ua={'iOS' if is_ios else 'Android'} {result.get('title','')[:50]} | {result.get('price')}")
        return result
    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.debug(f"[n11/mobile] Hata: {e}")
        _reset_mobile_session()
        return None


async def _via_curl_cffi(url: str) -> Optional[dict]:
    try:
        s, imp = await _get_or_create_session()
        html_text = await stream_fetch(
            s, url, json_markers=["window.model"], max_kb=1000, timeout=12,
            headers={"Referer": "https://www.n11.com/", "User-Agent": random.choice(UA_POOL)}
        )
        if not html_text or len(html_text) < 5000:
            _on_curl_block()
            _reset_curl_session()
            return None
        if any(t.lower() in html_text.lower() for t in CLOUDFLARE_TITLES):
            _on_curl_block()
            _reset_curl_session()
            return None
        result = await asyncio.to_thread(lambda: _parse_html(html_text))
        if result and result.get("price"):
            _on_curl_success()
        return result
    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.error(f"[n11/curl_cffi] Hata: {e}")
        _reset_curl_session()
        return None


async def _via_playwright_search(url: str, pool=None) -> Optional[dict]:
    """N11 arama motorunda content_id ile arama yapıp ürün sayfasını parse eder.
    Her 40 aramada bir ana sayfaya giderek oturumu yeniler."""
    global _search_counter

    content_id = _extract_content_id(url)
    if not content_id:
        log.warning(f"[n11/search] content_id çıkarılamadı, doğrudan gidiliyor")
        return await _via_playwright(url, pool=pool)

    _search_counter += 1
    do_reset = (_search_counter % _SEARCH_RESET_EVERY == 1)

    if pool is None:
        async with get_playwright_sem():
            return await _launch_playwright(url)

    page = await pool.acquire()
    try:
        from scrapers.cdp_base import setup_resource_blocking
        await setup_resource_blocking(page)
        await page.add_init_script(STEALTH_SCRIPT)

        if do_reset:
            log.info(f"[n11/search] #{_search_counter} — oturum yenileniyor (ana sayfa)")
            try:
                await page.goto("https://www.n11.com/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(random.uniform(1.5, 3.0))
            except Exception:
                pass

        search_url = f"https://www.n11.com/arama?q={content_id}"
        log.info(f"[n11/search] #{_search_counter} content_id={content_id}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        if page.is_closed():
            return None
        await asyncio.sleep(random.uniform(0.8, 1.5))

        product_href = None
        for sel in [
            ".product-item a.product-image",
            ".product-item a.product-title-link",
            ".column.a a[href*='/urun/']",
            ".prd-grid li a[href*='/urun/']",
            "a[href*='/urun/']",
        ]:
            el = await page.query_selector(sel)
            if el:
                product_href = await el.get_attribute("href")
                if product_href:
                    break

        if product_href:
            target = product_href if product_href.startswith("http") else f"https://www.n11.com{product_href}"
            log.info(f"[n11/search] Arama sonucu → {target[:80]}")
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        else:
            log.info(f"[n11/search] Arama sonucu yok, doğrudan URL deneniyor")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if page.is_closed():
            return None

        await page.mouse.wheel(0, 500)
        await asyncio.sleep(2)

        html = await page.content() if not page.is_closed() else None
        if html:
            data = _parse_html(html)
            if data and data.get("price"):
                log.info(f"[n11/search] ✔ {data.get('title','')[:50]} | {data.get('price')}")
            return data
        return None

    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err or "ERR_NAME_RESOLUTION_FAILED" in err:
            return {"dead_url": True}
        log.error(f"[n11/search] Hata: {e}")
        return None
    finally:
        await pool.release(page)


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
    async with get_playwright_sem():
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


async def scrape_n11(url: str, pool=None, price_only: bool = False,
                     cached_image: str = None) -> Optional[dict]:
    await (_limiter_fast if price_only else _limiter).wait()

    # Layer 0: GraphQL — en hızlı, Cloudflare yok, gerçek fiyat
    data = await _via_graphql(url)
    if data and data.get("price"):
        return _price_only_filter(data, price_only, cached_image)

    # Layer 1: Mobil curl (iOS Safari / Android Chrome TLS fingerprint)
    if CURL_CFFI_AVAILABLE:
        data = await _via_mobile(url)
        if data and data.get("dead_url"):
            return data
        if data and data.get("price"):
            _on_curl_success()
            return _price_only_filter(data, price_only, cached_image)

    # Layer 2: Desktop curl_cffi + window.model
    if _curl_ok():
        data = await _via_curl_cffi(url)
        if data and data.get("dead_url"):
            return data
        if data and data.get("price"):
            return _price_only_filter(data, price_only, cached_image)
    else:
        log.info(f"[n11/curl] circuit breaker aktif — {max(0, _curl_disabled_until - _time.time()):.0f}s kaldı")

    # Layer 3: Arama motoru Playwright
    result = await _via_playwright_search(url, pool=pool)
    return _price_only_filter(result, price_only, cached_image)


def _price_only_filter(data: Optional[dict], price_only: bool, cached_image: str = None) -> Optional[dict]:
    if not price_only or not data:
        return data
    result = {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}
    if cached_image:
        result["image_url"] = cached_image
    return result