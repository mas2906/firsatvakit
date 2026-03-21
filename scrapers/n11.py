#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N11.com scraper — Cloudflare için Playwright, httpx fallback.
Katmanlar:
  1) httpx + güncel seçiciler
  2) Playwright (Cloudflare varsa)
"""

import re
import random
import asyncio
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from scrapers.utils import UA_POOL, parse_price_tr_clean, normalize_image_url

# İstekler arası min/max bekleme (saniye)
N11_MIN_DELAY = 2.0
N11_MAX_DELAY = 5.0
_last_ts = 0.0
_sem = asyncio.Semaphore(1)


async def _wait():
    global _last_ts
    async with _sem:
        import time
        elapsed = time.monotonic() - _last_ts
        wait = random.uniform(N11_MIN_DELAY, N11_MAX_DELAY) - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        _last_ts = time.monotonic()


def _parse_soup(soup: BeautifulSoup) -> dict:
    """HTML'den ürün bilgilerini çıkar — güncel N11 seçicileri."""
    # ── Başlık ──────────────────────────────────────────────────
    title = None
    for sel in [
        "h1.title.max-three-lines",
        "h1.title",
        "h1[class*='title']",
        "h1[class*='name']",
        "h1[class*='proName']",
        "h1.proName",
        "h1",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 5 and not t.startswith("Ürün Bil"):
                title = t
                break

    # ── Fiyat ────────────────────────────────────────────────────
    price = None
    for sel in [
        ".newPrice",
        "span.newPrice",
        "ins.newPrice",
        "[class*='newPrice']",
        "[class*='bestPrice']",
        "[itemprop='price']",
        "[class*='price'] ins",
        "[class*='price']",
    ]:
        el = soup.select_one(sel)
        if el:
            val = el.get("content") or el.get_text(strip=True)
            p = parse_price_tr_clean(val)
            if p and p > 0:
                price = p
                break

    # Fiyat script'ten (JSON-LD veya inline)
    if not price:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                d = json.loads(script.string or "")
                offers = d.get("offers", {})
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    raw = offers.get("price") or offers.get("lowPrice")
                    if raw:
                        p = parse_price_tr_clean(str(raw))
                        if p and p > 0:
                            price = p
                            break
            except Exception:
                pass

    # Regex fallback
    if not price:
        m = re.search(r"[\"']price[\"']\s*:\s*[\"']?([\d.,]+)", str(soup))
        if m:
            price = parse_price_tr_clean(m.group(1))

    # ── Resim ─────────────────────────────────────────────────────
    image_url = None

    # 1) swiper-image-0 — Playwright'ta render edilen gerçek ürün görseli (en öncelikli)
    for sel in [
        "img.swiper-image-0",
        "img[class*='swiper-image']",
        "img#selectedProductImg",
        "img[class*='productImage']",
        "img[class*='product-image']",
        "img[itemprop='image']",
        ".productImage img",
        "meta[property='og:image']",
    ]:
        el = soup.select_one(sel)
        if el:
            raw = el.get("src") or el.get("content") or el.get("data-src") or el.get("data-lazy-src")
            url_candidate = normalize_image_url(raw)
            # Sadece n11 ürün CDN'inden gelen URL'leri kabul et
            if url_candidate and "akamaized.net" in url_candidate:
                image_url = url_candidate
                break

    # 2) n11scdn2-im regex — yüksek çözünürlük (CSS bulamazsa)
    if not image_url:
        html_str = str(soup)
        m = re.search(r'(https://n11scdn2-im\.akamaized\.net/a1/\d+/[^\s"\'<>]+\.(?:jpg|png|webp))', html_str)
        if m:
            image_url = m.group(1)

    # 3) n11scdn regex — 375_535 veya benzeri boyutlu görseller
    if not image_url:
        html_str = str(soup)
        m = re.search(
            r'(https://n11scdn\.akamaized\.net/a1/(?:\d+_\d+|\d{3,})/[^\s"\'<>]+\.(?:jpg|png|webp))',
            html_str
        )
        if m:
            image_url = m.group(1)

    # 4) JSON-LD image — sadece n11 CDN'inden geliyorsa kullan (site logosu değil)
    if not image_url:
        import json as _json
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                d = _json.loads(script.string or "")
                if isinstance(d, list): d = d[0]
                img_val = d.get("image")
                if img_val:
                    if isinstance(img_val, list): img_val = img_val[0]
                    if isinstance(img_val, str) and "akamaized.net" in img_val:
                        image_url = img_val
                        break
            except Exception:
                pass

    # ── Rating ────────────────────────────────────────────────────
    rating = None
    for sel in [
        ".ratingText",
        "[class*='ratingScore']",
        "span[class*='rating']",
        "h1[class*='score']",
    ]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"([\d]+[,.][\d]+|[1-5])", el.get_text())
            if m:
                try:
                    rating = float(m.group(1).replace(",", "."))
                    if 0 < rating <= 5:
                        break
                except Exception:
                    pass

    # ── Yorum sayısı ─────────────────────────────────────────────
    review_count = None
    for sel in [".ratingCount", "[class*='reviewCount']", "[class*='commentCount']"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"(\d+)", el.get_text())
            if m:
                review_count = int(m.group(1))
                break

    print(f"[n11] title={title!r:.50} price={price}")
    return {
        "title": title,
        "price": price,
        "image_url": image_url,
        "rating": rating,
        "review_count": review_count,
        "stock": "Bilinmiyor",
    }


