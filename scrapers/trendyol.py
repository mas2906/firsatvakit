#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trendyol scraper — çok katmanlı yaklaşım (v3).

Katmanlar:
1) httpx (hızlı, hafif — __envoy_ JSON + og:image parse)
2) curl_cffi (Chrome TLS fingerprint — anti-bot bypass)
3) Playwright (son çare — Python 3.14'te çalışmayabilir)
"""

import re
import json
import random
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from scrapers.utils import CLOUDFLARE_TITLES, parse_price_tr_clean

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

import httpx

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


async def scrape_trendyol(url: str) -> Optional[dict]:
    """Çok katmanlı Trendyol scraper."""

    # ── Katman 1: httpx (en hızlı) ───────────────────────────
    print(f"[trendyol] httpx deneniyor...")
    data = await _via_httpx(url)
    if data and data.get("title") and data.get("price"):
        print(f"[trendyol/httpx] ✔ title={data['title']!r:.50} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
        return data

    # ── Katman 2: curl_cffi (Chrome TLS fingerprint) ─────────
    if CURL_AVAILABLE:
        print(f"[trendyol] httpx başarısız → curl_cffi deneniyor...")
        data = await _via_curl_cffi(url)
        if data and data.get("title") and data.get("price"):
            print(f"[trendyol/curl_cffi] ✔ title={data['title']!r:.50} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
            return data
    else:
        print("[trendyol] curl_cffi yüklü değil, atlanıyor")

    # ── Katman 3: Playwright (son çare) ──────────────────────
    print(f"[trendyol] curl_cffi başarısız → Playwright deneniyor...")
    data = await _via_playwright(url)
    if data and data.get("title"):
        print(f"[trendyol/playwright] ✔ title={data['title']!r:.50} price={data.get('price')} image={'✔' if data.get('image_url') else '✘'}")
        return data

    print(f"[trendyol] ✗ Tüm yöntemler başarısız: {url}")
    return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 1: httpx
# ═══════════════════════════════════════════════════════════════

async def _via_httpx(url: str) -> Optional[dict]:
    headers = {
        "User-Agent": random.choice(UA_LIST),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.trendyol.com/",
        "sec-ch-ua": '"Chromium";v="125", "Google Chrome";v="125"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)

        if r.status_code != 200:
            print(f"[trendyol/httpx] status={r.status_code}")
            return None

        html = r.text
        if _is_blocked(html):
            print("[trendyol/httpx] Cloudflare/block algılandı")
            return None

        clean_url = url.split("?")[0].split("#")[0]
        return await asyncio.to_thread(_parse, html, clean_url)
    except Exception as e:
        print(f"[trendyol/httpx] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 2: curl_cffi
# ═══════════════════════════════════════════════════════════════

async def _via_curl_cffi(url: str) -> Optional[dict]:
    try:
        impersonate = random.choice(["chrome124", "chrome123", "chrome120"])
        async with CurlSession(impersonate=impersonate) as session:
            # Warmup
            try:
                await session.get("https://www.trendyol.com/", timeout=15)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except:
                pass

            r = await session.get(url, headers={
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Referer": "https://www.trendyol.com/",
            }, timeout=25)

        if r.status_code != 200:
            print(f"[trendyol/curl_cffi] status={r.status_code}")
            return None

        html = r.text
        if _is_blocked(html):
            print("[trendyol/curl_cffi] Block algılandı")
            return None

        clean_url = url.split("?")[0].split("#")[0]
        return await asyncio.to_thread(_parse, html, clean_url)
    except Exception as e:
        print(f"[trendyol/curl_cffi] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 3: Playwright (Python 3.14'te çalışmayabilir)
# ═══════════════════════════════════════════════════════════════

async def _via_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ])
            context = await browser.new_context(
                user_agent=random.choice(UA_LIST),
                locale="tr-TR",
                viewport={"width": 1366, "height": 768},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = await context.new_page()
            await page.route("**/*.{gif,svg,ico}", lambda r: r.abort())
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(800, 2000))

            page_title = await page.title()
            if any(cf.lower() in page_title.lower() for cf in CLOUDFLARE_TITLES):
                print(f"[trendyol/playwright] Cloudflare engeli")
                await browser.close()
                return None

            html = await page.content()
            current_url = page.url.split("?")[0].split("#")[0]
            await browser.close()

        return await asyncio.to_thread(_parse, html, current_url)
    except NotImplementedError:
        print("[trendyol/playwright] Python sürümü desteklenmiyor (3.14+)")
        return None
    except Exception as e:
        print(f"[trendyol/playwright] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# BLOCK TESPİT
# ═══════════════════════════════════════════════════════════════

def _is_blocked(html: str) -> bool:
    if not html:
        return True
    t = html[:5000].lower()
    return any(m in t for m in ["cloudflare", "captcha", "challenge-platform", "just a moment", "attention required"])


def _parse(html: str, current_url: str = "") -> Optional[dict]:
    product = _extract_product_json(html)
    title = price = brand = seller = image_url = rating = reviews = stock = barcode = None
    soup = None

    if product:
        title = product.get("name")

        winner = product.get("winnerVariant", {}) or {}
        p_obj = winner.get("price", {}) or {}
        v = (p_obj.get("discountedPrice") or {}).get("value") or \
            (p_obj.get("sellingPrice") or {}).get("value")
        if v is not None:
            price = float(v)

        if price is None:
            for var in (product.get("variants", []) or []):
                vp = var.get("price") or {}
                raw = vp.get("value") if isinstance(vp, dict) else None
                if raw is None:
                    raw = (vp.get("discountedPrice") or {}).get("value")
                if raw is not None:
                    price = float(raw)
                    break

        if price is None:
            ml_price = (product.get("merchantListing") or {}).get("price") or {}
            raw = (ml_price.get("discountedPrice") or {}).get("value") or \
                  (ml_price.get("sellingPrice") or {}).get("value")
            if raw is not None:
                price = float(raw)

        barcode = _normalize_barcode(winner.get("barcode"))
        if not barcode:
            for var in (product.get("variants", []) or []):
                barcode = _normalize_barcode(var.get("barcode"))
                if barcode:
                    break

        b = product.get("brand", {}) or {}
        brand = b.get("name")

        ml = product.get("merchantListing") or {}
        merchant = ml.get("merchant") or {}
        seller = merchant.get("name")

        # ── Görsel: Çoklu fallback ──────────────────────────────
        image_url = _extract_image_from_json(product)

        rs = product.get("ratingScore") or {}
        rating = rs.get("averageRating")
        reviews = rs.get("totalCount")

        in_stock = product.get("inStock")
        if in_stock is True:
            stock = "Stokta Var"
        elif in_stock is False:
            stock = "Stok Yok"

    # Fallback: HTML'den çek
    if not title:
        soup = BeautifulSoup(html, "html.parser")
        for sel in [
            "h1.pr-new-br span", "h1.pr-new-br",
            "[data-testid='product-name']",
            "h1[class*='product-name']", "h1[class*='title']", "h1",
        ]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) > 5:
                    title = t
                    break
        if not title:
            m = re.search(r'<title[^>]*>([^<]+)</title>', html)
            if m:
                t = m.group(1).strip()
                title = t.split("|")[0].strip() or t.split("-")[0].strip()

    if not price:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in [
            "[data-testid='price-current-price']", ".prc-dsc",
            ".product-price-container span", ".prc-slg",
            "span.discounted", "p.new-price", "[class*='new-price']",
        ]:
            el = soup.select_one(sel)
            if el:
                price = parse_price_tr_clean(el.get_text())
                if price:
                    break

    if not image_url:
        soup = soup or BeautifulSoup(html, "html.parser")
        image_url = _extract_image_from_html(soup, html)

    if not brand:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in ["[data-testid='product-brand-name']", ".product-brand-name",
                    "a[href*='/marka/']", "[class*='brand']"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) < 60:
                    brand = t
                    break

    if not seller:
        soup = soup or BeautifulSoup(html, "html.parser")
        for sel in ["[data-testid='merchant-name']", ".merchant-text", "a[href*='/magaza/']"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) < 80:
                    seller = t
                    break

    if not stock:
        soup = soup or BeautifulSoup(html, "html.parser")
        body_text = soup.get_text().lower()
        if "tükendi" in body_text or "stokta yok" in body_text:
            stock = "Stok Yok"
        elif "sepete ekle" in body_text:
            stock = "Stokta Var"
        else:
            stock = "Bilinmiyor"

    if not barcode:
        barcode = _extract_barcode_regex(html)

    print(f"[trendyol] title={title} price={price} brand={brand} seller={seller} stock={stock} image={'✔ '+image_url[:60] if image_url else '✘ YOK'}")
    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "rating": rating,
        "review_count": reviews,
        "brand": brand,
        "seller": seller,
        "stock": stock,
        "barcode": barcode,
        "url": current_url,
    }


def _extract_product_json(html: str) -> Optional[dict]:
    for marker_match in re.finditer(r'window\["__envoy_[^"]+__PROPS"\]\s*=\s*\{', html):
        start = marker_match.end() - 1
        depth, end = 0, None
        limit = min(start + 800000, len(html))
        for i in range(start, limit):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            continue
        try:
            data = json.loads(html[start:end])
            product = data.get("product")
            if product and "winnerVariant" in product:
                return product
        except Exception:
            continue
    return None


def _extract_barcode_regex(html: str) -> Optional[str]:
    m = re.search(r'"winnerVariant"\s*:\s*\{[^}]*"barcode"\s*:\s*"(\d{8,14})"', html)
    if m:
        return _normalize_barcode(m.group(1))
    m = re.search(r'"barcode"\s*:\s*"(\d{8,14})"', html)
    if m:
        return _normalize_barcode(m.group(1))
    return None


def _normalize_barcode(value) -> Optional[str]:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value).strip())
    if not digits or len(digits) < 8 or len(digits) > 14:
        return None
    if len(set(digits)) == 1:
        return None
    return digits


# ═══════════════════════════════════════════════════════════════
# GÖRSEL ÇEKME — Çoklu Fallback Sistemi
# ═══════════════════════════════════════════════════════════════

def _to_full_url(path: str) -> Optional[str]:
    """Trendyol CDN path'ini tam URL'ye çevir."""
    if not path or not path.strip():
        return None
    path = path.strip()
    if path.startswith("http"):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://cdn.dsmcdn.com{path}"


def _extract_image_from_json(product: dict) -> Optional[str]:
    """JSON product objesinden görsel URL çıkar — 7 farklı path dener."""
    if not product:
        return None

    # 1) product.images — klasik yol
    images = product.get("images") or []
    if images and isinstance(images, list):
        url = _to_full_url(str(images[0]))
        if url:
            return url

    # 2) winnerVariant içindeki görseller
    winner = product.get("winnerVariant") or {}
    for key in ("images", "imageUrls", "galleryImages"):
        imgs = winner.get(key) or []
        if imgs and isinstance(imgs, list):
            url = _to_full_url(str(imgs[0]))
            if url:
                return url

    # 3) winnerVariant.image / imageUrl (tekil)
    for key in ("image", "imageUrl", "listingImage"):
        val = winner.get(key)
        if val and isinstance(val, str):
            url = _to_full_url(val)
            if url:
                return url

    # 4) galleryImages / imageUrls (üst seviye)
    for key in ("galleryImages", "imageUrls", "productImages"):
        imgs = product.get(key) or []
        if imgs and isinstance(imgs, list):
            url = _to_full_url(str(imgs[0]))
            if url:
                return url

    # 5) contentDescriptions içindeki görseller
    for desc in (product.get("contentDescriptions") or []):
        for key in ("imageUrl", "image", "url"):
            val = desc.get(key)
            if val and isinstance(val, str) and ("cdn" in val or "/" in val):
                url = _to_full_url(val)
                if url:
                    return url

    # 6) variants listesinden
    for var in (product.get("variants") or []):
        for key in ("images", "imageUrls"):
            imgs = var.get(key) or []
            if imgs and isinstance(imgs, list):
                url = _to_full_url(str(imgs[0]))
                if url:
                    return url
        for key in ("image", "imageUrl", "listingImage"):
            val = var.get(key)
            if val and isinstance(val, str):
                url = _to_full_url(val)
                if url:
                    return url

    # 7) color.imageUrl
    color = product.get("color") or {}
    val = color.get("imageUrl") or color.get("image")
    if val and isinstance(val, str):
        url = _to_full_url(val)
        if url:
            return url

    # 8) Derin arama — herhangi bir *image* key'i bul (son çare)
    url = _deep_find_image(product, depth=0)
    if url:
        return url

    return None


def _deep_find_image(obj, depth=0) -> Optional[str]:
    """JSON objesinde recursive olarak ilk görsel URL'yi bul."""
    if depth > 3:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if ("image" in kl or "img" in kl) and isinstance(v, str) and len(v) > 10:
                url = _to_full_url(v)
                if url and "cdn.dsmcdn.com" in url:
                    return url
            if ("image" in kl or "img" in kl) and isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, str) and len(first) > 5:
                    url = _to_full_url(first)
                    if url and "cdn.dsmcdn.com" in url:
                        return url
        # Daha derine in
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                result = _deep_find_image(v, depth + 1)
                if result:
                    return result
    elif isinstance(obj, list):
        for item in obj[:3]:  # İlk 3 elemanla sınırla
            result = _deep_find_image(item, depth + 1)
            if result:
                return result
    return None


