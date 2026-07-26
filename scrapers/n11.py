#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N11 scraper

Katman mimarisi:
  1) curl_cffi: HTML çek (cookie al) → GQL dene (aynı session) → HTML parse (yedek)
  2) crawlee PlaywrightCrawler — tarayıcı GQL'i başarıyla çekebilir, response intercept
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

try:
    from camoufox.async_api import AsyncCamoufox
    _CAMOUFOX_OK = True
except ImportError:
    _CAMOUFOX_OK = False
from scrapers.utils import parse_price_tr_clean, normalize_image_url

log = logging.getLogger("scraper.n11")

# Aynı anda max 1 camoufox instance — RAM ve CPU koruması
_BROWSER_SEM: Optional[asyncio.Semaphore] = None

def _browser_sem() -> asyncio.Semaphore:
    global _BROWSER_SEM
    if _BROWSER_SEM is None:
        _BROWSER_SEM = asyncio.Semaphore(1)
    return _BROWSER_SEM

_N11_GQL_URL   = "https://www.n11.com/nss/api/graphql"
_N11_GQL_QUERY = """
query ProductDetail($contentId: Long!) {
  productDetail(contentId: $contentId) {
    displayName
    price { buyingPrice listPrice discountedPrice basketPrice hasCartDiscount }
    stock { quantity salable }
    productImageList { url order }
    ratingScore { averageScore totalCount }
    barcode
  }
}
"""


def _n11_extract_cid(url: str) -> Optional[int]:
    """URL'den contentId çıkar — 3 format desteklenir."""
    # 1) ?contentId=12345
    m = re.search(r"[?&]contentId=(\d+)", url)
    if m:
        return int(m.group(1))
    # 2) -B123456 veya -b123456 suffix
    m = re.search(r"-[Bb](\d{6,12})(?:[/?#]|$)", url)
    if m:
        return int(m.group(1))
    # 3) Path sonunda sayı (5-12 hane)
    path = url.split("?")[0].split("#")[0]
    m    = re.search(r"-(\d{5,12})$", path)
    if m:
        return int(m.group(1))
    return None


def _n11_extract_cid_from_html(html: str) -> Optional[int]:
    """HTML / JS içinden contentId çıkar (URL'de yoksa)."""
    m = re.search(r'"contentId"\s*:\s*(\d{5,12})', html)
    if m:
        return int(m.group(1))
    m = re.search(r'\bcontentId\s*[=:]\s*(\d{5,12})', html)
    if m:
        return int(m.group(1))
    m = re.search(r'data-content-id=["\'](\d{5,12})["\']', html)
    if m:
        return int(m.group(1))
    m = re.search(r'"productId"\s*:\s*(\d{5,12})', html)
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

    # Öncelik: buyingPrice → discountedPrice → basketPrice → listPrice
    price = None
    for candidate in [buying, discounted, basket, list_p]:
        if candidate and isinstance(candidate, (int, float)) and float(candidate) > 0:
            price = float(candidate)
            break

    if not price:
        return None

    # Was price: listPrice ≥ %1 yukarıda ise
    was_price = None
    if list_p and isinstance(list_p, (int, float)) and float(list_p) > 0:
        lp_val = float(list_p)
        if lp_val > price * 1.01:
            was_price = lp_val

    # Cart discount
    cart_discount = bool(price_block.get("hasCartDiscount"))
    if (basket and buying and isinstance(basket, (int, float))
            and isinstance(buying, (int, float))
            and float(basket) > 0 and float(basket) < float(buying) * 0.99):
        cart_discount = True

    # Stok
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
    """window.model JSON'dan ürün verisi çıkar — camoufox rendered HTML (>50KB, window.model var).

    target_cid: URL'den çıkarılan groupId — yanlış ürün seçimini önler.
    """
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

    # window.model = {...} — JSON sınırını bul
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

    # Tüm ürün nesnelerini topla (displayPriceFloat > 0 ve groupId olan)
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

    # target_cid varsa URL'deki ürünü seç; yoksa ilki al
    if target_cid:
        product = next((c for c in candidates if c.get("groupId") == target_cid), None)
        if not product:
            log.debug(f"[n11/model] groupId={target_cid} bulunamadı, {len(candidates)} aday var")
            product = candidates[0]
    else:
        product = candidates[0]

    # Fiyat: displayPriceFloat (kullanıcıya gösterilen fiyat) öncelikli, yoksa priceFloat
    price_raw = product.get("displayPriceFloat") or product.get("priceFloat")
    if not price_raw or float(price_raw) <= 0:
        return None
    price = float(price_raw)

    # Was price: firstListPrice veya oldPrice
    was_price = None
    for wp_key in ("firstListPrice", "oldPrice"):
        wp_val = product.get(wp_key)
        if wp_val and float(wp_val) > price * 1.01:
            was_price = float(wp_val)
            break

    # Stok
    stock_val = product.get("stock")
    stock = "Stokta Var" if (stock_val and int(float(stock_val)) > 0) else "Stok Yok"

    # Görsel: images[].path  → {0} = boyut placeholder
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
    # Önce window.model dene (camoufox rendered HTML — büyük, tam verili)
    model_result = _n11_parse_model(html, target_cid=cid)
    if model_result:
        return model_result

    soup = BeautifulSoup(html, "lxml")
    title = price = image_url = None
    was_price = None
    stock = "Bilinmiyor"
    cart_discount = False

    # ── 1) JSON-LD schema.org ─────────────────────────────────────────
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

    # ── 2) JS-embed: buyingPrice / price block ────────────────────────
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

    # ── 3) CSS seçiciler (son çare) ───────────────────────────────────
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

    # ── 4) Başlık ─────────────────────────────────────────────────────
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


