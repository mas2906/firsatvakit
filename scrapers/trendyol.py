#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trendyol scraper

Katman mimarisi:
  0) public.trendyol.com JSON API  (NXDOMAIN — devre dışı)
  1) crawlee PlaywrightCrawler     (XHR intercept + HTML fallback)

curl_cffi ön-denemesi kaldırıldı: trendyol'da çoğunlukla bloklanıp Playwright'a
düşüyordu, iki katman sıralı denemek gereksiz gecikme + fazladan istek (ban
yüzeyi) ekliyordu.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from scrapers.crawlee_base import price_filter, crawlee_pw_scrape, _warmup_navigation
from scrapers.utils import parse_price_tr_clean

log = logging.getLogger("scraper.trendyol")

# Amazon/Hepsiburada'da olup Trendyol'da eksik olan engel/captcha tespiti —
# 200 OK dönse bile içerik bir bot-doğrulama sayfası olabilir. Bu tespit
# olmadan "yanmış" oturum fark edilmeden tekrar tekrar kullanılıp ban riskini
# katlıyordu.
_TY_BLOCK_MARKERS = [
    "captcha", "access denied", "just a moment", "attention required",
    "checking your browser", "unusual traffic", "güvenlik doğrulaması",
    "robot değilsiniz", "erişiminiz kısıtlandı", "cf-challenge",
]

_TY_API_PRIMARY = "https://public.trendyol.com/discovery-web-productgw-service/api/productDetail/{cid}"
_TY_BASKET_KEYS = [
    "basketPrice", "flashSalePrice", "droppedPrice", "promotionPrice",
    "basketSellingPrice", "campaignSellingPrice", "memberPrice", "loyaltyDiscountedPrice",
]


def _pv(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, dict):
        for k in ("value", "amount", "sellingPrice", "price"):
            r = _pv(v.get(k))
            if r:
                return r
    try:
        return float(v) if float(v) > 0 else None
    except Exception:
        return None


def _ty_parse_product(product: dict) -> Optional[dict]:
    title  = product.get("name")
    brand  = (product.get("brand") or {}).get("name") or product.get("brandName")
    images = product.get("images") or []
    image_url = None
    if images:
        img = images[0]
        if isinstance(img, str):
            image_url = img if img.startswith("http") else "https://cdn.dsmcdn.com" + img

    ml     = product.get("merchantListing") or {}
    winner = ml.get("winnerVariant") or product.get("winnerVariant") or {}
    p_obj  = winner.get("price") or {}
    price  = None
    cart_discount = False
    for bk in _TY_BASKET_KEYS:
        v = _pv(p_obj.get(bk))
        if v:
            price = v
            cart_discount = True
            break
    if price is None:
        v_d = _pv(p_obj.get("discountedPrice"))
        v_s = _pv(p_obj.get("sellingPrice"))
        price = min(v_d, v_s) if v_d and v_s else (v_d or v_s)

    in_stock = winner.get("inStock") or winner.get("hasStock") or product.get("inStock", True)
    rs = product.get("ratingScore") or {}
    return {
        "title":         title,
        "price":         float(price) if price else None,
        "image_url":     image_url,
        "brand":         brand,
        "rating":        rs.get("averageRating"),
        "review_count":  rs.get("totalCount"),
        "stock":         "Stokta Var" if in_stock else "Stok Yok",
        "barcode":       None,
        "cart_discount": cart_discount,
        "coupon":        None,
    }


def _ld_image(img) -> Optional[str]:
    """schema.org image alanı: str | [str] | ImageObject{contentUrl: str | [str]}"""
    if isinstance(img, str):
        return img if img.startswith("http") else None
    if isinstance(img, list):
        return next((u for u in (_ld_image(i) for i in img) if u), None)
    if isinstance(img, dict):
        return _ld_image(img.get("contentUrl") or img.get("url"))
    return None


