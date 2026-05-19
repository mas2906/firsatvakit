#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon.com.tr scraper — v5 (global session reuse).

Katmanlar:
1) curl_cffi — global persistent CurlSession, TLS handshake 0
2) Playwright (son çare)
"""

import os
import re
import logging
import json
import random
import asyncio
from typing import Optional
from bs4 import BeautifulSoup
from scrapers.utils import (
    UA_POOL, RateLimiter,
    detect_cart_discount, get_playwright_sem, STEALTH_SCRIPT,
    parse_price_tr_clean, get_stealth_headers,
)

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CurlSession = None
    CURL_AVAILABLE = False

log = logging.getLogger("amazon")

_limiter      = RateLimiter(min_delay=0.0, max_delay=0.05)
_limiter_fast = RateLimiter(min_delay=0.0, max_delay=0.02)

# ── Cookie cache ─────────────────────────────────────────────────
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "amazon_cookies.json")
_cached_cookies: dict | None = None
_cookie_file_mtime: float | None = None


def _load_amazon_cookies() -> dict:
    global _cached_cookies, _cookie_file_mtime
    try:
        path = os.path.abspath(_COOKIE_FILE)
        mtime = os.path.getmtime(path)
        if _cached_cookies is not None and _cookie_file_mtime == mtime:
            return _cached_cookies
        with open(path, "r", encoding="utf-8") as f:
            cookies_list = json.load(f)
        _cached_cookies = {c["name"]: c["value"] for c in cookies_list if "name" in c and "value" in c}
        _cookie_file_mtime = mtime
        return _cached_cookies
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug(f"[amazon] cookie yüklenemedi: {e}")
        return {}


# ── Global session ───────────────────────────────────────────────
IMPERSONATE_POOL = ["chrome146", "chrome142", "chrome136", "chrome131", "chrome124", "chrome120"]

_SESSION: Optional[CurlSession] = None
_SESSION_LOCK = asyncio.Lock()


async def _get_or_create_session() -> CurlSession:
    global _SESSION
    async with _SESSION_LOCK:
        if _SESSION is None:
            imp = random.choice(IMPERSONATE_POOL)
            _SESSION = CurlSession(impersonate=imp, timeout=20)
            cookies = _load_amazon_cookies()
            if cookies:
                _SESSION.cookies.update(cookies)
            log.info(f"[amazon] Yeni session: {imp}" + (f", {len(cookies)} çerez" if cookies else ""))
        return _SESSION


def _reset_session() -> None:
    global _SESSION
    _SESSION = None


# ── Block/not-found detection ────────────────────────────────────
CAPTCHA_HINTS = [
    "robot check", "validatecaptcha", "type the characters",
    "enter the characters", "not a robot", "güvenlik kontrolü",
    "lütfen aşağıdaki karakterleri girin", "captcha",
]
BLOCK_HINTS = [
    "üzgünüz", "isteğinizi işlemeye", "sorun üzerinde çalışıyoruz",
    "something went wrong", "we're sorry", "api-services-support@amazon",
]
NOT_FOUND_HINTS = [
    "sitemizde işlev gösteren bir sayfaya karşılık gelmiyor",
    "aradığınız bir şey mi var",
    "sayfa bulunamadı",
    "dogs of amazon",
]


def _is_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    t = html[:10000].lower()
    return any(h in t for h in CAPTCHA_HINTS + BLOCK_HINTS)


def _is_not_found(html: str) -> bool:
    if not html:
        return False
    t = html[:5000].lower()
    return any(h in t for h in NOT_FOUND_HINTS)


# ═══════════════════════════════════════════════════════════════
# ANA FONKSİYON
# ═══════════════════════════════════════════════════════════════

async def scrape_amazon(url: str, pool=None, price_only: bool = False) -> Optional[dict]:
    await (_limiter_fast if price_only else _limiter).wait()

    if CURL_AVAILABLE:
        last_data = None
        for attempt in range(2):
            data = await _via_curl(url)
            if data and data.get("not_found"):
                _limiter.reset()
                return data
            if data and data.get("title") and data.get("price"):
                log.info(f"[amazon/curl] ✔ title={data['title'][:50]!r} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
                return _price_only_filter(data, price_only)
            if data and data.get("title"):
                last_data = data
                if data.get("stock") == "Stok Yok":
                    break
            if attempt == 0:
                backoff = random.uniform(1.0, 2.5)
                log.info(f"[amazon/curl] Deneme {attempt+1} başarısız → {backoff:.1f}s backoff")
                await asyncio.sleep(backoff)

        if last_data:
            log.info(f"[amazon/curl] title var fiyat yok → Playwright atlanıyor (stock={last_data.get('stock')})")
            return _price_only_filter(last_data, price_only)
    else:
        log.warning("[amazon] curl_cffi yüklü değil")

    log.info("[amazon] → Playwright deneniyor...")
    data = await _via_playwright(url, pool=pool)
    if data and data.get("not_found"):
        _limiter.reset()
        return data
    if data and data.get("title"):
        log.info(f"[amazon/playwright] ✔ title={data['title'][:50]!r} price={data.get('price')} image={'✔' if data.get('image_url') else '✘'}")
        return _price_only_filter(data, price_only)

    log.error(f"[amazon] ✗ Tüm yöntemler başarısız: {url}")
    return None


def _price_only_filter(data: Optional[dict], price_only: bool) -> Optional[dict]:
    if not price_only or not data:
        return data
    return {k: data[k] for k in ("price", "stock", "cart_discount", "coupon") if k in data}


# ═══════════════════════════════════════════════════════════════
# KATMAN 1: curl_cffi (global session)
# ═══════════════════════════════════════════════════════════════

_CURL_HEADERS = {
    "Referer": "https://www.amazon.com.tr/",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


async def _via_curl(url: str) -> Optional[dict]:
    if not CURL_AVAILABLE:
        return None
    try:
        session = await _get_or_create_session()
        r = await session.get(url, headers=_CURL_HEADERS)
        html = r.text

        if r.status_code in (404, 410):
            log.info(f"[amazon/curl] HTTP {r.status_code} — ürün yok")
            return {"not_found": True}
        if r.status_code != 200:
            log.info(f"[amazon/curl] HTTP {r.status_code}")
            _reset_session()
            return None
        if not html or len(html) < 500:
            log.info(f"[amazon/curl] Kısa yanıt ({len(html or '')}B)")
            return None
        if _is_not_found(html):
            log.info("[amazon/curl] Ürün bulunamadı (404)")
            return {"not_found": True}
        if _is_blocked(html):
            log.info("[amazon/curl] CAPTCHA/block → session sıfırlanıyor")
            _reset_session()
            return None

        return await asyncio.to_thread(_parse, html)

    except Exception as e:
        log.info(f"[amazon/curl] Hata: {e}")
        _reset_session()
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 2: Playwright
# ═══════════════════════════════════════════════════════════════

async def _via_playwright(url: str, pool=None) -> Optional[dict]:
    if pool:
        page = await pool.acquire()
        try:
            from scrapers.cdp_base import setup_resource_blocking_amazon
            await setup_resource_blocking_amazon(page)
            await page.add_init_script(STEALTH_SCRIPT)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await page.mouse.wheel(0, random.randint(300, 700))
            await asyncio.sleep(random.uniform(0.3, 0.8))
            html = await page.content()
            if html and len(html) > 5000:
                if _is_not_found(html):
                    log.info("[amazon/playwright+pool] Ürün bulunamadı (404)")
                    return {"not_found": True}
                return await asyncio.to_thread(_parse, html)
            return None
        except Exception as e:
            log.error(f"[amazon/playwright+pool] Hata: {e}")
            return None
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
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ])
            try:
                _ua = random.choice(UA_POOL)
                context = await browser.new_context(
                    user_agent=_ua,
                    locale="tr-TR",
                    timezone_id="Europe/Istanbul",
                    viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
                    extra_http_headers=get_stealth_headers(_ua),
                )
                await context.add_init_script(STEALTH_SCRIPT)
                page = await context.new_page()
                if _stealth:
                    await _stealth.apply_stealth_async(page)
                await page.route("**/*.{gif,svg,ico,woff,woff2}", lambda r: r.abort())
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                try:
                    await page.wait_for_selector("#productTitle", timeout=8000)
                except Exception:
                    pass

                await page.mouse.wheel(0, random.randint(250, 600))
                await asyncio.sleep(random.uniform(0.8, 1.8))
                if random.random() > 0.5:
                    await page.mouse.wheel(0, random.randint(100, 300))
                    await asyncio.sleep(random.uniform(0.4, 0.9))

                html = await page.content()
            finally:
                await browser.close()

        if _is_not_found(html):
            log.info("[amazon/playwright] Ürün bulunamadı (404)")
            return {"not_found": True}
        if _is_blocked(html):
            log.info("[amazon/playwright] CAPTCHA/block algılandı")
            return None
        return await asyncio.to_thread(_parse, html)

    except NotImplementedError:
        log.info("[amazon/playwright] Python sürümü desteklenmiyor (3.14+)")
        return None
    except Exception as e:
        log.error(f"[amazon/playwright] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# PARSE
# ═══════════════════════════════════════════════════════════════

def _parse_price_tr(price_text: str) -> Optional[float]:
    return parse_price_tr_clean(price_text)


def _parse_coupon(soup: BeautifulSoup, html: str) -> Optional[str]:
    """Kupon rozeti metnini döner (varsa), yoksa None."""
    for sel in [
        "#couponDeals",
        "#couponBadge_feature_div",
        ".couponBadge",
        "#prime_free_tie_feature_div",
        "#socialProofingAsinFaceout_feature_div",
        "[id*='coupon']",
        "[class*='coupon-badge']",
    ]:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if t and len(t) > 2:
                return t[:200]

    # Regex fallback — "X% kupon" veya "kupon: X TL" gibi kalıplar
    for pat in [
        r'(%\s*\d+)\s*(?:ekstra\s+)?(?:indirim\s+)?kupon',
        r'kupon\s*uygula[^<"]{0,80}',
        r'\d+\s*TL\s*kupon',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(0)[:200].strip()

    return None


def _parse_variants(soup: BeautifulSoup, html: str) -> list:
    """Ürün varyantlarını çıkar. Her öğe: {"asin": str, "name": str, "price": float|None}."""
    variants: list = []
    seen: set = set()

    # Yöntem 1: #twister_feature_div içindeki li öğeleri
    for li in soup.select(
        "#twister_feature_div li[data-dp-url], "
        "#twister_feature_div li[data-defaultasin], "
        "#variation_color_name li[data-dp-url], "
        "#variation_size_name li[data-dp-url]"
    ):
        dp_url = li.get("data-dp-url", "") or ""
        m = re.search(r"/dp/([A-Z0-9]{10})", dp_url)
        asin = m.group(1) if m else None

        if not asin:
            da = (li.get("data-defaultasin") or "").strip()
            if re.match(r"^[A-Z0-9]{10}$", da):
                asin = da

        if not asin or asin in seen:
            continue
        seen.add(asin)

        name = (li.get("title") or "").strip()
        if not name:
            img = li.select_one("img")
            if img:
                name = (img.get("alt") or "").strip()
        if not name:
            span = li.select_one("span.a-size-base, span")
            if span:
                name = span.get_text(strip=True)
        if not name:
            name = asin

        variants.append({"asin": asin, "name": name[:100], "price": None})

    # Yöntem 2: Gömülü JSON'dan asinToDpUrl / dimensionToAsinMap
    if not variants:
        m = re.search(r'"asinToDpUrl"\s*:\s*(\{[^}]{10,5000}\})', html)
        if m:
            try:
                asin_map = json.loads(m.group(1))
                for asin_key in asin_map:
                    if re.match(r"^[A-Z0-9]{10}$", asin_key) and asin_key not in seen:
                        seen.add(asin_key)
                        variants.append({"asin": asin_key, "name": asin_key, "price": None})
            except Exception:
                pass

    # Yöntem 3: Gömülü JSON'dan varyant fiyatları — varsa doldur
    # Amazon bazı ürünlerde "priceMap" veya "variationDisplayLabels" ile fiyat gömer
    price_map: dict = {}
    for pat in [
        r'"priceMap"\s*:\s*(\{[^}]{10,10000}\})',
        r'"variationPrices"\s*:\s*(\{[^}]{10,10000}\})',
    ]:
        pm = re.search(pat, html)
        if pm:
            try:
                raw = json.loads(pm.group(1))
                for k, v in raw.items():
                    if re.match(r"^[A-Z0-9]{10}$", k):
                        p = parse_price_tr_clean(str(v))
                        if p:
                            price_map[k] = p
            except Exception:
                pass
        if price_map:
            break

    # "displayPrice":"1.299,00 TL" kalıplarını asin yakınında ara
    if not price_map:
        for dm in re.finditer(r'"([A-Z0-9]{10})"[^}]{0,300}"displayPrice"\s*:\s*"([^"]+)"', html):
            p = parse_price_tr_clean(dm.group(2))
            if p:
                price_map[dm.group(1)] = p

    for v in variants:
        if v["asin"] in price_map:
            v["price"] = price_map[v["asin"]]

    return variants[:50]


def _parse(html: str) -> Optional[dict]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # ── Başlık ──────────────────────────────────────────────
    title = None
    for sel in ["#productTitle", "h1#title span", "span#productTitle"]:
        el = soup.select_one(sel)
        if el:
            t = re.sub(r"\s+", " ", el.get_text()).strip()
            if t and len(t) > 3:
                title = t
                break

    if not title:
        t_el = soup.select_one("title")
        if t_el:
            raw = re.sub(r"\s+", " ", t_el.get_text()).strip()
            raw = re.sub(r"^Amazon\.com(\.tr)?[:\s]*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*[:–\-]\s*Amazon.*$", "", raw, flags=re.IGNORECASE)
            if raw and len(raw) > 5:
                title = raw

    if not title:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                if ld.get("name"):
                    title = ld["name"]
                    break
            except Exception:
                pass

    # ── Fiyat ───────────────────────────────────────────────
    price = None
    price_text = None

    for sel in [
        "span.a-price span.a-offscreen",
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#apex_offerDisplay_desktop span.a-offscreen",
        "[data-a-color='price'] span.a-offscreen",
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#corePrice_feature_div span.a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            price_text = el.get_text(strip=True)
            if price_text:
                break

    if not price_text:
        whole = soup.select_one("span.a-price-whole")
        frac = soup.select_one("span.a-price-fraction")
        if whole:
            w = whole.get_text(strip=True).replace(".", "").replace(",", "")
            f = frac.get_text(strip=True) if frac else "00"
            if w:
                price_text = f"{w},{f} TL"

    if price_text:
        price = _parse_price_tr(price_text)

    if not price:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                offers = ld.get("offers", {})
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    p = offers.get("price") or offers.get("lowPrice")
                    if p:
                        price = parse_price_tr_clean(str(p))
                        break
            except Exception:
                pass

    if not price:
        m = re.search(r'"priceAmount"[:\s]*([\d.]+)', html)
        if m:
            try:
                price = float(m.group(1))
            except Exception:
                pass

    # ── Resim ──────────────────────────────────────────────
    image_url = None

    for sel in ["#landingImage", "#imgBlkFront", "#main-image", "img.a-dynamic-image"]:
        img = soup.select_one(sel)
        if img:
            image_url = img.get("data-old-hires") or img.get("src")
            if image_url and image_url.startswith("http"):
                break
            image_url = None

    if not image_url:
        for img in soup.select("img[data-a-dynamic-image]"):
            try:
                dyn = json.loads(img.get("data-a-dynamic-image", "{}"))
                if dyn:
                    best = max(dyn.keys(), key=lambda k: sum(dyn[k]))
                    if best.startswith("http"):
                        image_url = best
                        break
            except Exception:
                pass

    if not image_url:
        og = soup.find("meta", {"property": "og:image"})
        if og and og.get("content"):
            image_url = og["content"]

    if not image_url:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                img = ld.get("image")
                if isinstance(img, list) and img:
                    image_url = img[0]
                elif isinstance(img, str) and img:
                    image_url = img
                if image_url:
                    break
            except Exception:
                pass

    if not image_url:
        m = re.search(r'"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        if m:
            image_url = m.group(1)

    # ── Rating ──────────────────────────────────────────────
    rating = None
    rating_el = soup.select_one("span.a-icon-alt")
    if rating_el:
        m = re.search(r"([\d,]+)", rating_el.text)
        if m:
            r = float(m.group(1).replace(",", "."))
            if 0 < r <= 5:
                rating = r

    # ── Yorum sayısı ────────────────────────────────────────
    reviews = None
    rev_el = soup.select_one("#acrCustomerReviewText")
    if rev_el:
        m = re.search(r"[\d.]+", rev_el.text.replace(".", "").replace(",", ""))
        if m:
            reviews = int(m.group())

    # ── Stok ────────────────────────────────────────────────
    stock = None

    for sel in [
        "#availability span.primary-availability-message",
        ".primary-availability-message",
        "#availability span",
        "#availability",
        "div#availability span",
        "#availabilityInsideBuyBox_feature_div span",
        "#availability_feature_div span",
        "#desktop_qualifiedBuyBox_feature_div span",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                tl = t.lower()

                if any(k in tl for k in (
                        "stokta",
                        "kargo",
                        "teslim",
                        "kaldı",
                        "sepete ekle",
                        "satışta",
                        "sipariş ver",
                        "in stock",
                        "available"
                )):
                    stock = "Stokta Var"

                elif any(k in tl for k in (
                        "mevcut değil",
                        "tükendi",
                        "stokta yok",
                        "out of stock",
                        "currently unavailable",
                        "temporarily out of stock",
                        "unavailable",
                        "geçici olarak stokta yok",
                        "şu an için stokta yok"
                )):
                    stock = "Stok Yok"

                elif "stok" not in tl and "yok" in tl:
                    stock = "Stok Yok"

                else:
                    stock = t
                break

    has_atc = bool(soup.select_one(
        "#add-to-cart-button, "
        "input[name='submit.add-to-cart'], "
        "#add-to-cart-announce, "
        "#attachSiNAButton, "
        "#submit.add-to-cart"
    ))

    has_buy_now = bool(soup.select_one(
        "#buy-now-button, "
        "#buy-now-button-announce"
    ))

    has_oos = bool(soup.select_one(
        "#outOfStock, "
        ".outOfStock"
    ))

    html_low = html.lower()

    if has_atc or has_buy_now:
        stock = "Stokta Var"

    elif has_oos:
        stock = "Stok Yok"

    elif "currently unavailable" in html_low:
        stock = "Stok Yok"

    elif "temporarily out of stock" in html_low:
        stock = "Stok Yok"

    elif stock == "Stokta Var":
        stock = "Stokta Var"

    else:
        stock = "Stok Yok"

    # stok yoksa fiyatı iptal et
    if stock == "Stok Yok":
        price = None

    # ── Barkod (EAN/GTIN) ───────────────────────────────────
    barcode = None
    for row in soup.select("#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr, .a-normal tr"):
        label = row.select_one("th, td:first-child")
        value = row.select_one("td:last-child")
        if label and value:
            lbl = label.get_text(strip=True).lower()
            if any(k in lbl for k in ["ean", "gtin", "barkod", "barcode", "upc"]):
                bc = re.sub(r"[^\d]", "", value.get_text(strip=True))
                if bc and 8 <= len(bc) <= 14:
                    barcode = bc
                    break

    if not barcode:
        for pat in [r'"ean"\s*:\s*"(\d{8,14})"', r'"gtin"\s*:\s*"(\d{8,14})"', r'EAN[:\s]+(\d{8,14})']:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                barcode = m.group(1)
                break

    # ── Sepette indirim ──────────────────────────────────────
    cart_discount = detect_cart_discount(html)
    if not cart_discount:
        for sel in ["#priceBadging_feature_div", "#promoPriceBlockMessage_feature_div"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                cart_discount = True
                break

    # ── Kupon ─────────────────────────────────────────────────
    coupon = _parse_coupon(soup, html)

    # ── Varyantlar ────────────────────────────────────────────
    variants = _parse_variants(soup, html)

    log.info(
        f"[amazon] parse: title={'✔' if title else '✘'} price={price} barcode={barcode} "
        f"image={'✔' if image_url else '✘'} stock={stock} cart_discount={cart_discount} "
        f"coupon={'✔' if coupon else '✘'} variants={len(variants)}"
    )
    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "rating": rating,
        "review_count": reviews,
        "stock": stock,
        "barcode": barcode,
        "cart_discount": cart_discount,
        "coupon": coupon,
        "variants": variants if variants else None,
    }