async def _n11_via_curl(url: str) -> Optional[dict]:
    """HTML çek (cookie kur) → GQL dene (aynı session) → HTML parse (yedek)."""
    if not _CURL_OK or not cb_ok("n11_curl"):
        return None
    try:
        session = await get_session("n11", "safari18_0")
        ua = random.choice(IOS_UAS)

        # 1) HTML çek — Cloudflare cookie al
        r = await session.get(url, headers={
            "User-Agent":      ua,
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9",
            "Referer":         "https://www.n11.com/",
        }, allow_redirects=True, timeout=18)

        if r.status_code in (404, 410):
            return {"dead_url": True}
        if r.status_code in (403, 429, 503):
            cb_fail("n11_curl")
            drop_session("n11")
            return None
        if r.status_code != 200:
            return None

        html = r.text
        if not html or len(html) < 2000:
            return None

        # 2) contentId — URL'den veya HTML'den
        cid = _n11_extract_cid(url) or _n11_extract_cid_from_html(html)

        # 3) GQL — HTML fetch ile elde edilen cookie ile, aynı session
        if cid and cb_ok("n11_gql"):
            payload = {"query": _N11_GQL_QUERY, "variables": {"contentId": cid}}
            try:
                r_gql = await session.post(
                    _N11_GQL_URL,
                    json=payload,
                    headers={
                        "Content-Type":  "application/json",
                        "Accept":        "application/json, text/plain, */*",
                        "User-Agent":    ua,
                        "Referer":       url,
                        "Origin":        "https://www.n11.com",
                    },
                    timeout=12,
                )
                if r_gql.status_code == 200:
                    result = _n11_parse_gql(r_gql.json())
                    if result:
                        cb_reset("n11_gql")
                        log.info(f"[n11/curl+gql] ✔ cid={cid} price={result.get('price')}")
                        return result
                if r_gql.status_code in (403, 429, 503):
                    cb_fail("n11_gql")
                    log.debug(f"[n11/curl+gql] {r_gql.status_code} — Cloudflare engeli")
            except Exception as e:
                log.debug(f"[n11/curl+gql] GQL hata: {e}")

        # 4) HTML parse — N11 fiyatları HTML'e gömmüyor, genellikle None döner
        return _n11_parse_html(html)
    except Exception as e:
        drop_session("n11")
        log.debug(f"[n11/curl] hata: {e}")
        return None


async def _n11_via_browser(url: str) -> Optional[dict]:
    """camoufox: tam tarayıcı render → window.model JSON parse veya GQL intercept."""
    if not _CAMOUFOX_OK:
        log.warning("[n11/browser] camoufox kurulu değil")
        return None
    cid = _n11_extract_cid(url)

    async with _browser_sem():
        try:
            gql_result: dict = {}

            async def _on_response(response):
                if response.status != 200:
                    return
                if "graphql" not in response.url and "nss/api" not in response.url:
                    return
                try:
                    j = await response.json()
                    parsed = _n11_parse_gql(j)
                    if parsed and parsed.get("price"):
                        gql_result.update(parsed)
                        log.info(f"[n11/browser] GQL intercept ✔ price={parsed.get('price')}")
                except Exception:
                    pass

            html = ""
            async with AsyncCamoufox(headless=True) as browser:
                page = await browser.new_page()
                page.on("response", _on_response)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass

                # GQL intercept için artımlı bekleme (max 8s)
                for _ in range(8):
                    if gql_result.get("price"):
                        break
                    await asyncio.sleep(1)

                if gql_result.get("price"):
                    await page.close()
                    return gql_result

                html = await page.content()
                await page.close()

            if len(html) < 8000:
                log.debug(f"[n11/browser] Cloudflare engeli ({len(html)} byte)")
                return None

            return _n11_parse_html(html, cid=cid)
        except Exception as e:
            log.debug(f"[n11/browser] hata: {e}")
            return None


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
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # GQL için 6s bekle
    for _ in range(6):
        if gql_result.get("price"):
            return gql_result
        await asyncio.sleep(1)

    html = await page.content()
    cid = _n11_extract_cid(url)
    return _n11_parse_html(html, cid=cid)


async def scrape_n11(url: str, price_only: bool = False,
                      cached_image: Optional[str] = None) -> Optional[dict]:
    await RL["n11"].wait()

    # Katman 1: camoufox — tam tarayıcı render, Cloudflare bypass
    data = await _n11_via_browser(url)
    if data and (data.get("dead_url") or data.get("price")):
        return price_filter(data, price_only, cached_image)

    # Katman 2: Playwright (crawlee) — farklı fingerprint, camoufox başarısız olunca
    log.info(f"[n11] camoufox başarısız → Playwright: {url[:60]}")
    data = await crawlee_pw_scrape(url, "n11", _n11_pw_handler, timeout=50)
    return price_filter(data, price_only, cached_image) if data else None
