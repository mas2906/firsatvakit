#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Karaca (karaca.com) ürün scraper'ı.

karaca.com bir Next.js sitesi (Cloudflare arkasında ama bot engeli yok — 2026-10-01
itibarıyla hem ev IP'sinden hem VPS'ten curl_cffi ile 200 dönüyor). Ürün sayfasında
schema.org Product ld+json'u tam veri taşıyor: ad, güncel fiyat, görsel, marka, stok,
puan ve barkod (gtin). Liste (üstü çizili) fiyat RSC payload'ındaki
"analytics":{"listPrice":…,"currentPrice":…} alanında. Tarayıcıya gerek yok.

Kaldırılmış ürün: HTTP 404. dead_url ürünü VPS'te ANINDA sildirdiği için (bkz.
memory: scraper_dead_url_false_positive) 404 tek başına yetmez — ikinci bir istekle
teyit edilir.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from scrapers.crawlee_base import drop_session, get_session, price_filter

log = logging.getLogger("scraper")

_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
# Sayfada birden fazla analytics bloğu var (önerilen/benzer ürünler) — doğru blok,
# mpn'i ld+json'daki ürünün mpn'iyle eşleşen. İlk bloğu almak 149,99 TL'lik bir
# bardağa 10.999 TL liste fiyatı yazıyordu.
_ANALYTICS_RE = re.compile(
    r'"analytics"\s*:\s*\{\s*"listPrice"\s*:\s*([\d.]+)\s*,\s*"currentPrice"\s*:\s*([\d.]+)\s*,\s*"mpn"\s*:\s*"(\w+)"'
)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _first_image(img) -> Optional[str]:
    if isinstance(img, str):
        return img if img.startswith("http") else None
    if isinstance(img, list):
        return next((u for u in (_first_image(i) for i in img) if u), None)
    if isinstance(img, dict):
        return _first_image(img.get("contentUrl") or img.get("url"))
    return None


def parse_karaca_product(html: str) -> Optional[dict]:
    """Karaca ürün sayfası HTML'inden ürün verisi. Fiyat yoksa None."""
    product = None
    for m in _LD_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            product = d
            break
    if not product:
        return None

    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _num(offers.get("price"))
    if not price:
        return None

    was_price = None
    mpn = str(product.get("mpn") or "")
    for ma in _ANALYTICS_RE.finditer(html.replace('\\"', '"')):
        if mpn and ma.group(3) != mpn:
            continue
        lp, cp = _num(ma.group(1)), _num(ma.group(2))
        # Ek güvence: bloğun güncel fiyatı ld+json fiyatıyla aynı olmalı
        if lp and cp and abs(cp - price) < 0.01 and lp > price * 1.01:
            was_price = lp
        break

    brand = product.get("brand") or {}
    rating = product.get("aggregateRating") or {}
    in_stock = "instock" in str(offers.get("availability") or "").lower()
    return {
        "title":         (product.get("name") or "").strip() or None,
        "price":         price,
        "was_price":     was_price,
        "image_url":     _first_image(product.get("image")),
        "brand":         brand.get("name") if isinstance(brand, dict) else None,
        "rating":        _num(rating.get("ratingValue")),
        "review_count":  int(rating["reviewCount"]) if rating.get("reviewCount") else None,
        "stock":         "Stokta Var" if in_stock else "Stok Yok",
        "barcode":       product.get("gtin") or product.get("gtin13") or product.get("sku"),
        "cart_discount": False,
        "coupon":        None,
    }


async def _fetch(url: str):
    session = await get_session("karaca", "chrome124")
    return await session.get(url, allow_redirects=True)


async def scrape_karaca(url: str, price_only: bool = False,
                        cached_image: Optional[str] = None) -> Optional[dict]:
    try:
        r = await _fetch(url)
        if r.status_code == 404:
            # Teyit: anlık bir hata ürünü sildirmesin
            await asyncio.sleep(5)
            r = await _fetch(url)
            if r.status_code == 404:
                log.info(f"[karaca] 404 (2 kez) → dead_url: {url[:70]}")
                return {"dead_url": True}
        if r.status_code != 200:
            log.warning(f"[karaca] HTTP {r.status_code}: {url[:70]}")
            drop_session("karaca")
            return None
        if "/urun/" not in str(r.url):
            # Ürün kaldırılınca kategoriye/ana sayfaya yönlendirilirse ölü sayma —
            # sadece logla; yönlendirme kalıbına dayanan ölü-URL tespiti geçmişte
            # canlı ürünleri sildirdi.
            log.warning(f"[karaca] ürün sayfası yerine {str(r.url)[:70]} döndü")
            return None
        data = parse_karaca_product(r.text)
        if not data:
            log.warning(f"[karaca] ld+json Product/fiyat bulunamadı: {url[:70]}")
            return None
        log.info(f"[karaca] ✔ price={data['price']} was={data.get('was_price')}")
        return price_filter(data, price_only, cached_image)
    except Exception as e:
        log.warning(f"[karaca] hata: {type(e).__name__}: {e}")
        drop_session("karaca")
        return None