def _extract_image_from_html(soup, html: str) -> Optional[str]:
    """HTML'den fallback olarak görsel URL çıkar."""

    # 1) og:image (en güvenilir)
    og = soup.find("meta", {"property": "og:image"})
    if og and og.get("content"):
        url = og["content"].strip()
        if url and len(url) > 10:
            return url

    # 2) twitter:image
    tw = soup.find("meta", {"name": "twitter:image"})
    if tw and tw.get("content"):
        url = tw["content"].strip()
        if url and len(url) > 10:
            return url

    # 3) JSON-LD structured data
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            ld = json.loads(script.string or "")
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            img = ld.get("image")
            if img:
                if isinstance(img, list) and img:
                    return str(img[0])
                if isinstance(img, str) and len(img) > 10:
                    return img
        except Exception:
            continue

    # 4) Ürün görseli CSS selektörleri (src + data-src + data-original)
    for sel in [
        "img.detail-section-img",
        "img[data-testid='product-image']",
        ".gallery-modal-content img",
        ".base-product-image img",
        "img[class*='product']",
        ".product-slide img",
        "img.ph-gl-img",
        ".slick-slide img",
        "img[class*='detail']",
        "img[class*='gallery']",
    ]:
        el = soup.select_one(sel)
        if el:
            for attr in ("src", "data-src", "data-original", "content"):
                src = el.get(attr, "")
                if src and "cdn.dsmcdn.com" in src:
                    return _to_full_url(src)

    # 5) Herhangi bir img tag'i cdn.dsmcdn.com içeren
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original"):
            src = img.get(attr, "")
            if src and "cdn.dsmcdn.com" in src and "/ty" in src:
                return _to_full_url(src)

    # 6) Regex: HTML'deki ilk cdn.dsmcdn.com görsel URL'si
    m = re.search(r'(https?://cdn\.dsmcdn\.com/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp))', html)
    if m:
        return m.group(1)

    # 7) Regex: tırnaklar arasındaki cdn path'i
    m = re.search(r'["\'](/ty\d+/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp))["\']', html)
    if m:
        return f"https://cdn.dsmcdn.com{m.group(1)}"

    return None

