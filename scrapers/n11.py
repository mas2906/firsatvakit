#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N11 scraper — Playwright (crawlee) tabanlı, GQL intercept + HTML parse."""

import asyncio
import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from scrapers.crawlee_base import RL, price_filter, crawlee_pw_scrape
from scrapers.utils import parse_price_tr_clean, normalize_image_url

log = logging.getLogger("scraper.n11")


def _n11_extract_cid(url: str) -> Optional[int]:
    """URL'den contentId çıkar — 3 format desteklenir."""
    m = re.search(r"[?&]contentId=(\d+)", url)
    if m:
        return int(m.group(1))
    m = re.search(r"-[Bb](\d{6,12})(?:[/?#]|$)", url)
    if m:
        return int(m.group(1))
    path = url.split("?")[0].split("#")[0]
    m    = re.search(r"-(\d{5,12})$", path)
    if m:
        return int(m.group(1))
    return None


def _n11_parse_gql(data: dict) -> Optional[dict]:
    try:
        detail = data["data"]["productDetail"]
    except (KeyError, TypeError):
        return None
    if not detail:
        return None

    price_block  = detail.get("price") or {}
    stock_block  = detail.get("stock") or {}
    rating_block = detail.get("ratingScore") or {}
    images       = detail.get("productImageList") or []

    buying     = price_block.get("buyingPrice")
    discounted = price_block.get("discountedPrice")
    basket     = price_block.get("basketPrice")
    list_p     = price_block.get("listPrice")

    price = None
    for candidate in [buying, discounted, basket, list_p]:
        if candidate and isinstance(candidate, (int, float)) and float(candidate) > 0:
            price = float(candidate)
            break

    if not price:
        return None

    was_price = None
    if list_p and isinstance(list_p, (int, float)) and float(list_p) > 0:
        lp_val = float(list_p)
        if lp_val > price * 1.01:
            was_price = lp_val

    cart_discount = bool(price_block.get("hasCartDiscount"))
    if (basket and buying and isinstance(basket, (int, float))
            and isinstance(buying, (int, float))
            and float(basket) > 0 and float(basket) < float(buying) * 0.99):
        cart_discount = True

    qty     = stock_block.get("quantity")
    salable = stock_block.get("salable")
    if salable is False or (isinstance(qty, (int, float)) and qty == 0):
        stock = "Stok Yok"
    elif salable is True or (isinstance(qty, (int, float)) and qty > 0):
        stock = "Stokta Var"
    else:
        stock = "Bilinmiyor"

    image_url = None
    if images:
        sorted_imgs = sorted(images, key=lambda x: x.get("order", 99))
        image_url   = normalize_image_url(sorted_imgs[0].get("url"))

    avg_score = rating_block.get("averageScore")
    return {
        "title":         detail.get("displayName"),
        "price":         price,
        "was_price":     was_price,
        "image_url":     image_url,
        "rating":        round(float(avg_score) / 20, 1) if avg_score else None,
        "review_count":  rating_block.get("totalCount"),
        "stock":         stock,
        "barcode":       detail.get("barcode"),
        "cart_discount": cart_discount,
    }


def _n11_parse_model(html: str, target_cid: Optional[int] = None) -> Optional[dict]:
    """window.model JSON'dan ürün verisi çıkar."""
    if "window.model" not in html or "displayPriceFloat" not in html:
        return None

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    target = next(
        (s for s in sorted(scripts, key=len, reverse=True)
         if "window.model" in s and "displayPriceFloat" in s),
        None,
    )
    if not target:
        return None

    wm_pos = target.find("window.model")
    eq_pos = target.find("=", wm_pos) + 1
    depth = 0
    in_str = esc = False
    end_pos = eq_pos
    for i, c in enumerate(target[eq_pos:], eq_pos):
        if esc:
            esc = False; continue
        if c == "\\" and in_str:
            esc = True; continue
        if c == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_pos = i + 1; break

    try:
        model = json.loads(target[eq_pos:end_pos].strip())
    except Exception:
        return None

    candidates: list = []

    def _collect(obj, d=0):
        if d > 10:
            return
        if isinstance(obj, dict):
            if obj.get("displayPriceFloat", 0) > 0 and obj.get("groupId"):
                candidates.append(obj)
            for v in obj.values():
                _collect(v, d + 1)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item, d + 1)

    _collect(model)

    if not candidates:
        return None

    if target_cid:
        product = next((c for c in candidates if c.get("groupId") == target_cid), None)
        if not product:
            log.debug(f"[n11/model] groupId={target_cid} bulunamadı, {len(candidates)} aday var")
            product = candidates[0]
    else:
        product = candidates[0]

    price_raw = product.get("displayPriceFloat") or product.get("priceFloat")
    if not price_raw or float(price_raw) <= 0:
        return None
    price = float(price_raw)

    was_price = None
    for wp_key in ("firstListPrice", "oldPrice"):
        wp_val = product.get(wp_key)
        if wp_val and float(wp_val) > price * 1.01:
            was_price = float(wp_val)
            break

    stock_val = product.get("stock")
    stock = "Stokta Var" if (stock_val and int(float(stock_val)) > 0) else "Stok Yok"

    images = product.get("images") or []
    image_url = None
    if images:
        raw_path = images[0].get("path", "")
        image_url = raw_path.replace("{0}", "600_800") if raw_path else None

    log.info(f"[n11/model] ✔ title={product.get('title','?')[:40]} price={price}")
    return {
        "title":        product.get("title"),
        "price":        price,
        "was_price":    was_price,
        "image_url":    image_url,
        "stock":        stock,
        "cart_discount": False,
        "barcode":      None,
        "rating":       None,
        "review_count": None,
    }