async def _via_httpx(url: str) -> Optional[dict]:
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.n11.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=25, follow_redirects=True
        ) as client:
            # Ana sayfaya önce git (cookie/session)
            try:
                await client.get("https://www.n11.com/", timeout=10)
                await asyncio.sleep(random.uniform(0.7, 1.8))
            except Exception:
                pass
            r = await client.get(url)

        if r.status_code == 403 or "cloudflare" in r.text.lower() or "captcha" in r.text.lower():
            print("[n11/httpx] Cloudflare engeli algılandı → Playwright'e geçiyor")
            return None

        if r.status_code != 200:
            print(f"[n11/httpx] status={r.status_code}")
            return None

        html_text = r.text
        data = await asyncio.to_thread(lambda: _parse_soup(BeautifulSoup(html_text, "html.parser")))
        if data.get("title") and data.get("price"):
            return data
        print("[n11/httpx] Parse başarısız → Playwright deneniyor")
        return None
    except Exception as e:
        print(f"[n11/httpx] Hata: {e}")
        return None


async def _via_playwright(url: str) -> Optional[dict]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = await browser.new_context(
                user_agent=random.choice(UA_POOL),
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": 1366, "height": 768},
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "Object.defineProperty(navigator,'languages',{get:()=>['tr-TR','tr']});"
                "window.chrome={runtime:{}};"
            )
            page = await ctx.new_page()
            # Gereksiz kaynakları engelle (hız için)
            await page.route("**/*.{gif,svg,ico,woff,woff2}", lambda r: r.abort())

            # Önce ana sayfa
            try:
                await page.goto("https://www.n11.com/", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(random.randint(600, 1500))
            except Exception:
                pass

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(1000, 2500))

            content = await page.content()
            await browser.close()

            if "cloudflare" in content.lower() and "captcha" in content.lower():
                print("[n11/playwright] Cloudflare aşılamadı")
                return None

            data = await asyncio.to_thread(lambda: _parse_soup(BeautifulSoup(content, "html.parser")))
            if data.get("title"):
                print(f"[n11/playwright] ✔ title={data['title']!r:.50} price={data['price']}")
                return data

    except NotImplementedError:
        print("[n11/playwright] Python 3.14+ desteklenmiyor")
    except Exception as e:
        print(f"[n11/playwright] Hata: {e}")
    return None


async def scrape_n11(url: str) -> Optional[dict]:
    await _wait()
    data = await _via_httpx(url)
    if data:
        return data
    data = await _via_playwright(url)
    return data
