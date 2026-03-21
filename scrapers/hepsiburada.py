#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hepsiburada scraper — güçlendirilmiş çok katmanlı yaklaşım.

Katmanlar:
1) Dahili JSON API (SKU varsa)
2) curl_cffi (gerçek Chrome TLS fingerprint)
3) httpx (yalnızca zayıf fallback)
4) Playwright persistent context + warmup

Not:
- Bu sürüm anti-bot engellerini "garanti" aşmaz.
- Ama istek ritmi, session ısınması ve fallback zinciri iyileştirildi.
"""

import re
import json
import time
import importlib.util
import random
import asyncio
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from scrapers.utils import UA_POOL, parse_price_tr, normalize_image_url

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    print("[hepsiburada] ⚠ curl_cffi yüklü değil → pip install curl_cffi")

HTTPX_HTTP2_AVAILABLE = importlib.util.find_spec("h2") is not None
if not HTTPX_HTTP2_AVAILABLE:
    print("[hepsiburada] ⚠ h2 yüklü değil → httpx HTTP/1.1 modunda çalışacak")


HB_SEMAPHORE = asyncio.Semaphore(1)
HB_LAST_REQUEST_TS = 0.0
HB_MIN_DELAY = 2.5
HB_MAX_DELAY = 6.0
HB_STATE_DIR = Path(".hb_browser_state")
HB_STATE_DIR.mkdir(exist_ok=True)


def _hb_normalize_image(raw: str | None) -> Optional[str]:
    """Hepsiburada görsel URL'lerini normalize eder."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("http"):
        return raw
    # /s/... → productimages CDN'e çevir
    if raw.startswith("/s/"):
        return f"https://productimages.hepsiburada.net{raw}"
    return None


async def scrape_hepsiburada(url: str) -> Optional[dict]:
    await _hb_wait_turn()
    sku = _extract_sku(url)

    if sku:
        print(f"[hepsiburada] SKU={sku} → API deneniyor...")
        data = await _via_api(sku)
        if data:
            if not data.get("title"):
                data["title"] = _title_from_url(url)
            print(f"[hepsiburada/api] ✔ title={data['title']!r} price={data['price']}")
            return data

    if CURL_CFFI_AVAILABLE:
        print("[hepsiburada] API başarısız → curl_cffi deneniyor...")
        data = await _via_curl_cffi(url)
        if data:
            if not data.get("title"):
                data["title"] = _title_from_url(url)
            print(f"[hepsiburada/curl_cffi] ✔ title={data['title']!r} price={data['price']}")
            return data
    else:
        print("[hepsiburada] curl_cffi yüklü değil.")

    print("[hepsiburada] curl_cffi başarısız/yok → httpx deneniyor...")
    data = await _via_httpx(url)
    if data:
        if not data.get("title"):
            data["title"] = _title_from_url(url)
        print(f"[hepsiburada/httpx] ✔ title={data['title']!r} price={data['price']}")
        return data

    print("[hepsiburada] httpx başarısız → Playwright deneniyor...")
    data = await _via_playwright(url)
    if data:
        if not data.get("title"):
            data["title"] = _title_from_url(url)
        print(f"[hepsiburada/playwright] ✔ title={data['title']!r} price={data['price']}")
        return data

    print("[hepsiburada] ✗ Tüm yöntemler başarısız.")
    return None


def _title_from_url(url: str) -> str:
    """URL slug'ından okunabilir başlık üret. Son çare fallback."""
    path = url.split("?")[0].rstrip("/").split("/")[-1]
    # pm-HB... kısmını kaldır
    path = re.sub(r"-pm-HB[A-Z0-9]+$", "", path, flags=re.IGNORECASE)
    title = path.replace("-", " ").title()
    return title if len(title) > 5 else ""


def _extract_sku(url: str) -> Optional[str]:
    """URL'den HB SKU çıkar. Format: HBC00005HO0L2 (alfanümerik)."""
    m = re.search(r"pm-(HB[A-Z0-9]{8,})", url) or re.search(r"(HB[A-Z0-9]{8,})", url)
    return m.group(1) if m else None


