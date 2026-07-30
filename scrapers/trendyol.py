#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trendyol scraper

Katman mimarisi:
  0) public.trendyol.com JSON API  (NXDOMAIN — devre dışı)
  1) curl_cffi iOS Safari HTML
  2) crawlee PlaywrightCrawler     (XHR intercept + HTML fallback)
"""

import asyncio
import json
import logging
import random
import re
from typing import Optional

from scrapers.crawlee_base import (
    IOS_UAS, get_session, drop_session, RL,
    cb_ok, cb_fail, cb_reset, price_filter,
    crawlee_pw_scrape, _CURL_OK,
)
from scrapers.utils import parse_price_tr_clean

log = logging.getLogger("scraper.trendyol")

_TY_API_PRIMARY = "https://public.trendyol.com/discovery-web-productgw-service/api/productDetail/{cid}"
_TY_BASKET_KEYS = [
    "basketPrice", "flashSalePrice", "droppedPrice", "promotionPrice",
    "basketSellingPrice", "campaignSellingPrice", "memberPrice", "loyaltyDiscountedPrice",
]


def _ty_extract_cid(url: str) -> Optional[str]:
    m = re.search(r"-p-(\d+)", url)
    return m.group(1) if m else None


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


def _ty_parse_html(html: str) -> Optional[dict]:
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


async def _ty_via_curl(url: str) -> Optional[dict]:
    if not _CURL_OK or not cb_ok("ty_curl"):
        return None
    try:
        session = await get_session("trendyol", "safari18_0")
        ua = random.choice(IOS_UAS)
        r  = await session.get(url, headers={
            "User-Agent":      ua,
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            "Referer":         "https://www.trendyol.com/",
        }, allow_redirects=True, timeout=20)

        if r.status_code in (404, 410):
            return {"dead_url": True}
        if r.status_code in (403, 429, 503):
            cb_fail("ty_curl")
            drop_session("trendyol")
            return None
        if r.status_code != 200:
            return None

        html = r.text
        if not html or len(html) < 3000:
            drop_session("trendyol")
            return None

        result = _ty_parse_html(html)
        if result and result.get("price"):
            cb_reset("ty_curl")
            log.info(f"[trendyol/curl] ✔ price={result.get('price')}")
        return result
    except Exception as e:
        drop_session("trendyol")
        log.debug(f"[trendyol/curl] hata: {e}")
        return None


async def _ty_pw_handler(page, url: str) -> Optional[dict]:
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

    actual_url = page.url
    cid = _ty_extract_cid(url)
    if cid and actual_url and f"-p-{cid}" not in actual_url and "trendyol.com" in actual_url:
        return {"dead_url": True}

    if _api_json:
        body    = _api_json[-1]
        product = (body.get("result") or {}).get("product") or body.get("product")
        if product:
            result = _ty_parse_product(product)
            if result and result.get("price"):
                log.info(f"[trendyol/pw] ✔(api) price={result.get('price')}")
                return result

    html = await page.content()
    data = _ty_parse_html(html)
    if data and data.get("price"):
        log.info(f"[trendyol/pw] ✔(html) price={data.get('price')}")
    return data


async def scrape_trendyol(url: str, price_only: bool = False,
                           cached_image: Optional[str] = None, priority: bool = False) -> Optional[dict]:
    await RL["trendyol"].wait(priority=priority)

    data = await _ty_via_curl(url)
    if data and (data.get("dead_url") or data.get("price")):
        return price_filter(data, price_only, cached_image)

    log.info(f"[trendyol] curl başarısız → Playwright: {url[:60]}")
    data = await crawlee_pw_scrape(url, "trendyol", _ty_pw_handler, timeout=48)
    return price_filter(data, price_only, cached_image) if data else None