def _n11_parse_html(html: str, cid: Optional[int] = None) -> Optional[dict]:
    model_result = _n11_parse_model(html, target_cid=cid)
    if model_result:
        return model_result

    soup = BeautifulSoup(html, "lxml")
    title = price = image_url = None
    was_price = None
    stock = "Bilinmiyor"
    cart_discount = False

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(script.string or "")
            if isinstance(j, list):
                j = j[0]
            if j.get("@type") in ("Product", "product"):
                title  = j.get("name")
                offers = j.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0]
                if offers.get("price"):
                    price = parse_price_tr_clean(str(offers["price"]))
                avail = (offers.get("availability") or "").lower()
                if "instock" in avail:
                    stock = "Stokta Var"
                elif "outofstock" in avail:
                    stock = "Stok Yok"
                img_list = j.get("image") or []
                if isinstance(img_list, str):
                    img_list = [img_list]
                image_url = normalize_image_url(img_list[0] if img_list else None)
                break
        except Exception:
            pass

    if not price:
        for script in soup.find_all("script"):
            js = script.string or ""
            if "buyingPrice" not in js:
                continue
            m = re.search(r'"buyingPrice"\s*:\s*([\d.]+)', js)
            if not m:
                continue
            try:
                p = float(m.group(1))
                if p <= 0:
                    continue
                price = p
                m2 = re.search(r'"listPrice"\s*:\s*([\d.]+)', js)
                if m2:
                    lp = float(m2.group(1))
                    if lp > price * 1.01:
                        was_price = lp
                if '"hasCartDiscount":true' in js or '"hasCartDiscount": true' in js:
                    cart_discount = True
                if '"salable":true' in js or '"salable": true' in js:
                    stock = "Stokta Var"
                elif '"salable":false' in js or '"salable": false' in js:
                    stock = "Stok Yok"
                break
            except Exception:
                continue

    if not price:
        for sel in [
            ".newPrice ins", ".newPrice span", ".newPrice",
            ".price-container .onnpStdPrice",
            "[itemprop='price']",
            ".price ins", ".unf-p-price ins",
            "[class*='currentPrice']", "[class*='sale-price']",
        ]:
            el = soup.select_one(sel)
            if el:
                v = parse_price_tr_clean(el.get_text(strip=True))
                if v and v > 0:
                    price = v
                    break

    if not title:
        for sel in ["h1.proName", "h1[itemprop='name']", "h1.product-name", "h1"]:
            h1 = soup.select_one(sel)
            if h1:
                t = h1.get_text(strip=True)[:300]
                if t and len(t) > 3:
                    title = t
                    break

    if price is None:
        return None
    return {
        "title":        title,
        "price":        price,
        "was_price":    was_price,
        "image_url":    image_url,
        "stock":        stock,
        "cart_discount": cart_discount,
        "barcode":      None,
        "rating":       None,
        "review_count": None,
    }


async def _n11_pw_handler(page, url: str) -> Optional[dict]:
    """Playwright handler: GQL intercept + HTML parse fallback."""
    gql_result: dict = {}

    async def _on_response(resp):
        if resp.status != 200:
            return
        if "graphql" not in resp.url and "nss/api" not in resp.url:
            return
        try:
            j = await resp.json()
            parsed = _n11_parse_gql(j)
            if parsed and parsed.get("price"):
                gql_result.update(parsed)
                log.info(f"[n11/pw] GQL intercept ✔ price={parsed.get('price')}")
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass

    for _ in range(4):
        if gql_result.get("price"):
            return gql_result
        await asyncio.sleep(1)

    html = await page.content()
    cid = _n11_extract_cid(url)
    return _n11_parse_html(html, cid=cid)


async def scrape_n11(url: str, price_only: bool = False,
                      cached_image: Optional[str] = None, priority: bool = False) -> Optional[dict]:
    await RL["n11"].wait(priority=priority)
    data = await crawlee_pw_scrape(url, "n11", _n11_pw_handler, timeout=30)
    return price_filter(data, price_only, cached_image) if data else None