_OG_IMAGE_RES = (
    re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"'),
    re.compile(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"'),
)
_DSMCDN_RE = re.compile(r'https://cdn\.dsmcdn\.com/(?:mnresize/\d+/\d+/)?(ty\d+/[^\s"\'<>\\]+?\.(?:jpg|jpeg|png|webp))')


def _ty_find_image(html: str) -> Optional[str]:
    """Sayfadan ana ürün görseli: og:image → ilk dsmcdn ürün görseli.
    2026-09-30: ld+json / state-blob yolları image_url'i hep None döndürüyordu →
    Ağustos sonrası eklenen 850 Trendyol ürünü görselsiz kalmıştı."""
    if not html:
        return None
    for rx in _OG_IMAGE_RES:
        m = rx.search(html)
        if m and "dsmcdn" in m.group(1):
            return m.group(1)
    m = _DSMCDN_RE.search(html)
    return ("https://cdn.dsmcdn.com/" + m.group(1)) if m else None


def _ty_parse_ldjson(html: str) -> Optional[dict]:
    """schema.org Product ld+json'dan temel ürün verisi çıkarır (referans:
    github.com/mas2906/ecom scraper'ı)."""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict) and d.get("@type") == "Product"), None)
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue

        offers = data.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _pv(offers.get("price"))
        title = (data.get("name") or "").strip()
        if not price or not title:
            continue

        brand_raw = data.get("brand") or {}
        brand = brand_raw.get("name") if isinstance(brand_raw, dict) else None
        in_stock = "instock" in str(offers.get("availability") or "").lower()

        return {
            "title": title, "price": price, "image_url": _ld_image(data.get("image")),
            "stock": "Stokta Var" if in_stock else "Stok Yok",
            "cart_discount": False, "coupon": None,
            "brand": brand, "barcode": None, "rating": None, "review_count": None,
        }
    return None


def _ty_parse_html(html: str) -> Optional[dict]:
    data = _ty_parse_html_fields(html)
    if data and not data.get("image_url"):
        data["image_url"] = _ty_find_image(html)
    return data