async def _hb_wait_turn() -> None:
    global HB_LAST_REQUEST_TS
    async with HB_SEMAPHORE:
        now = time.monotonic()
        elapsed = now - HB_LAST_REQUEST_TS
        target_delay = random.uniform(HB_MIN_DELAY, HB_MAX_DELAY)
        wait_needed = target_delay - elapsed
        if wait_needed > 0:
            await asyncio.sleep(wait_needed)
        HB_LAST_REQUEST_TS = time.monotonic()


def _is_blocked(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    markers = [
        "hbblockandcaptcha",
        "captcha",
        "access denied",
        "forbidden",
        "/captcha/",
        "robot olmadığınızı",
        "robot olmadığınıza",
    ]
    return any(marker in t for marker in markers)


def _parse_html(soup: BeautifulSoup) -> tuple:
    title = price = image_url = rating = None

    # ── 1) __NEXT_DATA__ (en güvenilir kaynak) ──────────────
    next_data = _extract_next_data(soup)
    if next_data:
        title, price, image_url, rating = _parse_next_data(next_data)
        if title and price:
            return title, price, image_url, rating

    # ── 2) window.__INITIAL_STATE__ veya hbProductDetail ────
    for script in soup.find_all("script"):
        txt = script.string or ""
        # window.__INITIAL_STATE__
        if "__INITIAL_STATE__" in txt or "hbProductDetail" in txt:
            t, p, i, r = _parse_state_script(txt)
            title = title or t
            price = price or p
            image_url = image_url or i
            rating = rating or r

    # ── 3) JSON-LD ─────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        t, p, i, r = _extract_jsonld_product(item)
                        title = title or t
                        price = price or p
                        image_url = image_url or i
                        rating = rating or r
            elif isinstance(data, dict):
                t, p, i, r = _extract_jsonld_product(data)
                title = title or t
                price = price or p
                image_url = image_url or i
                rating = rating or r
        except Exception:
            pass

    # ── 4) HTML selektörleri (fallback) ────────────────────
    if not title:
        for sel in [
            "h1.product-name",
            "h1[data-test-id='product-name']",
            "h1[data-test-id='title']",
            "h1[itemprop='name']",
            "span[data-test-id='product-name']",
            "[class*='product-name']",
            "[class*='pdp-product-title']",
            "[class*='product-title']",
            "meta[property='og:title']",
            "meta[name='twitter:title']",
            "h1",
        ]:
            el = soup.select_one(sel)
            if el:
                t = el.get("content") if el.name == "meta" else el.get_text(strip=True)
                if t and len(t) > 3:
                    title = t
                    break

    if not price:
        for sel in [
            "[data-test-id='price-current-price']",
            "[data-test-id='default-price']",
            "[data-test-id='final-price']",
            "span[itemprop='price']",
            "[itemprop='price']",
            "[class*='price-value']",
            "[class*='currentPrice']",
            "[class*='product-price'] span",
        ]:
            el = soup.select_one(sel)
            if el:
                val = el.get("content") or el.get_text(" ", strip=True)
                price = parse_price_tr(val)
                if price and price > 0:
                    break

    if not image_url:
        for sel in [
            "img#ht-product-picture",
            "img.product-image",
            "img[itemprop='image']",
            "img[data-test-id='product-image-image']",
            "img[class*='product']",
            "meta[property='og:image']",
        ]:
            el = soup.select_one(sel)
            if el:
                if el.name == "meta":
                    raw = el.get("content")
                else:
                    raw = el.get("src") or el.get("data-src") or el.get("data-original")
                image_url = _hb_normalize_image(raw)
                if image_url:
                    break

    # ── 5) productimages.hepsiburada.net regex fallback ───
    if not image_url:
        html_text = str(soup)
        # Büyük boyutlu görseli tercih et (424-600), yoksa herhangi birini al
        m = re.search(
            r'(https://productimages\.hepsiburada\.net/s/\d+/424-600/[^\s"\'<>]+\.jpg)(?:/format:webp)?',
            html_text
        )
        if not m:
            m = re.search(
                r'(https://productimages\.hepsiburada\.net/s/\d+/\d+-\d+/[^\s"\'<>]+\.jpg)(?:/format:webp)?',
                html_text
            )
        if m:
            image_url = m.group(1)

    # ── 6) Regex fallback — fiyat HTML'de gizliyse ────────
    if not price:
        html_text = str(soup)
        m = re.search(r'"(?:price|salePrice|finalPrice|currentPrice)"[:\s]*(\d+(?:\.\d+)?)', html_text)
        if m:
            try:
                p = float(m.group(1))
                price = p / 100 if p > 100000 else p
            except:
                pass

    return title, price, image_url, rating


def _extract_next_data(soup: BeautifulSoup) -> Optional[dict]:
    """__NEXT_DATA__ script tag'inden JSON çıkar."""
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if script and script.string:
        try:
            return json.loads(script.string)
        except:
            pass
    # id olmadan da dene
    for s in soup.find_all("script"):
        txt = s.string or ""
        if '"props":' in txt and '"pageProps":' in txt and len(txt) > 500:
            try:
                return json.loads(txt)
            except:
                pass
    return None


def _parse_next_data(data: dict) -> tuple:
    """__NEXT_DATA__ JSON'undan ürün bilgisi çıkar."""
    title = price = image_url = rating = None

    try:
        # props.pageProps altında ürün verisi
        page_props = data.get("props", {}).get("pageProps", {})

        # Hepsiburada farklı key'ler kullanabiliyor
        product = (
            page_props.get("product") or
            page_props.get("productDetail") or
            page_props.get("initialProduct") or
            page_props.get("data", {}).get("product") if isinstance(page_props.get("data"), dict) else None
        )

        if not product and isinstance(page_props, dict):
            # Derin arama: herhangi bir "product" key'i
            product = _deep_find_key(page_props, ["product", "productDetail", "item"])

        if not isinstance(product, dict):
            return title, price, image_url, rating

        title = (product.get("name") or product.get("displayName") or
                 product.get("productName") or product.get("title") or
                 product.get("shortName") or product.get("fullName"))

        # Fiyat — birçok olası path
        price = _extract_hb_price(product)

        # Görsel
        images = product.get("images") or product.get("productImages") or []
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                raw = first.get("url") or first.get("src") or first.get("path") or first.get("original")
            elif isinstance(first, str):
                raw = first
            else:
                raw = None
            image_url = _hb_normalize_image(raw)

        if not image_url:
            image_url = _hb_normalize_image(
                product.get("image") or product.get("imageUrl") or product.get("mainImage")
            )

        # Rating
        rating_val = product.get("averageRating") or product.get("rating")
        if rating_val:
            try:
                rating = float(str(rating_val).replace(",", "."))
            except:
                pass

    except Exception as e:
        print(f"[hepsiburada] __NEXT_DATA__ parse hatası: {e}")

    return title, price, image_url, rating


def _extract_hb_price(product: dict) -> Optional[float]:
    """Hepsiburada JSON'undan fiyat çıkar — birçok path dener."""

    # Direkt fiyat alanları
    for key in ["salePrice", "price", "finalPrice", "currentPrice",
                "discountedPrice", "displayPrice"]:
        val = product.get(key)
        if val not in (None, "", 0):
            try:
                p = float(str(val).replace(",", "."))
                return p / 100 if p > 100000 else p
            except:
                pass

    # listings içinden
    listings = product.get("listings") or product.get("variants") or []
    if isinstance(listings, list):
        for lst in listings:
            if not isinstance(lst, dict):
                continue
            for key in ["price", "finalPrice", "salePrice", "originalPrice"]:
                val = lst.get(key)
                if val not in (None, "", 0):
                    try:
                        p = float(str(val).replace(",", "."))
                        return p / 100 if p > 100000 else p
                    except:
                        pass

    # offers altında
    offers = product.get("offers") or {}
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        for key in ["price", "lowPrice", "salePrice"]:
            val = offers.get(key)
            if val not in (None, "", 0):
                try:
                    p = float(str(val).replace(",", "."))
                    return p / 100 if p > 100000 else p
                except:
                    pass

    # priceInfo altında
    price_info = product.get("priceInfo") or product.get("priceData") or {}
    if isinstance(price_info, dict):
        for key in ["price", "salePrice", "finalPrice", "value"]:
            val = price_info.get(key)
            if val not in (None, "", 0):
                try:
                    p = float(str(val).replace(",", "."))
                    return p / 100 if p > 100000 else p
                except:
                    pass

    return None


def _parse_state_script(txt: str) -> tuple:
    """window.__INITIAL_STATE__ veya inline JSON'dan ürün bilgisi çıkar."""
    title = price = image_url = rating = None
    try:
        # JSON bloğunu bul
        for pattern in [
            r'__INITIAL_STATE__\s*=\s*(\{.+?\});',
            r'hbProductDetail\s*=\s*(\{.+?\});',
            r'"product"\s*:\s*(\{.+?\})\s*[,;]',
        ]:
            m = re.search(pattern, txt, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
                product = data.get("product") or data
                if isinstance(product, dict):
                    title = product.get("name") or product.get("displayName")
                    price = _extract_hb_price(product)
                    images = product.get("images") or []
                    if images:
                        first = images[0]
                        image_url = first.get("url") if isinstance(first, dict) else first
                    rating_val = product.get("averageRating")
                    if rating_val:
                        try: rating = float(rating_val)
                        except: pass
                    if title and price:
                        break
    except:
        pass
    return title, price, image_url, rating


def _deep_find_key(obj: dict, keys: list, depth: int = 0):
    """Dict içinde belirtilen key'lerden birini recursive bul."""
    if depth > 4:
        return None
    for k in keys:
        if k in obj and isinstance(obj[k], dict):
            return obj[k]
    for v in obj.values():
        if isinstance(v, dict):
            result = _deep_find_key(v, keys, depth + 1)
            if result:
                return result
    return None


def _extract_jsonld_product(data: dict) -> tuple:
    title = data.get("name")
    offers = data.get("offers", {})
    if isinstance(offers, list) and offers:
        offers = offers[0]
    p_val = None
    if isinstance(offers, dict):
        p_val = offers.get("price") or offers.get("lowPrice")

    price = None
    if p_val not in (None, ""):
        try:
            price = float(str(p_val).replace(",", "."))
        except Exception:
            price = parse_price_tr(str(p_val))

    image_url = data.get("image")
    if isinstance(image_url, list) and image_url:
        image_url = image_url[0]
    image_url = normalize_image_url(image_url if isinstance(image_url, str) else None)

    rating = None
    agg = data.get("aggregateRating", {})
    if isinstance(agg, dict) and agg.get("ratingValue") not in (None, ""):
        try:
            rating = float(str(agg.get("ratingValue")).replace(",", "."))
        except Exception:
            rating = None

    return title, price, image_url, rating


async def _via_api(sku: str) -> Optional[dict]:
    api_headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.hepsiburada.com/",
        "Origin": "https://www.hepsiburada.com",
        "Accept-Language": "tr-TR,tr;q=0.9",
    }
    endpoints = [
        f"https://www.hepsiburada.com/api/product/detail/listing/{sku}",
        f"https://www.hepsiburada.com/api/product/{sku}",
        f"https://www.hepsiburada.com/api/product-detail/{sku}",
    ]

    for api_url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, http2=HTTPX_HTTP2_AVAILABLE) as client:
                r = await client.get(api_url, headers=api_headers)
            if r.status_code != 200:
                continue

            data = r.json()
            product = data.get("product") or data.get("data") or data.get("result") or data
            # Nested data yapıları
            if isinstance(product, dict) and "product" in product:
                product = product["product"]
            if not isinstance(product, dict):
                continue

            title = (product.get("name") or product.get("displayName") or
                 product.get("productName") or product.get("title") or
                 product.get("shortName") or product.get("fullName"))
            price = _extract_hb_price(product)

            images = product.get("images") or product.get("productImages") or []
            image_url = None
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, dict):
                    raw = first.get("url") or first.get("src") or first.get("path") or first.get("original")
                elif isinstance(first, str):
                    raw = first
                else:
                    raw = None
                image_url = _hb_normalize_image(raw)
            if not image_url:
                image_url = _hb_normalize_image(product.get("image") or product.get("imageUrl") or product.get("mainImage"))

            rating_val = product.get("averageRating") or product.get("rating")
            try:
                rating = float(rating_val) if rating_val not in (None, "") else None
            except Exception:
                rating = None
            reviews = product.get("ratingCount") or product.get("reviewCount")

            if title and price:
                return {
                    "title": title,
                    "price": price,
                    "image_url": image_url,
                    "rating": rating,
                    "review_count": int(reviews) if reviews else None,
                }
        except Exception as e:
            print(f"[hepsiburada/api] {api_url} → {e}")

    return None


