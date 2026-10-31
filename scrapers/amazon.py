#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon.com.tr scraper

Katman mimarisi:
  0) crawlee PlaywrightCrawler (browserforge stealth)

Selektörler amazon_tr_scraper.py referansından alınmıştır.

curl_cffi ön-denemesi kaldırıldı (trendyol ile aynı gerekçe): iki katmanı
sıralı denemek gereksiz gecikme + fazladan istek (ban yüzeyi) ekliyordu.
"""

import asyncio
import json
import logging
import random
import re
from typing import Optional

from bs4 import BeautifulSoup

from scrapers.crawlee_base import price_filter, crawlee_pw_scrape, already_on
from scrapers.utils import parse_price_tr_clean, normalize_image_url

log = logging.getLogger("scraper.amazon")

# ── Algılama kelimeleri ────────────────────────────────────────────────────────
_AZ_CAPTCHA  = ["robot check", "validatecaptcha", "type the characters", "not a robot",
                "güvenlik kontrolü", "captcha", "px-captcha", "api-services-support"]
_AZ_BLOCK    = ["üzgünüz", "isteğinizi işlemeye", "sorun üzerinde çalışıyoruz",
                "something went wrong", "we're sorry", "to discuss automated"]
_AZ_NOTFOUND = ["sitemizde işlev gösteren bir sayfaya karşılık gelmiyor",
                "aradığınız bir şey mi var", "sayfa bulunamadı", "dogs of amazon"]

_AZ_NOISE_SELECTORS = [
    "#similarities_feature_div", "[id*='sims-']", "[id*='p13n']", "[id*='sp_detail']",
    ".a-carousel-viewport", "#HLCXComparisonWidget_feature_div", "[id*='comparison']",
    "[id*='viewed-together']", "#discovery-and-inspiration", "#rhf", "[id*='rhf_']",
    "[class*='_vc-pfo']", "[data-component-type='s-search-results']",
]

_OOS_KW = [
    "şu an için stokta yok", "geçici olarak stokta yok", "currently unavailable",
    "tükendi", "stokta yok", "bu ürün şu anda mevcut değil", "satışa sunulmamaktadır",
    "satışta değil", "temin edilemiyor", "yakında stokta", "ön sipariş", "ürün mevcut değil",
]


def _az_is_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    t = html[:10000].lower()
    return any(h in t for h in _AZ_CAPTCHA + _AZ_BLOCK)


def _az_is_notfound(html: str) -> bool:
    if not html:
        return False
    t = html[:5000].lower()
    # Türkçe ı/i farkını aşmak için ASCII normalize edilmiş forma da bak
    t_ascii = t.replace("ı", "i").replace("ğ", "g").replace("ş", "s").replace("ç", "c").replace("ö", "o").replace("ü", "u")
    return any(h in t or h.replace("ı", "i").replace("ğ", "g") in t_ascii for h in _AZ_NOTFOUND)


# ── Fiyat / stok yardımcıları (amazon_tr_scraper.py referansı) ───────────────

def _parse_price_soup(soup: BeautifulSoup) -> Optional[float]:
    """
    Öncelik sırası (amazon_tr_scraper.py referansı):
    1) apexPriceToPay / priceToPay → Amazon'un kesin "ödenecek fiyat" elementi
    2) :not(.a-text-price)         → üzeri çizili eski fiyatı atla
    3) whole+fraction parçaları    → bazı sayfalar .a-offscreen kullanmaz
    4) JSON-LD schema.org          → son API fallback
    """
    for sel in [
        "#corePriceDisplay_desktop_feature_div .apexPriceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePrice_feature_div .apexPriceToPay .a-offscreen",
        "#corePrice_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .apexPriceToPay .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .priceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .apexPriceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .priceToPay .a-offscreen",
        "#mobile-buybox .apexPriceToPay .a-offscreen",
        "#mobile-buybox .priceToPay .a-offscreen",
        # .a-text-price = üzeri çizili eski fiyat → :not ile hariç tut
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#apex_offerDisplay_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#mobile-buybox .a-price:not(.a-text-price) .a-offscreen",
        # Son fallback (taksit/yanlış fiyat riski var)
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        # Varyantı (beden/ölçü) seçilmemiş sayfa: satın alma kutusu "Satın almak için
        # Ölçü seçin" diyor, fiyat sadece #apex_desktop'ta (ve a-text-price sınıflı).
        # Fiyat ARALIĞI ise (1.699 TL - 2.399 TL) alınmaz — hangi varyant belli değil.
        "#apex_desktop .apexPriceToPay .a-offscreen",
        "#apex_desktop .priceToPay .a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            if sel.startswith("#apex_desktop") and _az_is_range(soup.select_one("#apex_desktop")):
                continue
            v = parse_price_tr_clean(el.get_text(strip=True))
            if v:
                return v

    # Whole + fraction: Amazon'un yeni ("reinvent") fiyat düzeninde .a-offscreen boş
    # geliyor, fiyat sadece a-price-whole + a-price-fraction'da. Kutular öncelik
    # sırasıyla TEK TEK denenir — eskiden virgüllü tek seçici sayfada ilk çıkan
    # #buybox'ı alıyor, asıl fiyat kutusuna hiç bakmıyordu (2026-10-31, #48570).
    for cid in ("#corePriceDisplay_desktop_feature_div", "#corePriceDisplay_mobile_feature_div",
                "#corePrice_feature_div", "#apex_offerDisplay_desktop", "#apex_desktop",
                "#mobile-buybox", "#buybox"):
        container = soup.select_one(cid)
        if not container or _az_is_range(container):
            continue
        price_el = (container.select_one(".apexPriceToPay, .priceToPay")
                    or container.select_one(".a-price:not(.a-text-price)")
                    or container)
        whole = price_el.select_one("span.a-price-whole")
        if not whole:
            continue
        frac = price_el.select_one("span.a-price-fraction")
        w = whole.get_text(strip=True).replace(".", "").replace(",", "")
        f = (frac.get_text(strip=True) if frac else "00").strip()
        if w.isdigit():
            v = parse_price_tr_clean(f"{w},{f} TL")
            if v:
                return v

    return None


def _az_is_range(el) -> bool:
    """Varyant seçilmemiş sayfalardaki '1.699,00 TL - 2.399,00 TL' fiyat aralığı."""
    if el is None:
        return False
    if el.select_one(".a-price-range"):
        return True
    return bool(re.search(r"\d\s*TL\s*[-–]\s*\d", el.get_text(" ", strip=True)))


def _parse_was_price_soup(soup: BeautifulSoup) -> Optional[float]:
    """Üzeri çizili referans fiyatı (amazon_tr_scraper.py get_was_price_soup)."""
    for sel in [
        "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen",
        "#corePrice_feature_div .a-text-price .a-offscreen",
        "#corePriceDisplay_mobile_feature_div .a-text-price .a-offscreen",
        "#apex_offerDisplay_desktop .a-text-price .a-offscreen",
        "#mobile-buybox .a-text-price .a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            v = parse_price_tr_clean(el.get_text(strip=True))
            if v:
                return v
    return None


def _parse_coupon_soup(soup: BeautifulSoup) -> tuple[Optional[str], Optional[float]]:
    """
    Kupon bilgisi (amazon_tr_scraper.py parse_coupon).
    Döndürür: ('pct', 10.0) | ('abs', 50.0) | (None, None)
    """
    for sel in [
        "#couponBadgeRegularVpc", "#couponBadge", ".couponBadge",
        "#promoPriceBlockMessage_feature_div", "#cpsPlaceHolder_feature_div",
        "#vpcButton", "#couponDeals", "#couponBadge_feature_div", "[id*='coupon']",
    ]:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if not el:
            continue
        txt = el.get_text(" ", strip=True).lower()
        m = re.search(r'%\s*(\d+(?:[.,]\d+)?)', txt) or re.search(r'(\d+(?:[.,]\d+)?)\s*%', txt)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if 0 < val < 80:
                    return "pct", val
            except Exception:
                pass
        m2 = re.search(r'[₺]\s*([\d.]+(?:,\d+)?)', txt) or re.search(r'([\d.]+(?:,\d+)?)\s*tl', txt)
        if m2:
            val = parse_price_tr_clean(m2.group(1).replace(".", "").replace(",", "."))
            if val and val > 0:
                return "abs", val
    return None, None


def _get_seller_info(soup: BeautifulSoup) -> tuple[Optional[str], bool]:
    """
    (satıcı_adı, is_amazon) döndürür (amazon_tr_scraper.py get_seller_info).
    Amazon.com.tr doğrudan satıyorsa is_amazon=True.
    """
    for sel in [
        "#merchantInfoFeature_feature_div",
        "#merchant-info",
        "#tabular-buybox-container",
        "#sellerProfileTriggerId",
    ]:
        el = soup.select_one(sel)
        if not el:
            continue
        txt   = el.get_text(" ", strip=True)
        lower = txt.lower()
        if "amazon.com.tr" in lower:
            return "Amazon.com.tr", True
        trigger = el.select_one("#sellerProfileTriggerId")
        if trigger:
            return trigger.get_text(strip=True), False
        m = re.search(r"satıcı[:\s]+([^\n,]+)", lower)
        if m:
            return m.group(1).strip(), False
        if "sold by" in lower or "satıcı" in lower:
            return txt[:60].strip(), False
    return None, True  # bilgi yoksa Amazon say (optimistic)


def _get_other_sellers_price(soup: BeautifulSoup) -> Optional[float]:
    """Diğer satıcılar bölümünden en düşük fiyat (amazon_tr_scraper.py)."""
    for sel in [
        "#moreBuyingChoices_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#moreBuyingChoices_feature_div .a-price .a-offscreen",
        "#olpLinkWidget_feature_div .a-price .a-offscreen",
        "#usedBuySection .a-price .a-offscreen",
        "#buybox-see-all-buying-choices .a-price .a-offscreen",
    ]:
        els = soup.select(sel)
        prices = [p for p in (parse_price_tr_clean(e.get_text()) for e in els) if p]
        if prices:
            return min(prices)
    for sel in ["#moreBuyingChoices_feature_div", "#olpLinkWidget_feature_div"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r'([\d.]+,\d{2})\s*TL', el.get_text())
            if m:
                return parse_price_tr_clean(m.group(1) + " TL")
    return None


def _normalize_gtin(digits: str) -> Optional[str]:
    """GTIN-14'ü EAN-13'e çevirir (başındaki 0'ı atar), uzunluk aralığını doğrular."""
    digits = re.sub(r"[^\d]", "", digits)
    if len(digits) == 14 and digits.startswith("0"):
        digits = digits[1:]
    return digits if 8 <= len(digits) <= 14 else None


def _parse_barcode(html: str, soup: BeautifulSoup) -> Optional[str]:
    """Ürün sayfasından barkod/EAN/GTIN çıkarır. Öncelik sırası (referans:
    github.com/mas2906/ecom scraper'ı ve DevTools 'Amazon Barkod Bulucu' scripti):
    1) rhfHandlerParams.keywords — Amazon'un JS veri katmanındaki asıl EAN alanı,
       en güvenilir kaynak
    2) JSON-LD structured data (gtin13/gtin8/gtin/isbn)
    3) Teknik özellikler tablosu (th→td) — GTIN'in resmi Türkçe adı dahil
    4) Düz metin üzerinde "Barkod:"/ISBN-13 arama — bidi işaretlerine dayanıklı,
       yukarıdakilerin hepsi başarısız olursa son çare
    """
    for m in re.finditer(r'rhfHandlerParams["\s:]+(\{[^}]+\})', html):
        try:
            data = json.loads(m.group(1))
            kw = str(data.get("keywords", "")).strip()
            if re.fullmatch(r"\d{8,14}", kw):
                norm = _normalize_gtin(kw)
                if norm:
                    return norm
        except Exception:
            pass

    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ("gtin13", "gtin8", "gtin", "isbn"):
                val = data.get(key)
                if val and re.fullmatch(r"\d{8,14}", str(val).strip()):
                    norm = _normalize_gtin(str(val).strip())
                    if norm:
                        return norm
        except Exception:
            pass

    for label in ("Global Ticaret Tanımlama Numarası", "GTIN", "EAN", "UPC", "Barkod", "ISBN"):
        m = re.search(
            rf'{re.escape(label)}[^<]*</th>\s*<td[^>]*>\s*(\d{{8,14}})\s*</td>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            norm = _normalize_gtin(m.group(1))
            if norm:
                return norm

    # Son çare: rendered text üzerinde ara (etiket-değer arasında görünmez bidi
    # işaretleri ‎/‏ veya satır sonu olabiliyor — [^\d] hepsini atlayıp doğrudan
    # rakam dizisini yakalıyoruz)
    text = soup.get_text("\n")
    m = re.search(r"Barkod[^\d]{0,20}(\d[\d\s\-]{6,18}\d)", text)
    if m:
        norm = _normalize_gtin(m.group(1))
        if norm:
            return norm

    m = re.search(r"ISBN-13[^\d]{0,10}([\d\-\s]{10,20})", text)
    if m:
        norm = _normalize_gtin(m.group(1))
        if norm:
            return norm

    return None


def _parse_stock_soup(soup: BeautifulSoup, html: str) -> str:
    """
    Stok durumunu belirler (amazon_tr_scraper.py check_stock_soup).
    Döndürür: 'Stokta Var' | 'Stok Yok' | 'Bilinmiyor'
    """
    if soup.select_one("#add-to-cart-button, #buy-now-button, #add-to-cart-button-announce, #attachSiNAButton"):
        return "Stokta Var"
    if soup.select_one("#outOfStock"):
        return "Stok Yok"
    avail = soup.select_one("#availability span, #availabilityInsideBuyBox_feature_div span")
    if avail:
        av = avail.get_text(strip=True).lower()
        if any(k in av for k in ("stokta", "var", "in stock", "available", "kargo", "sipariş", "teslim")):
            return "Stokta Var"
        if any(k in av for k in _OOS_KW) or "out of stock" in av or "unavailable" in av:
            return "Stok Yok"
    h_snip = html[:8000].lower()
    if any(k in h_snip for k in _OOS_KW):
        return "Stok Yok"
    if "add-to-cart-button" in h_snip or "buy-now-button" in h_snip:
        return "Stokta Var"
    # Net sinyal yok → stokta yok say, yanlış fiyat kaydetme (referans: check_stock_soup)
    return "Stok Yok"


def _az_best_img(url: str) -> str:
    """Amazon CDN URL'sinden size modifier kaldırıp 1500px versiyonu döndürür.
    CSS sprite / bundle URL'lerini olduğu gibi bırakır."""
    if not url or "|" in url or ".css" in url.lower():
        return url
    # Image ID'leri + ve % içerebilir (örn: 81C+gMIe8yL veya 41%2BJln...)
    m = re.match(r'(https://m\.media-amazon\.com/images/I/[A-Za-z0-9+%_-]+)\.', url)
    if m:
        return f"{m.group(1)}._AC_SL1500_.jpg"
    return url


def _az_is_valid_img(url: str) -> bool:
    """Gerçek Amazon ürün görseli mi (CSS sprite/bundle değil)."""
    if not url or "|" in url or ".css" in url.lower():
        return False
    return "m.media-amazon.com/images/I/" in url


def _parse_dadi(el) -> Optional[str]:
    """data-a-dynamic-image JSON → en yüksek çözünürlüklü URL (_AC_SL1500_)."""
    dadi = el.get("data-a-dynamic-image")
    if not dadi:
        return None
    try:
        imgs = json.loads(dadi)
        if imgs:
            best = max(imgs.items(), key=lambda x: (x[1][0] * x[1][1]) if isinstance(x[1], list) and len(x[1]) >= 2 else 0)
            if best[0].startswith("http"):
                return _az_best_img(best[0])
    except Exception:
        pass
    return None


def _az_parse(html: str) -> Optional[dict]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for sel in _AZ_NOISE_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass

    # ── 1) Stok ÖNCE (referans mantığı: stok yoksa fiyat parse edilmez) ──
    stock = _parse_stock_soup(soup, html)

    # ── 2) Başlık ─────────────────────────────────────────────────────────
    title = None
    for sel in [
        "#productTitle", "h1#title span", "span#productTitle",
        "h1[data-feature-name='title']", "[data-feature-name='title'] span",
    ]:
        el = soup.select_one(sel)
        if el:
            t = re.sub(r"\s+", " ", el.get_text()).strip()
            if t and len(t) > 3 and t.lower() != "ürün galerisi":
                title = t
                break

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

    # Sadece "İkinci El" (Amazon Depo vb.) teklif var, yeni ürün satılmıyor: fiyat
    # kutusundaki tutar kullanılmış ürünün fiyatı — yeni ürün fiyatı diye kaydedilirse
    # sahte "indirim" üretir (2026-10-31, #48570 Apple Watch: 13.774 TL ikinci el).
    if (soup.select_one("#usedBuySection")
            and not soup.select_one("#newAccordionRow, #newAccordionRow_0, #buyNew_noncbb, #add-to-cart-button")):
        stock = "Stok Yok"

    # ── 3) Fiyat (stok yoksa atla — referans mantığı) ────────────────────
    price = None
    if stock != "Stok Yok":
        price = _parse_price_soup(soup)

    # ── 4) Was price + kupon ──────────────────────────────────────────────
    was_price = _parse_was_price_soup(soup)
    if was_price and price and was_price <= price:
        was_price = None

    coupon_type, coupon_value = _parse_coupon_soup(soup)

    # ── 5) Fallback zinciri (referans öncelik sırası) ─────────────────────
    # 5a) Diğer satıcılar — JSON-LD'den önce (referans sırası)
    if not price and stock != "Stok Yok":
        other_p = _get_other_sellers_price(soup)
        if other_p:
            price = other_p
            stock = "Stokta Var"

    # 5b) JSON-LD schema.org
    if not price and stock != "Stok Yok":
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

    # 5c) priceAmount regex
    if not price and stock != "Stok Yok":
        m = re.search(r'"priceAmount"[:\s]*([\d.]+)', html)
        if m:
            try:
                price = float(m.group(1))
            except Exception:
                pass

    # ── 6) Efektif fiyat — kupon sonrası gerçek ödeme (referans mantığı) ──
    effective_price = price
    if price and coupon_type and coupon_value:
        if coupon_type == "pct":
            effective_price = round(price * (1 - coupon_value / 100), 2)
        elif coupon_type == "abs":
            effective_price = round(max(price - coupon_value, 0), 2)

    coupon_text = None
    if coupon_type and coupon_value:
        coupon_text = (f"%{coupon_value:.0f} kupon" if coupon_type == "pct"
                       else f"{coupon_value:.0f} TL kupon")

    # ── 7) Görsel ─────────────────────────────────────────────────────────
    image_url = None
    for sel in ["#imgBlkFront", "#landingImage", "#main-image-container img",
                "#imgTagWrapperId img", "img.a-dynamic-image"]:
        el = soup.select_one(sel)
        if not el:
            continue
        hires = el.get("data-old-hires", "")
        if hires and _az_is_valid_img(str(hires)):
            image_url = _az_best_img(str(hires))
            break
        img_url = _parse_dadi(el)
        if img_url and _az_is_valid_img(img_url):
            image_url = img_url
            break
        src = el.get("src", "")
        if src and _az_is_valid_img(str(src)):
            image_url = _az_best_img(str(src))
            break

    if not image_url:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0]
                imgs = ld.get("image") or []
                if isinstance(imgs, str):
                    imgs = [imgs]
                if imgs and _az_is_valid_img(str(imgs[0])):
                    image_url = _az_best_img(str(imgs[0]))
                    break
            except Exception:
                pass

    if not image_url:
        m_cdn = re.search(
            r'https://m\.media-amazon\.com/images/I/([A-Za-z0-9+%_-]+)\._AC_(?:SL|SX|SY)\d+_\.jpg',
            html,
        )
        if m_cdn:
            image_url = f"https://m.media-amazon.com/images/I/{m_cdn.group(1)}._AC_SL1500_.jpg"

    # ── 8) Satıcı ─────────────────────────────────────────────────────────
    seller, is_amazon = _get_seller_info(soup)

    # ── 8b) Barkod (çapraz platform eşleştirme için) ───────────────────────
    barcode = _parse_barcode(html, soup)

    # ── 9) Rating ─────────────────────────────────────────────────────────
    rating = review_count = None
    rating_el = soup.select_one("#acrPopover, [data-hook='rating-out-of-text']")
    if rating_el:
        m = re.search(r"([\d,]+)\s+üzerinden ([\d,]+)", rating_el.get("title") or rating_el.get_text())
        if not m:
            m = re.search(r"([\d,]+)\s+out of ([\d,]+)", rating_el.get("title") or rating_el.get_text())
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except Exception:
                pass
    rc_el = soup.select_one("#acrCustomerReviewText, [data-hook='total-review-count']")
    if rc_el:
        m2 = re.search(r"([\d,.]+)", rc_el.get_text())
        if m2:
            try:
                review_count = int(m2.group(1).replace(".", "").replace(",", ""))
            except Exception:
                pass

    cart_discount = bool(was_price and price and was_price > price * 1.01)

    if not price and stock not in ("Stok Yok",):
        return None

    return {
        "title":           title,
        "price":           price,
        "was_price":       was_price,
        "effective_price": effective_price,
        "image_url":       image_url,
        "rating":          rating,
        "review_count":    review_count,
        "stock":           stock,
        "cart_discount":   cart_discount,
        "coupon":          coupon_text,
        "coupon_type":     coupon_type,
        "coupon_value":    coupon_value,
        "seller":          seller,
        "barcode":         barcode,
        "is_amazon":       is_amazon,
    }


# ── Katman 0: Playwright ───────────────────────────────────────────────────────
async def _az_pw_handler(page, url: str) -> Optional[dict]:
    # crawlee ürün sayfasını zaten açtı. Eskiden burada ana sayfaya "ısınma"
    # gidip ürünü YENİDEN açıyordu — 3 navigasyon, ürün başı ~50sn; yük altında
    # 65sn sınırını aşıp "veri yok" dönüyordu (2026-10-31 log analizi).
    if not already_on(page, url):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
    try:
        await page.wait_for_selector(
            "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, "
            "#apex_offerDisplay_desktop, #productTitle",
            timeout=6000,
        )
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.3, 0.7))
    try:
        await page.mouse.wheel(0, random.randint(250, 500))
        await asyncio.sleep(0.3)
    except Exception:
        pass

    # ASIN redirect tespiti (curl katmanındaki kontrolün Playwright karşılığı) —
    # olmadan Amazon bir ürünü başka bir ASIN'e birleştirdiğinde, yanlış ürünün
    # verisi asıl istenen product_id altına kaydedilebiliyordu.
    asin_m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if asin_m and asin_m.group(1) not in (page.url or ""):
        return {"not_found": True}

    html = await page.content()
    if not html or len(html) < 3000:
        return None
    if _az_is_notfound(html):
        return {"not_found": True}
    if _az_is_blocked(html):
        return None
    return await asyncio.to_thread(_az_parse, html)


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────
async def scrape_amazon(url: str, price_only: bool = False,
                         cached_image: Optional[str] = None) -> Optional[dict]:
    # RL.wait() artık çağıran (local_scraper.process_job) tarafında, dış
    # timeout'un DIŞINDA yapılıyor — bkz. crawlee_base.RL üzerindeki not.
    # timeout 50→65: isinma navigasyonu (_warmup_navigation) eklendi, ~15-20s ek sure gerekiyor
    data = await crawlee_pw_scrape(url, "amazon", _az_pw_handler, timeout=65)
    if data and (data.get("dead_url") or data.get("not_found")):
        return data
    return price_filter(data, price_only, cached_image) if data else None