def _ty_parse_html_fields(html: str) -> Optional[dict]:
    # __PRODUCT_DETAIL_APP_INITIAL_STATE__
    m = re.search(r"__PRODUCT_DETAIL_APP_INITIAL_STATE__\s*=\s*\{", html)
    if m:
        try:
            brace_start = html.index("{", m.start())
            depth = 0
            end   = brace_start
            for i, ch in enumerate(html[brace_start:], brace_start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            obj     = json.loads(html[brace_start:end])
            product = obj.get("product") or {}
            title      = product.get("name") or product.get("title")
            price_info = product.get("priceInfo") or {}
            price      = _pv(price_info.get("discountedPrice") or price_info.get("price"))
            brand      = (product.get("brand") or {}).get("name")
            if title and price:
                return {
                    "title": title, "price": price, "image_url": None,
                    "stock": "Bilinmiyor", "cart_discount": False, "coupon": None,
                    "brand": brand, "barcode": None, "rating": None, "review_count": None,
                }
        except Exception:
            pass

    # __NEXT_DATA__
    m2 = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
    if m2:
        try:
            nd    = json.loads(m2.group(1))
            props = nd.get("props") or {}
            pp    = props.get("pageProps") or {}
            pd    = pp.get("productDetailPage") or pp.get("product") or {}
            pdata = pd.get("product") or pd
            title = pdata.get("name") or pdata.get("title")
            price = _pv(pdata.get("discountedPrice") or pdata.get("price"))
            if title and price:
                return {
                    "title": title, "price": price, "image_url": None,
                    "stock": "Bilinmiyor", "cart_discount": False, "coupon": None,
                    "brand": None, "barcode": None, "rating": None, "review_count": None,
                }
        except Exception:
            pass

    # schema.org ld+json — state blob'ları (__PRODUCT_DETAIL_APP_INITIAL_STATE__,
    # __NEXT_DATA__) sayfa yapısı değiştikçe kırılıyor; ld+json ise stabil ve
    # platform-bağımsız bir standart (referans: github.com/mas2906/ecom scraper'ı,
    # "detay sayfası mikro-servisleri kararsız, ld+json stabil" notu). was_price/
    # cart_discount gibi zenginleştirilmiş alanları taşımıyor ama son regex
    # fallback'ten (winnerVariant) daha güvenilir, bu yüzden ondan önce denenir.
    ld = _ty_parse_ldjson(html)
    if ld:
        return ld

    # winnerVariant regex fallback — winnerVariant bulunamazsa TÜM sayfada arama
    # yapma: sayfadaki "benzer ürünler" gibi widget'lardan alakasız bir ürünün
    # fiyatını yanlışlıkla asıl ürünün fiyatı sanabilir.
    mwv = re.search(r'"winnerVariant"\s*:\s*\{', html)
    if not mwv:
        return None
    snippet = html[mwv.start():mwv.start() + 3000]
    mp      = re.search(r'"discountedPrice"\s*:\s*\{"value"\s*:\s*([\d.]+)', snippet)
    if mp:
        try:
            price = float(mp.group(1))
            ms    = re.search(r'"inStock"\s*:\s*(true|false)', snippet)
            stock = "Var" if ms and ms.group(1) == "true" else ("Stok Yok" if ms else "Bilinmiyor")
            title = None
            mt    = re.search(r"<title>([^<]+)</title>", html)
            if mt:
                t = re.sub(r"\s*[-|]\s*(Fiyatı|Yorum|Satın Al|Trendyol).*$", "", mt.group(1), flags=re.I).strip()
                title = t if t and len(t) > 3 else None
            mb    = re.search(r'"brand"\s*:\s*\{\s*"id"\s*:\s*\d+\s*,\s*"name"\s*:\s*"([^"]+)"', html)
            brand = mb.group(1) if mb else None
            mi    = re.search(r'content="(https://cdn\.dsmcdn\.com/ty\d+/[^"]+)"', html)
            if not mi:
                m_mn  = re.search(r'https://cdn\.dsmcdn\.com/mnresize/\d+/\d+/(ty\d+/[^\s"\'<>]+)', html)
                mi_url = ("https://cdn.dsmcdn.com/" + m_mn.group(1)) if m_mn else None
            else:
                mi_url = mi.group(1)
            mbar   = re.search(r'"barcode"\s*:\s*"([^"]+)"', snippet)
            barcode = mbar.group(1) if mbar else None
            if price:
                return {
                    "title": title, "price": price, "image_url": mi_url,
                    "stock": stock, "cart_discount": False, "coupon": None,
                    "brand": brand, "barcode": barcode, "rating": None, "review_count": None,
                }
        except Exception:
            pass

    return None


async def _ty_via_api(url: str) -> Optional[dict]:
    """Katman 0: public.trendyol.com — NXDOMAIN, devre dışı."""
    return None


async def _ty_pw_handler(page, url: str) -> Optional[dict]:
    # XHR intercept listener'ı takmadan önce ısınma navigasyonu yap — aksi
    # halde ana sayfadaki alakasız istekler _on_response'a düşüp gürültü yapar.
    await _warmup_navigation(page, "https://www.trendyol.com")

    _api_json: list = []

    async def _on_response(resp):
        ru = resp.url
        if ("productDetail" in ru or "productGw" in ru) and resp.ok:
            try:
                j = await resp.json()
                if j and ((j.get("result") or {}).get("product") or j.get("product")):
                    _api_json.append(j)
            except Exception:
                pass

    page.on("response", _on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=38000)
    except Exception:
        pass
    try:
        await page.wait_for_selector(
            ".prc-dsc, [data-testid='current-price'], [class*='product-price']",
            timeout=6000,
        )
    except Exception:
        await asyncio.sleep(3)

    if _api_json:
        body    = _api_json[-1]
        product = (body.get("result") or {}).get("product") or body.get("product")
        if product:
            result = _ty_parse_product(product)
            if result and result.get("price") and not result.get("image_url"):
                try:
                    result["image_url"] = _ty_find_image(await page.content())
                except Exception:
                    pass
            if result and result.get("price"):
                log.info(f"[trendyol/pw] ✔(api) price={result.get('price')}")
                return result

    html = await page.content()
    low = (html or "")[:5000].lower()
    if any(k in low for k in _TY_BLOCK_MARKERS):
        log.warning("[trendyol/pw] engel/captcha sayfası tespit edildi")
        return None

    data = _ty_parse_html(html)
    if data and data.get("price"):
        log.info(f"[trendyol/pw] ✔(html) price={data.get('price')}")
    return data


async def scrape_trendyol(url: str, price_only: bool = False,
                           cached_image: Optional[str] = None) -> Optional[dict]:
    # RL.wait() artık çağıran (local_scraper.process_job) tarafında, dış
    # timeout'un DIŞINDA yapılıyor — bkz. crawlee_base.RL üzerindeki not.
    # timeout 48→65: isinma navigasyonu (_warmup_navigation) eklendi, ~15-20s ek sure gerekiyor
    data = await crawlee_pw_scrape(url, "trendyol", _ty_pw_handler, timeout=65)
    return price_filter(data, price_only, cached_image) if data else None
