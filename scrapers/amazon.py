#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon.com.tr scraper — çok katmanlı (v3).
amazon_gui.py'deki savaş-test edilmiş parse mantığı entegre edildi.

Katmanlar:
1) httpx (hızlı)
2) curl_cffi (Chrome TLS fingerprint — anti-bot bypass)
3) Playwright (son çare — Python 3.14'te çalışmayabilir)
"""

import re
import json
import random
import asyncio
from typing import Optional, Tuple
from bs4 import BeautifulSoup
from scrapers.utils import CLOUDFLARE_TITLES

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

import httpx

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# amazon_gui.py'den alınan blok/captcha tespiti
CAPTCHA_HINTS = [
    "robot check", "validatecaptcha", "type the characters",
    "enter the characters", "not a robot", "güvenlik kontrolü",
    "lütfen aşağıdaki karakterleri girin", "captcha",
]
BLOCK_HINTS = [
    "üzgünüz", "isteğinizi işlemeye", "sorun üzerinde çalışıyoruz",
    "something went wrong", "we're sorry", "api-services-support@amazon",
]


async def scrape_amazon(url: str) -> Optional[dict]:
    """Çok katmanlı Amazon.com.tr scraper."""

    # Katman 1: httpx
    print("[amazon] httpx deneniyor...")
    data = await _via_httpx(url)
    if data and data.get("title") and data.get("price"):
        print(f"[amazon/httpx] ✔ title={data['title'][:50]!r} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
        return data

    # Katman 2: curl_cffi
    if CURL_AVAILABLE:
        print("[amazon] httpx başarısız → curl_cffi deneniyor...")
        data = await _via_curl_cffi(url)
        if data and data.get("title") and data.get("price"):
            print(f"[amazon/curl_cffi] ✔ title={data['title'][:50]!r} price={data['price']} image={'✔' if data.get('image_url') else '✘'}")
            return data
    else:
        print("[amazon] curl_cffi yüklü değil")

    # Katman 3: Playwright
    print("[amazon] → Playwright deneniyor...")
    data = await _via_playwright(url)
    if data and data.get("title"):
        print(f"[amazon/playwright] ✔ title={data['title'][:50]!r} price={data.get('price')} image={'✔' if data.get('image_url') else '✘'}")
        return data

    print(f"[amazon] ✗ Tüm yöntemler başarısız: {url}")
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
        "Referer": "https://www.amazon.com.tr/",
        "sec-ch-ua": '"Chromium";v="125", "Google Chrome";v="125"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
        if r.status_code != 200:
            print(f"[amazon/httpx] status={r.status_code}")
            return None
        if _is_blocked(r.text):
            print("[amazon/httpx] Bot koruması/CAPTCHA algılandı")
            return None
        return await asyncio.to_thread(_parse, r.text)
    except Exception as e:
        print(f"[amazon/httpx] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 2: curl_cffi
# ═══════════════════════════════════════════════════════════════

async def _via_curl_cffi(url: str) -> Optional[dict]:
    try:
        impersonate = random.choice(["chrome124", "chrome123", "chrome120"])
        async with CurlSession(impersonate=impersonate) as session:
            # Warmup — amazon_gui.py'deki gibi önce anasayfaya git
            try:
                await session.get("https://www.amazon.com.tr/", timeout=15)
                await asyncio.sleep(random.uniform(0.8, 2.2))
            except:
                pass

            r = await session.get(url, headers={
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Referer": "https://www.amazon.com.tr/",
                "Upgrade-Insecure-Requests": "1",
            }, timeout=30)

        if r.status_code != 200:
            print(f"[amazon/curl_cffi] status={r.status_code}")
            return None
        if _is_blocked(r.text):
            print("[amazon/curl_cffi] Bot koruması/CAPTCHA algılandı")
            return None
        return await asyncio.to_thread(_parse, r.text)
    except Exception as e:
        print(f"[amazon/curl_cffi] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# KATMAN 3: Playwright
# ═══════════════════════════════════════════════════════════════

async def _via_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ])
            context = await browser.new_context(
                user_agent=random.choice(UA_LIST),
                locale="tr-TR",
                viewport={"width": 1366, "height": 768},
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR', 'tr', 'en-US'] });
                window.chrome = { runtime: {} };
            """)

            page = await context.new_page()
            # Font ve medya engelle ama görselleri koru (image_url için)
            await page.route("**/*.{gif,svg,ico,woff,woff2}", lambda r: r.abort())
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            try:
                await page.wait_for_selector("#productTitle", timeout=8000)
            except:
                pass
            await page.wait_for_timeout(1000)

            html = await page.content()
            await browser.close()

        if _is_blocked(html):
            print("[amazon/playwright] CAPTCHA/block algılandı")
            return None
        return await asyncio.to_thread(_parse, html)

    except NotImplementedError:
        print("[amazon/playwright] Python sürümü desteklenmiyor (3.14+)")
        return None
    except Exception as e:
        print(f"[amazon/playwright] Hata: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# BLOCK / CAPTCHA TESPİT (amazon_gui.py'den)
# ═══════════════════════════════════════════════════════════════

def _is_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    t = html[:10000].lower()
    for h in CAPTCHA_HINTS + BLOCK_HINTS:
        if h in t:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# PARSE (amazon_gui.py parse mantığı entegre)
# ═══════════════════════════════════════════════════════════════

def _parse_price_tr(price_text: str) -> Optional[float]:
    """amazon_gui.py'deki parse_price fonksiyonu."""
    if not price_text:
        return None
    txt = price_text.strip()
    m = re.search(r"([\d\.\s]+,\d{1,2}|[\d\.\s]+)", txt)
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _parse(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # ── Başlık ──────────────────────────────────────────────
    title = None
    # 1) #productTitle (en güvenilir)
    for sel in ["#productTitle", "h1#title span", "span#productTitle"]:
        el = soup.select_one(sel)
        if el:
            t = re.sub(r"\s+", " ", el.get_text()).strip()
            if t and len(t) > 3:
                title = t
                break

    # 2) <title> tag fallback (amazon_gui.py'deki gibi temizle)
    if not title:
        t_el = soup.select_one("title")
        if t_el:
            raw = re.sub(r"\s+", " ", t_el.get_text()).strip()
            raw = re.sub(r"^Amazon\.com(\.tr)?[:\s]*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*[:–\-]\s*Amazon.*$", "", raw, flags=re.IGNORECASE)
            if raw and len(raw) > 5:
                title = raw

    # 3) JSON-LD
    if not title:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list): ld = ld[0]
                if ld.get("name"):
                    title = ld["name"]
                    break
            except:
                pass

    # ── Fiyat ───────────────────────────────────────────────
    price = None
    price_text = None

    # 1) offscreen fiyat (en yaygın — amazon_gui.py'deki sıralama)
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

    # 2) whole + fraction fallback (amazon_gui.py'deki gibi)
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

    # 3) JSON-LD fiyat fallback
    if not price:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list): ld = ld[0]
                offers = ld.get("offers", {})
                if isinstance(offers, list) and offers: offers = offers[0]
                if isinstance(offers, dict):
                    p = offers.get("price") or offers.get("lowPrice")
                    if p:
                        price = float(str(p).replace(",", "."))
                        break
            except:
                pass

    # 4) Regex fallback — HTML'de gizli fiyat
    if not price:
        m = re.search(r'"priceAmount"[:\s]*([\d.]+)', str(soup))
        if m:
            try:
                price = float(m.group(1))
            except:
                pass

    # ── Resim (çoklu fallback) ──────────────────────────────
    image_url = None

    # 1) Klasik selektörler
    for sel in ["#landingImage", "#imgBlkFront", "#main-image", "img.a-dynamic-image"]:
        img = soup.select_one(sel)
        if img:
            image_url = img.get("data-old-hires") or img.get("src")
            if image_url and image_url.startswith("http"):
                break
            image_url = None

    # 2) data-a-dynamic-image JSON — en büyük görseli seç
    if not image_url:
        for img in soup.select("img[data-a-dynamic-image]"):
            try:
                dyn = json.loads(img.get("data-a-dynamic-image", "{}"))
                if dyn:
                    best = max(dyn.keys(), key=lambda k: sum(dyn[k]))
                    if best.startswith("http"):
                        image_url = best
                        break
            except:
                pass

    # 3) og:image
    if not image_url:
        og = soup.find("meta", {"property": "og:image"})
        if og and og.get("content"):
            image_url = og["content"]

    # 4) JSON-LD image
    if not image_url:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list): ld = ld[0]
                img = ld.get("image")
                if isinstance(img, list) and img: image_url = img[0]
                elif isinstance(img, str) and img: image_url = img
                if image_url: break
            except:
                pass

    # 5) Regex: media-amazon.com görseli
    if not image_url:
        m = re.search(r'"(https://m\.media-amazon\.com/images/I/[^"]+)"', str(soup))
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
    for sel in ["#availability span", "#availability", "div#availability span"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t:
                tl = t.lower()
                if "stokta" in tl or "kargo" in tl or "teslim" in tl:
                    stock = "Stokta Var"
                elif "stok" not in tl and ("yok" in tl or "tükendi" in tl or "mevcut değil" in tl):
                    stock = "Stok Yok"
                else:
                    stock = t
                break

    print(f"[amazon] parse: title={'✔' if title else '✘'} price={price} image={'✔' if image_url else '✘'} stock={stock}")
    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "rating": rating,
        "review_count": reviews,
        "stock": stock,
    }