async def _via_curl_cffi(url: str) -> Optional[dict]:
    impersonate_versions = ["chrome124", "chrome123", "chrome120"]
    impersonate = random.choice(impersonate_versions)

    try:
        async with CurlSession(impersonate=impersonate) as session:
            try:
                await session.get(
                    "https://www.hepsiburada.com/",
                    headers={"Accept-Language": "tr-TR,tr;q=0.9"},
                    timeout=20,
                )
                await asyncio.sleep(random.uniform(0.8, 2.0))
            except Exception:
                pass

            r = await session.get(
                url,
                headers={
                    "Accept-Language": "tr-TR,tr;q=0.9",
                    "Referer": "https://www.hepsiburada.com/",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=30,
            )

        if r.status_code == 200 and not _is_blocked(r.text):
            html_text = r.text

            def _parse_curl():
                soup = BeautifulSoup(html_text, "html.parser")
                t, p, i, r_ = _parse_html(soup)
                if not p:
                    for pat in [
                        r'"(?:finalPrice|salePrice|currentPrice|price)":\s*"?([\d]+(?:[.,]\d+)?)"?',
                        r'data-price="([\d]+(?:[.,]\d+)?)"',
                        r'content="([\d]+(?:\.\d+)?)"[^>]*itemprop="price"',
                        r'id="offering-baseFiyat"[^>]*>\s*([\d.,]+)',
                    ]:
                        m = re.search(pat, html_text)
                        if m:
                            p = parse_price_tr(m.group(1))
                            if p:
                                break
                return t, p, i, r_, soup.find("script", id="__NEXT_DATA__") is not None

            title, price, image_url, rating, has_next = await asyncio.to_thread(_parse_curl)

            if title:
                return {
                    "title": title,
                    "price": price,
                    "image_url": image_url,
                    "rating": rating,
                    "review_count": None,
                }
            print(f"[hepsiburada/curl_cffi] Sayfa geldi ama parse edilemedi (title={title!r}, price={price}) — __NEXT_DATA__={'var' if has_next else 'yok'}")
        else:
            print(f"[hepsiburada/curl_cffi] Başarısız: status={r.status_code} blocked={_is_blocked(getattr(r, 'text', ''))}")
    except Exception as e:
        print(f"[hepsiburada/curl_cffi] Hata: {e}")

    return None


async def _via_httpx(url: str) -> Optional[dict]:
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Referer": "https://www.hepsiburada.com/",
    }

    try:
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=1)
        async with httpx.AsyncClient(
            headers=headers,
            timeout=30,
            follow_redirects=True,
            http2=HTTPX_HTTP2_AVAILABLE,
            limits=limits,
        ) as client:
            try:
                await client.get("https://www.hepsiburada.com/")
                await asyncio.sleep(random.uniform(1.0, 2.5))
            except Exception:
                pass

            r = await client.get(url)

        if r.status_code == 200 and not _is_blocked(r.text):
            html_text = r.text

            def _parse_httpx():
                soup = BeautifulSoup(html_text, "html.parser")
                t, p, i, r_ = _parse_html(soup)
                if not p:
                    for pat in [
                        r'"(?:finalPrice|salePrice|currentPrice|price)":\s*"?([\d]+(?:[.,]\d+)?)"?',
                        r'data-price="([\d]+(?:[.,]\d+)?)"',
                        r'content="([\d]+(?:\.\d+)?)"[^>]*itemprop="price"',
                    ]:
                        m = re.search(pat, html_text)
                        if m:
                            p = parse_price_tr(m.group(1))
                            if p:
                                break
                return t, p, i, r_, soup.find("script", id="__NEXT_DATA__") is not None

            title, price, image_url, rating, has_next = await asyncio.to_thread(_parse_httpx)
            if title:
                return {
                    "title": title,
                    "price": price,
                    "image_url": image_url,
                    "rating": rating,
                    "review_count": None,
                }
            print(f"[hepsiburada/httpx] Sayfa geldi ama parse edilemedi (title={title!r}, price={price}) — __NEXT_DATA__={'var' if has_next else 'yok'}")
            return None

        if r.status_code == 403 or _is_blocked(r.text):
            print("[hepsiburada/httpx] 403 / block / captcha algılandı")
        else:
            print(f"[hepsiburada/httpx] Başarısız: status={r.status_code}")
    except Exception as e:
        print(f"[hepsiburada/httpx] Hata: {type(e).__name__}: {e}")

    return None


