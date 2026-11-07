#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N11 scraper

Katman mimarisi:
  0) curl_cffi iOS Safari HTML (window.model JSON parse)
  1) crawlee PlaywrightCrawler (GQL intercept + HTML parse fallback)

curl katmanı sonradan eklendi: n11.com bu sunucunun IP'sini hiç
bloklamıyor (düz curl isteği anında 200 + window.model verisiyle
dönüyor) — asıl sorun n11'in sadece Playwright'a bağımlı olup en
yavaş/en kırılgan yönteme mahkum kalmasıydı.
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
    crawlee_pw_scrape, _CURL_OK, redirected_away,
)
from scrapers.utils import parse_price_tr_clean, normalize_image_url

log = logging.getLogger("scraper.n11")

_N11_BLOCK_MARKERS = [
    "captcha", "access denied", "just a moment", "attention required",
    "checking your browser", "unusual traffic", "güvenlik doğrulaması",
    "robot değilsiniz", "erişiminiz kısıtlandı", "cf-challenge",
]


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


_N11_GQL_ID_KEYS = ("contentId", "groupId", "id", "productId")


def _n11_parse_gql(data: dict, target_cid: Optional[int] = None) -> Optional[dict]:
    try:
        detail = data["data"]["productDetail"]
    except (KeyError, TypeError):
        return None
    if not detail:
        return None

    # Kimlik doğrulama: n11 sayfasında "benzer ürünler" gibi widget'lar da
    # aynı productDetail şemasını kullanan GraphQL cevapları döndürebilir —
    # bunlar istenen üründen farklı bir ürünün fiyatını taşıyabilir. Yanıtta
    # bilinen bir kimlik alanı varsa ve hedef contentId ile uyuşmuyorsa
    # reddet. Alan hiç yoksa (şema bilinmiyor) mevcut davranışı bozmamak
    # için kabul et — sadece bilinen bir uyuşmazlıkta reddet.
    if target_cid is not None:
        for key in _N11_GQL_ID_KEYS:
            if key in detail:
                try:
                    if int(detail[key]) != int(target_cid):
                        log.warning(f"[n11/gql] kimlik uyuşmazlığı ({key}={detail[key]!r} != {target_cid}) — reddedildi")
                        return None
                except (TypeError, ValueError):
                    pass
                break

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
        "stock_qty":     int(qty) if isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty >= 0 else None,
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
            # candidates[0]'a düşmek yanlış ürünün (öneri/benzer ürün widget'ı)
            # verisini asıl ürün sanıp kaydetme riski taşıyordu — gerçek
            # verilerde groupId==target_cid eşleşmesi güvenilir şekilde
            # bulunuyor, bulunamıyorsa veri döndürmemek daha güvenli.
            log.debug(f"[n11/model] groupId={target_cid} bulunamadı, {len(candidates)} aday var — reddedildi")
            return None
    else:
        product = candidates[0]

    price_raw = product.get("displayPriceFloat") or product.get("priceFloat")
    if not price_raw or float(price_raw) <= 0:
        return None
    price = float(price_raw)

    was_price = None
    for wp_key in ("firstListPrice", "oldPrice"):
        # Bazı sayfalarda bu alan ham sayı değil, "18.000 TL" gibi formatlı
        # bir string olarak geliyor — düz float() burada ValueError fırlatıp
        # (yakalanmadan) Crawlee'ye kadar çıkıyor, Crawlee de retry fırtınasına
        # girip dış timeout bütçesini tüketiyordu.
        wp_val = parse_price_tr_clean(str(product.get(wp_key))) if product.get(wp_key) else None
        if wp_val and wp_val > price * 1.01:
            was_price = wp_val
            break

    # "Sepette" fiyat: N11 normal fiyatı priceFloat'ta, sepette düşen fiyatı
    # displayPriceFloat'ta veriyor (sayfada üstü çizili değil; büyük yazı sepet fiyatı,
    # küçük yazı normal fiyat). Kaydedilen fiyat zaten sepet fiyatı — bayrak yalnızca
    # bildirim/rozet ve fırsat kapanma toleransı için (2026-11-02).
    list_raw = product.get("priceFloat")
    cart_discount = bool(isinstance(list_raw, (int, float)) and float(list_raw) > price * 1.01)
    if cart_discount and not was_price:
        was_price = float(list_raw)

    stock_val = product.get("stock")
    stock = "Stokta Var" if (stock_val and int(float(stock_val)) > 0) else "Stok Yok"
    # Stok adedi: ürünün kendi "stock" alanı (2026-11-07: 2/51/78/98/618 doğrulandı;
    # "Son 2 ürün" rozetiyle aynı sayı)
    try:
        stock_qty = max(0, int(float(stock_val))) if stock_val is not None else None
    except (TypeError, ValueError):
        stock_qty = None

    images = product.get("images") or []
    image_url = None
    if images:
        raw_path = images[0].get("path", "")
        image_url = raw_path.replace("{0}", "600_800") if raw_path else None

    # Barkod: N11 seçili/satıştaki varyantın GTIN'ini defaultGtin'de veriyor
    # (skus[] tüm renk/boyut varyantlarının barkodlarını içerir — onlar değil).
    # Eskiden hiç okunmuyordu; N11 ürünleri barkodsuz kalıp çapraz arama atlanıyordu.
    gtin = str(product.get("defaultGtin") or "").strip()
    barcode = gtin if re.fullmatch(r"\d{8,14}", gtin) else None
    sel = product.get("seller") if isinstance(product.get("seller"), dict) else {}
    seller = (str(sel.get("nickName") or sel.get("name") or "").strip()) or None

    log.info(f"[n11/model] ✔ title={product.get('title','?')[:40]} price={price}")
    return {
        "title":        product.get("title"),
        "price":        price,
        "was_price":    was_price,
        "image_url":    image_url,
        "stock":        stock,
        "stock_qty":    stock_qty,
        "cart_discount": cart_discount,
        "barcode":      barcode,
        "seller":       seller,
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
                # Eski ve indirimli fiyat aynı elementte olabilir → en düşüğü güncel fiyat
                v = parse_price_tr_clean(el.get_text(" ", strip=True), pick="min")
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
    target_cid = _n11_extract_cid(url)

    async def _on_response(resp):
        if resp.status != 200:
            return
        if "graphql" not in resp.url and "nss/api" not in resp.url:
            return
        try:
            j = await resp.json()
            parsed = _n11_parse_gql(j, target_cid=target_cid)
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

    for _ in range(20):
        if gql_result.get("price"):
            return gql_result
        await asyncio.sleep(0.2)

    html = await page.content()
    cid = _n11_extract_cid(url)
    return _n11_parse_html(html, cid=cid)


async def _n11_via_curl(url: str) -> Optional[dict]:
    if not _CURL_OK or not cb_ok("n11_curl"):
        return None
    try:
        session = await get_session("n11", "safari18_0")
        ua = random.choice(IOS_UAS)
        r = await session.get(url, headers={
            "User-Agent":      ua,
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            "Cache-Control":   "no-cache",
            "Pragma":          "no-cache",
            "Referer":         "https://www.n11.com/",
        }, allow_redirects=True, timeout=20)

        if r.status_code in (404, 410):
            return {"dead_url": True}
        if r.status_code in (403, 429, 503):
            cb_fail("n11_curl")
            RL["n11"].record_failure()
            drop_session("n11")
            return None
        if r.status_code != 200:
            return None

        if redirected_away("n11", url, str(r.url)):
            log.info(f"[n11/curl] kaldırılmış ürün → {str(r.url)[-60:]}")
            return {"removed": True, "final_url": str(r.url)}

        html = r.text
        if not html or len(html) < 3000:
            drop_session("n11")
            return None

        low = html[:5000].lower()
        if any(k in low for k in _N11_BLOCK_MARKERS):
            log.warning("[n11/curl] engel/captcha sayfası tespit edildi — oturum atılıyor")
            cb_fail("n11_curl")
            RL["n11"].record_failure()
            drop_session("n11")
            return None

        cid = _n11_extract_cid(url)
        result = _n11_parse_html(html, cid=cid)
        if result and result.get("price"):
            cb_reset("n11_curl")
            RL["n11"].record_success()
            log.info(f"[n11/curl] ✔ price={result.get('price')}")
        return result
    except Exception as e:
        drop_session("n11")
        log.debug(f"[n11/curl] hata: {e}")
        return None


async def scrape_n11(url: str, price_only: bool = False,
                      cached_image: Optional[str] = None) -> Optional[dict]:
    # RL.wait() artık çağıran (local_scraper.process_job) tarafında, dış
    # timeout'un DIŞINDA yapılıyor — bkz. crawlee_base.RL üzerindeki not.
    data = await _n11_via_curl(url)
    if data and (data.get("dead_url") or data.get("removed") or data.get("price")):
        return price_filter(data, price_only, cached_image)

    log.info(f"[n11] curl başarısız → Playwright: {url[:60]}")
    data = await crawlee_pw_scrape(url, "n11", _n11_pw_handler, timeout=55)
    return price_filter(data, price_only, cached_image) if data else None
