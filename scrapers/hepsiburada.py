#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hepsiburada scraper

Katman mimarisi:
  0) curl_cffi iOS Safari (productState JSON embed)
  1) crawlee PlaywrightCrawler (JS render + price fallback)
"""

import asyncio
import json
import logging
import random
import re
from typing import Optional

from bs4 import BeautifulSoup

from scrapers.crawlee_base import (
    IOS_UAS, get_session, drop_session, RL,
    cb_ok, cb_fail, cb_reset, price_filter,
    crawlee_pw_scrape, _CURL_OK,
)
from scrapers.utils import parse_price_tr_clean, normalize_image_url

log = logging.getLogger("scraper.hepsiburada")

_HB_IOS_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
    "Referer":         "https://www.hepsiburada.com/",
}


def _hb_extract_json_at(html: str, start: int) -> Optional[dict]:
    depth = 0
    in_str = False
    esc = False
    end = start
    for i, ch in enumerate(html[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(html[start:end])
    except Exception:
        return None


def _hb_price_from_state(ps: dict) -> Optional[float]:
    for listing in (ps.get("product") or {}).get("listings") or []:
        if not isinstance(listing, dict):
            continue
        for key in ("salePrice", "originalPrice", "listingPrice", "price"):
            v = listing.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    p = (ps.get("product") or {}).get("price")
    if isinstance(p, (int, float)) and p > 0:
        return float(p)
    return None


def _hb_parse_html(html: str) -> Optional[dict]:
    title = price = image_url = None
    rating = review_count = None
    stock = "Bilinmiyor"
    cart_discount = False
    coupon = None

    ps = None

    # productState (eski format)
    m = re.search(r"window\.productState\s*=\s*(\{)", html)
    if m:
        ps = _hb_extract_json_at(html, m.start(1))

    # Yeni format: {"accountState":...,"productState":{...}}
    if not ps:
        m2 = re.search(r'<script[^>]*>\s*(\{"accountState"\s*:)', html)
        if m2:
            brace = html.find("{", m2.start())
            obj   = _hb_extract_json_at(html, brace)
            if obj and "productState" in obj:
                ps = obj.get("productState")

    if ps:
        try:
            product = ps.get("product") or {}
            title   = product.get("name")
            price   = _hb_price_from_state(ps)
            imgs    = product.get("images") or []
            if imgs and isinstance(imgs[0], str):
                image_url = normalize_image_url(imgs[0])

            for listing in (product.get("listings") or [])[:1]:
                if not isinstance(listing, dict):
                    continue
                orig = listing.get("originalPrice")
                sale = listing.get("salePrice")
                if (isinstance(orig, (int, float)) and orig > 10 and
                        isinstance(sale, (int, float)) and sale > 10 and
                        sale < orig * 0.90):
                    cart_discount = True
                    price = float(sale)
                break

            psd = ps.get("productStructuredData") or {}
            for g in (psd.get("@graph") or []):
                if g.get("@type") != "Product":
                    continue
                if not image_url:
                    raw_imgs = g.get("image") or []
                    if isinstance(raw_imgs, str):
                        raw_imgs = [raw_imgs]
                    if raw_imgs:
                        image_url = normalize_image_url(raw_imgs[0].split("/format:")[0])
                agg = g.get("aggregateRating") or {}
                if agg.get("ratingValue"):
                    try:
                        rating = float(agg["ratingValue"])
                    except Exception:
                        pass
                if agg.get("ratingCount"):
                    try:
                        review_count = int(agg["ratingCount"])
                    except Exception:
                        pass
                offers = g.get("offers") or {}
                if isinstance(offers, dict):
                    if offers.get("price"):
                        try:
                            p_sd = float(offers["price"])
                            if p_sd > 0:
                                price = p_sd
                                cart_discount = False
                        except Exception:
                            pass
                    avail = offers.get("availability", "")
                    if stock == "Bilinmiyor":
                        if "InStock" in avail:
                            stock = "Stokta Var"
                        elif "OutOfStock" in avail:
                            stock = "Stok Yok"
                break

            if stock == "Bilinmiyor":
                si = product.get("stockInformation") or {}
                if si.get("isInStock") is True:
                    stock = "Stokta Var"
                elif si.get("isInStock") is False:
                    stock = "Stok Yok"
        except Exception:
            pass

    # __NEXT_DATA__ fallback
    if not title or not price:
        m2 = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if m2:
            try:
                nd    = json.loads(m2.group(1))
                pp    = ((nd.get("props") or {}).get("pageProps") or {})
                pd    = pp.get("productData") or pp.get("product") or {}
                if not title:
                    title = pd.get("name") or pd.get("title")
                if not price:
                    price = parse_price_tr_clean(str(pd.get("price") or ""))
            except Exception:
                pass

    soup = BeautifulSoup(html, "lxml")

    if not title:
        for sel in ['[data-test-id="title"]', 'h1[data-test-id^="title"]', "h1.product-name"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) > 5:
                    title = t
                    break

    if not price:
        for sel in [
            '[data-test-id="price-current-price"]',
            '[data-test-id^="final-price-"]',
        ]:
            el = soup.select_one(sel)
            if el:
                v = parse_price_tr_clean(el.get_text(strip=True))
                if v and v > 0:
                    price = v
                    cart_discount = True
                    break
    if not price:
        el = soup.select_one('[data-test-id="default-price"]')
        if el:
            v = parse_price_tr_clean(el.get_text(strip=True))
            if v and v > 0:
                price = v

    if stock == "Bilinmiyor":
        for sel in ['[data-test-id="addToCart"]', '[data-test-id^="add-to-cart-button-"]']:
            if soup.select_one(sel):
                stock = "Stokta Var"
                break

    if not rating or not review_count:
        for sel in ['[data-test-id="has-review"]', '[data-test-id^="rating-"]']:
            el = soup.select_one(sel)
            if not el:
                continue
            txt = el.get_text(strip=True)
            if not rating:
                mr = re.search(r"([\d]+[.,][\d]+)", txt)
                if mr:
                    try:
                        r_val = float(mr.group(1).replace(",", "."))
                        if 0 < r_val <= 5:
                            rating = r_val
                    except Exception:
                        pass
            if not review_count:
                mc = re.search(r"\((\d[\d.]*)\)", txt)
                if mc:
                    try:
                        review_count = int(mc.group(1).replace(".", ""))
                    except Exception:
                        pass
            if rating and review_count:
                break

    if not image_url or not title:
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                j = json.loads(sc.string or sc.text)
                if isinstance(j, list):
                    j = j[0]
                if isinstance(j, dict):
                    if not title and j.get("name"):
                        title = j["name"]
                    if not image_url:
                        imgs = j.get("image") or []
                        if isinstance(imgs, str):
                            imgs = [imgs]
                        if imgs:
                            image_url = normalize_image_url(imgs[0])
                    if title and image_url:
                        break
            except Exception:
                pass

    if not price:
        return None
    return {
        "title":         title,
        "price":         price,
        "image_url":     image_url,
        "rating":        rating,
        "review_count":  review_count,
        "stock":         stock,
        "cart_discount": cart_discount,
        "coupon":        coupon,
    }


async def _hb_via_curl(url: str) -> Optional[dict]:
    if not _CURL_OK or not cb_ok("hb_curl"):
        return None
    try:
        session = await get_session("hepsiburada", "safari18_0")
        ua = random.choice(IOS_UAS)
        r  = await session.get(url, headers={**_HB_IOS_HEADERS, "User-Agent": ua},
                               allow_redirects=True, timeout=20)

        if r.status_code in (404, 410):
            return {"dead_url": True}
        if r.status_code in (403, 429, 503):
            cb_fail("hb_curl")
            drop_session("hepsiburada")
            return None
        if r.status_code != 200:
            drop_session("hepsiburada")
            return None

        html = r.text
        if not html or len(html) < 3000:
            drop_session("hepsiburada")
            return None

        final_url = str(getattr(r, "url", url))
        if final_url and final_url.rstrip("/") != url.rstrip("/"):
            fu_l = final_url.lower()
            if not any(p in fu_l for p in ("/pm-", "/p/hb", "-pm-", "/p/hbc")):
                return {"dead_url": True}

        if len(html) > 500_000:
            cb_fail("hb_curl")
            drop_session("hepsiburada")
            return None

        low = html[:5000].lower()
        if any(k in low for k in ("captcha", "cf-challenge", "just a moment", "access denied")):
            cb_fail("hb_curl")
            drop_session("hepsiburada")
            return None

        if not any(k in html.lower() for k in ("productstate", "__next_data__", "data-test-id")):
            drop_session("hepsiburada")
            return None

        result = _hb_parse_html(html)
        if result and result.get("price"):
            cb_reset("hb_curl")
            log.info(f"[hepsiburada/curl] ✔ price={result.get('price')}")
        return result
    except Exception as e:
        err = str(e)
        if "ERR_NAME_NOT_RESOLVED" in err:
            return {"dead_url": True}
        drop_session("hepsiburada")
        log.debug(f"[hepsiburada/curl] hata: {e}")
        return None


async def _hb_pw_handler(page, url: str) -> Optional[dict]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        await page.wait_for_selector(
            '[data-test-id="default-price"],[data-test-id="checkout-price"]',
            timeout=5000,
        )
    except Exception:
        await asyncio.sleep(2)

    pw_url = page.url
    if pw_url and pw_url.rstrip("/") != url.rstrip("/"):
        fu_l = pw_url.lower()
        if not any(p in fu_l for p in ("/pm-", "/p/hb", "-pm-", "/p/hbc")):
            return {"dead_url": True}

    html = await page.content()
    if len(html or "") > 500_000:
        return None
    low = (html or "")[:5000].lower()
    if any(k in low for k in ("captcha", "cf-challenge", "access denied")):
        return None

    data = _hb_parse_html(html)

    if not data or not data.get("price"):
        try:
            js_p = await page.evaluate("""
                () => {
                    const el = document.querySelector('[data-test-id="default-price"],[data-test-id="price-current-price"]');
                    return el ? el.textContent : null;
                }
            """)
            if js_p:
                v = parse_price_tr_clean(js_p)
                if v:
                    data = data or {}
                    data["price"] = v
        except Exception:
            pass

    if not data:
        return None
    if not data.get("title"):
        try:
            meta_title = await page.get_attribute('meta[property="og:title"]', "content")
            if meta_title:
                data["title"] = meta_title
        except Exception:
            pass

    if data.get("price"):
        log.info(f"[hepsiburada/pw] ✔ price={data.get('price')}")
    return data


async def scrape_hepsiburada(url: str, price_only: bool = False,
                              cached_image: Optional[str] = None, priority: bool = False) -> Optional[dict]:
    await RL["hepsiburada"].wait(priority=priority)

    data = await _hb_via_curl(url)
    if data and (data.get("dead_url") or data.get("price")):
        return price_filter(data, price_only, cached_image)

    log.info(f"[hepsiburada] curl başarısız → Playwright: {url[:60]}")
    data = await crawlee_pw_scrape(url, "hepsiburada", _hb_pw_handler, timeout=55)
    if data and data.get("dead_url"):
        return data
    return price_filter(data, price_only, cached_image) if data else None