async def _via_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth()
        except ImportError:
            _stealth = None
            print("[hepsiburada/playwright] playwright-stealth yüklü değil")

        pw_ctx = async_playwright()
        if _stealth:
            pw_ctx = _stealth.use_async(pw_ctx)

        async with pw_ctx as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--window-size=1366,768",
                ],
            )
            ctx = await browser.new_context(
                user_agent=random.choice(UA_POOL),
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "tr-TR,tr;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            page = await ctx.new_page()

            try:
                await page.goto("https://www.hepsiburada.com/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(random.randint(800, 2000))
                await page.mouse.move(random.randint(200, 800), random.randint(200, 500))
                await page.wait_for_timeout(random.randint(300, 800))
            except Exception:
                pass

            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(1200, 2800))
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
                await page.wait_for_timeout(random.randint(400, 900))
            except Exception:
                pass

            content = await page.content()
            page_title = await page.title()
            await browser.close()

            if _is_blocked(content):
                print(f"[hepsiburada/playwright] Güvenlik engeli aşılamadı (title={page_title!r})")
                return None

            _pt = page_title

            def _parse_pw():
                soup = BeautifulSoup(content, "html.parser")
                t, p, i, r_ = _parse_html(soup)
                if not p:
                    for pat in [
                        r'"(?:finalPrice|salePrice|currentPrice|price)":\s*"?([\d]+(?:[.,]\d+)?)"?',
                        r'data-price="([\d]+(?:[.,]\d+)?)"',
                        r'content="([\d]+(?:\.\d+)?)"[^>]*itemprop="price"',
                    ]:
                        m = re.search(pat, content)
                        if m:
                            p = parse_price_tr(m.group(1))
                            if p:
                                break
                if not t and _pt:
                    t2 = re.sub(r'\s*[-|]\s*.+$', '', _pt).strip()
                    if len(t2) > 5:
                        t = t2
                return t, p, i, r_, soup.find("script", id="__NEXT_DATA__") is not None

            title, price, image_url, rating, has_next = await asyncio.to_thread(_parse_pw)
            if title:
                return {
                    "title": title,
                    "price": price,
                    "image_url": image_url,
                    "rating": rating,
                    "review_count": None,
                }

            print(f"[hepsiburada/playwright] Parse edilemedi (title={title!r}, price={price}, page_title={page_title!r}) — __NEXT_DATA__={'var' if has_next else 'yok'}")
    except Exception as e:
        print(f"[hepsiburada/playwright] Hata: {type(e).__name__}: {e}")

    return None