#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Çapraz Platform Ürün Arama & Fiyat Karşılaştırma — v2."""

import re, asyncio, random, json, logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("cross_search")

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

_IMPERSONATE = ["chrome124", "chrome123", "chrome120"]

_REFERERS = {
    "amazon":      "https://www.amazon.com.tr/",
    "trendyol":    "https://www.trendyol.com/",
    "n11":         "https://www.n11.com/",
    "hepsiburada": "https://www.hepsiburada.com/",
}


async def _get(url: str, extra_headers: dict = None) -> Optional[str]:
    """curl_cffi varsa onu kullan (Cloudflare bypass), yoksa httpx."""
    if CURL_AVAILABLE:
        try:
            # curl_cffi impersonation tüm browser header'larını otomatik ayarlar;
            # herhangi bir özel header (Referer dahil) TLS fingerprint'i bozup 503'e yol açar
            async with CurlSession(impersonate=random.choice(_IMPERSONATE)) as s:
                r = await s.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
        except Exception as e:
            print(f"[cross/curl] {url[:60]} → {e}")
    # httpx fallback — tam header seti kullan
    hdrs = {**HEADERS, **(extra_headers or {})}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers=hdrs)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"[cross/httpx] {url[:60]} → {e}")
    return None


async def _hb_canonical_url(sku: str) -> Optional[str]:
    """HB /p/SKU formatını canonical ürün URL'sine çevirir."""
    for endpoint in [
        f"https://www.hepsiburada.com/api/listing/sku/{sku}",
        f"https://www.hepsiburada.com/api/product/detail/{sku}",
    ]:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                r = await c.get(endpoint, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                    "Referer": "https://www.hepsiburada.com/",
                })
            if r.status_code != 200:
                continue
            data = r.json()
            product = data.get("product") or data.get("data") or data
            if isinstance(product, dict) and "product" in product:
                product = product["product"]
            # Canonical URL veya slug'dan oluştur
            canonical = product.get("canonicalUrl") or product.get("url") or product.get("productUrl")
            if canonical:
                if not canonical.startswith("http"):
                    canonical = f"https://www.hepsiburada.com{canonical}"
                return canonical.split("?")[0]
            # slug + productId kombinasyonu
            slug = product.get("slug") or product.get("urlSlug")
            pid = product.get("sku") or product.get("id") or product.get("productId")
            if slug and pid:
                return f"https://www.hepsiburada.com/{slug}-pm-{pid}"
        except Exception:
            continue
    return None


def _normalize_barcode(raw: str) -> Optional[str]:
    """Variant suffix'ini at (4210201313908-2 → 4210201313908), saf rakam kontrolü yap."""
    bc = raw.split("-")[0]
    bc = re.sub(r'[^\d]', '', bc)
    return bc if 8 <= len(bc) <= 14 else None


def _all_barcodes_from_page(platform: str, html: str) -> list:
    """Ürün sayfasındaki tüm barkodları döndürür (variant'lar dahil)."""
    if not html:
        return []
    if platform == "n11":
        return list(dict.fromkeys(re.findall(r'"gtin\d*"\s*:\s*"(\d{8,14})"', html)))
    if platform == "trendyol":
        found = list(dict.fromkeys(re.findall(r'"barcode"\s*:\s*"(\d{8,14})"', html)))
        # <li>Barkod No: 4210201313908</li>
        found += re.findall(r'Barkod\s*No\s*:\s*(\d{8,14})', html)
        return list(dict.fromkeys(found))
    if platform == "hepsiburada":
        found = []
        # "product_barcode(s)": ["..."] veya "product_barcode": "..."
        for raw in re.findall(r'"product_barcodes?"\s*:\s*\[?"([^"]{6,20})"', html):
            bc = _normalize_barcode(raw)
            if bc: found.append(bc)
        # HERMES: "barcode":["4210201313908-2"] — array içinde, variant suffix'li
        for raw in re.findall(r'"barcode"\s*:\s*\["([^"]{6,20})"', html):
            bc = _normalize_barcode(raw)
            if bc: found.append(bc)
        # "barcode":"4210201313908" — string format (bazı HB sayfalarda)
        for raw in re.findall(r'"barcode"\s*:\s*"(\d[^"]{5,19})"', html):
            bc = _normalize_barcode(raw)
            if bc: found.append(bc)
        return list(dict.fromkeys(found))
    if platform == "amazon":
        found = re.findall(r'"gtin\d*"\s*:\s*"(\d{8,14})"', html, re.IGNORECASE)
        found += re.findall(r'(?:EAN|GTIN|Barkod|UPC)[^\d<]{0,30}(\d{8,14})', html, re.IGNORECASE)
        return list(dict.fromkeys(found))
    return []


def _extract_barcode_from_page(platform: str, html: str) -> Optional[str]:
    """Ürün sayfası HTML'inden barkod çıkar."""
    if not html:
        return None
    if platform == "n11":
        # JSON-LD: "gtin8":"6932485009435" veya "gtin13":"..."
        m = re.search(r'"gtin\d+"\s*:\s*"(\d{8,14})"', html)
        if m: return m.group(1)
    elif platform == "trendyol":
        # JSON içinde "barcode":"8694740411947" (winnerVariant veya genel)
        m = re.search(r'"barcode"\s*:\s*"(\d{8,14})"', html)
        if m: return m.group(1)
        # <li> içinde "Barkod No: ..." (eski format)
        m = re.search(r'Barkod\s*No\s*:\s*(\d{8,14})', html)
        if m: return m.group(1)
    elif platform == "hepsiburada":
        # HERMES: "barcode":["4210201313908-2"] — variant suffix'li array format
        m = re.search(r'"barcode"\s*:\s*\["(\d+)(?:-\d+)?"', html)
        if m: return m.group(1)
        # utagData: "product_barcode(s)"
        m = re.search(r'"product_barcodes"\s*:\s*\["([^"]{6,20})"', html)
        if m: return _normalize_barcode(m.group(1))
        m = re.search(r'"product_barcode"\s*:\s*"(\d{8,14})"', html)
        if m: return m.group(1)
    elif platform == "amazon":
        # Teknik detaylar: "EAN" veya "GTIN" etiketi → yanındaki değer
        m = re.search(r'(?:EAN|GTIN|Barkod|UPC)\s*[:\-]?\s*</[^>]+>\s*<[^>]+>\s*(\d{8,14})', html, re.IGNORECASE)
        if m: return m.group(1)
        m = re.search(r'(?:EAN|GTIN|Barkod|UPC)[^\d<]{0,30}(\d{8,14})', html, re.IGNORECASE)
        if m: return m.group(1)
        # JSON-LD veya meta içinde
        m = re.search(r'"gtin\d*"\s*:\s*"(\d{8,14})"', html, re.IGNORECASE)
        if m: return m.group(1)
        # <td> içinde 13 haneli EAN
        m = re.search(r'<td[^>]*>\s*(\d{13})\s*</td>', html)
        if m: return m.group(1)
    return None


def _title_from_url_slug(url: str) -> str:
    """URL slug'ından arama sorgusu üret. title yokken fallback."""
    if not url:
        return ""
    path = url.split("?")[0].rstrip("/").split("/")[-1]
    # HB: ...ürün-adı-p-HBCV000... → -p-HB öncesini al
    path = re.sub(r"-p-HB\w+$", "", path, flags=re.IGNORECASE)
    # Trendyol: ...ürün-adı-p-123456 → -p- öncesini al
    path = re.sub(r"-p-\d+$", "", path)
    # N11: ...ürün-adı-950296 → sayısal son kısmı at
    path = re.sub(r"-\d{5,}$", "", path)
    title = path.replace("-", " ").strip()
    # İlk 6 kelime yeterli
    return " ".join(title.split()[:6]) if len(title) > 4 else ""


async def cross_search_product(db, product_id: int, search_platforms: list = None):
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return

    title = product["title"]
    # title yoksa URL slug'ından üret
    if not title:
        title = _title_from_url_slug(product.get("source_url", ""))
        if not title:
            return

    src = product["platform"]
    all_p = ["amazon", "trendyol", "n11", "hepsiburada"]
    targets = [p for p in (search_platforms or all_p) if p != src]

    barcode = product["barcode"] if "barcode" in product.keys() else None

    # Barkod yoksa kaynak ürün sayfasını çekip barkodu bulmaya çalış
    if not barcode:
        source_url = product["source_url"] if "source_url" in product.keys() else None
        if source_url:
            fetch_url = source_url
            # HB /p/SKU formatı web'de çalışmıyor → başlıkla arama yap
            if src == "hepsiburada" and "/p/" in source_url:
                src_title = product["title"] if "title" in product.keys() else None
                if src_title:
                    hb_results = await _search_hb(src_title[:40])
                    if hb_results and hb_results[0].get("url"):
                        fetch_url = hb_results[0]["url"]
                        print(f"[cross] HB /p/ URL çözüldü → {fetch_url[:60]}")
            print(f"[cross] barkod DB'de yok, kaynak sayfa çekiliyor: {fetch_url[:60]}")
            page_html = await _get(fetch_url, {"Referer": _REFERERS.get(src, "")})
            barcode = _extract_barcode_from_page(src, page_html) if page_html else None
            if barcode:
                db.execute("UPDATE products SET barcode=?, source_url=? WHERE id=?",
                           (barcode, fetch_url, product_id))
                db.commit()
                print(f"[cross] ✔ kaynak sayfadan barkod çekildi: {barcode}")

    group_id = _get_or_create_group(db, product_id, product)

    print(f"[cross] barcode={barcode!r} → {targets}")
    if not barcode:
        print(f"[cross] barkod bulunamadı, çapraz arama atlandı (sadece barkod eşleşmesi)")
        return

    for plat in targets:
        try:
            match = None
            results = await _search(plat, barcode)
            for r in results[:3]:
                r_url = r.get("url", "")
                if not r_url: continue
                if plat == "amazon":
                    r["barcode"] = barcode
                    match = r
                    print(f"[cross] ✔ amazon (barkod aramasından): {r.get('title','?')[:50]}")
                    break
                page_html = await _get(r_url)
                page_barcodes = _all_barcodes_from_page(plat, page_html) if page_html else []
                if barcode in page_barcodes:
                    r["barcode"] = barcode
                    match = r
                    print(f"[cross] ✔ {plat} (barkod doğrulandı): {r.get('title','?')[:50]}")
                    break
                # Barkod bulunamadıysa başlık benzerliğiyle fallback
                title_score = _title_match_score(title, r.get("title", ""))
                if title_score >= 0.40:
                    r["barcode"] = barcode
                    r["match_type"] = "title_fallback"
                    match = r
                    print(f"[cross] ✔ {plat} (başlık fallback score={title_score:.2f}): {r.get('title','?')[:50]}")
                    break
                else:
                    print(f"[cross] ✘ {plat} barkod={page_barcodes} başlık_score={title_score:.2f} beklenen={barcode!r}")
                await asyncio.sleep(0.5)
            if results and not match:
                print(f"[cross] ✘ {plat}: barkod veya başlık eşleşmesi bulunamadı")

            if match:
                _save_match(db, group_id, match, plat)
            else:
                print(f"[cross] ✘ {plat}: yok")
        except Exception as e:
            print(f"[cross] ✘ {plat}: {e}")
        await asyncio.sleep(1.5)


async def find_cross_products(barcode: str, src_platform: str, src_title: str = None) -> list:
    """WSL tarafı: HTTP-only çapraz arama. DB bağlantısı yok."""
    targets = [p for p in ["amazon", "trendyol", "n11", "hepsiburada"] if p != src_platform]
    results = []
    for plat in targets:
        try:
            search_results = await _search(plat, barcode)
            for r in search_results[:3]:
                url = r.get("url", "")
                if not url:
                    continue
                # Amazon EAN'ı ürün sayfasında göstermiyor; barkodla arama sayfasından
                # dönen ilk sonuç zaten doğru ürün olduğu için sayfa doğrulaması atlanır.
                if plat == "amazon":
                    r["platform"] = plat
                    r["barcode"] = barcode
                    results.append(r)
                    print(f"[cross] ✔ amazon (barkod aramasından): {r.get('title','?')[:50]}")
                    break
                page_html = await _get(url)
                page_barcodes = _all_barcodes_from_page(plat, page_html) if page_html else []
                if barcode in page_barcodes:
                    r["platform"] = plat
                    r["barcode"] = barcode
                    results.append(r)
                    print(f"[cross] ✔ {plat} barkod doğrulandı: {r.get('title','?')[:50]}")
                    break
                # Barkod bulunamadıysa başlık benzerliğiyle fallback
                if src_title:
                    title_score = _title_match_score(src_title, r.get("title", ""))
                    if title_score >= 0.40:
                        r["platform"] = plat
                        r["barcode"] = barcode
                        r["match_type"] = "title_fallback"
                        results.append(r)
                        print(f"[cross] ✔ {plat} başlık fallback score={title_score:.2f}: {r.get('title','?')[:50]}")
                        break
                    else:
                        print(f"[cross] ✘ {plat} barkod={page_barcodes} başlık_score={title_score:.2f}")
                else:
                    print(f"[cross] ✘ {plat} barkod={page_barcodes} beklenen={barcode!r}")
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[cross/{plat}] {e}")
        await asyncio.sleep(1.5)
    return results


def save_cross_search_results(db, product_id: int, results: list):
    """VPS tarafı: WSL'den gelen çapraz arama sonuçlarını DB'ye kaydet."""
    if not results:
        return
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return
    group_id = _get_or_create_group(db, product_id, product)
    for r in results:
        platform = r.get("platform")
        if not platform:
            continue
        # Barkod doğrulaması olmadan gelen sonuçları kaydetme
        if not r.get("barcode"):
            print(f"[cross/save] {platform} sonucu barkod içermiyor, atlandı")
            continue
        _save_match(db, group_id, r, platform)


def _get_or_create_group(db, pid, product) -> int:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute("SELECT group_id FROM product_group_members WHERE product_id=?", (pid,)).fetchone()
    if row: return row["group_id"]

    p = dict(product)
    cur = db.execute("INSERT INTO product_groups(name,image_url,category,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (p["title"], p.get("image_url"), p.get("category"), now, now))
    gid = cur.lastrowid
    db.execute("INSERT OR IGNORE INTO product_group_members(group_id,product_id,match_type,confidence,added_at) VALUES(?,?,'source',1.0,?)",
               (gid, pid, now))
    db.commit()
    return gid


def _save_match(db, group_id, result, platform):
    from scraper_router import clean_tracking_params
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    url = result.get("url","")
    if not url: return
    clean = clean_tracking_params(url, platform)

    db.execute("INSERT OR IGNORE INTO products(platform,asin_or_id,source_url,title,image_url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
               (platform, result.get("product_id"), clean, result.get("title"), result.get("image_url"), now, now))
    db.commit()
    row = db.execute("SELECT id FROM products WHERE source_url=?", (clean,)).fetchone()
    pid = row["id"] if row else None
    if not pid: return

    price = result.get("price")
    if price and price > 0:
        db.execute("INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,'TRY',?)", (pid, price, now))

    db.execute("INSERT OR IGNORE INTO product_group_members(group_id,product_id,match_type,confidence,added_at) VALUES(?,?,'auto_barcode',0.9,?)",
               (group_id, pid, now))
    # Zaten pending/processing kuyruğu varsa tekrar ekleme
    if not db.execute(
        "SELECT id FROM scan_queue WHERE product_id=? AND status IN ('pending','processing')", (pid,)
    ).fetchone():
        db.execute("INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) VALUES(?,?,?,'pending',2,?)",
                   (pid, clean, platform, now))
    db.commit()


# ══ Platform Arama ════════════════════════════════════════════

async def _search(platform, query) -> list:
    if platform == "trendyol":   return await _search_trendyol(query)
    if platform == "amazon":     return await _search_amazon(query)
    if platform == "n11":        return await _search_n11(query)
    if platform == "hepsiburada":return await _search_hb(query)
    return []


async def _search_trendyol(q) -> list:
    html = await _get(f"https://www.trendyol.com/sr?q={quote_plus(q)}",
                      {"Referer": "https://www.trendyol.com/"})
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("a.product-card, a[data-testid='product-card']")[:5]:
        try:  # noqa: trendyol-parse
            href = card.get("href", "")
            if not href.startswith("http"):
                href = f"https://www.trendyol.com{href}"
            mid = re.search(r"-p-(\d+)", href)
            # Başlık: img[alt] en güvenilir
            img = card.select_one("img[alt]")
            title = img.get("alt", "") if img else None
            if not title:
                name_el = card.select_one("[class*='name'],[class*='Name']")
                title = name_el.get_text(strip=True) if name_el else None
            price_el = card.select_one("[class*='price'],[class*='Price']")
            out.append({
                "url": href.split("?")[0],
                "title": title,
                "price": _price(price_el.get_text() if price_el else None),
                "product_id": mid.group(1) if mid else None,
                "image_url": img.get("src") if img else None,
            })
        except Exception as e:
            log.debug(f"[cross/trendyol] parse hatası: {e}")
    return out


async def _search_amazon(q) -> list:
    html = await _get(f"https://www.amazon.com.tr/s?k={quote_plus(q)}",
                      {"Referer": "https://www.amazon.com.tr/"})
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("[data-asin]")[:5]:
        asin = item.get("data-asin")
        if not asin or len(asin) != 10: continue
        t = item.select_one("h2 a span, .a-text-normal")
        pw = item.select_one(".a-price-whole")
        pf = item.select_one(".a-price-fraction")
        img = item.select_one("img.s-image")
        price = None
        if pw:
            ps = pw.get_text(strip=True).replace(".","").replace(",",".")
            if pf: ps += pf.get_text(strip=True)
            try: price = float(ps)
            except Exception: pass
        out.append({"url": f"https://www.amazon.com.tr/dp/{asin}",
                     "title": t.get_text(strip=True) if t else None,
                     "price": price, "product_id": asin,
                     "image_url": img.get("src") if img else None})
    return out


async def _search_n11(q) -> list:
    html = await _get(f"https://www.n11.com/arama?q={quote_plus(q)}",
                      {"Referer": "https://www.n11.com/"})
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("a.product-item")[:5]:
        try:
            href = item.get("href", "")
            if not href: continue
            if not href.startswith("http"):
                href = f"https://www.n11.com{href}"
            t = item.select_one(".product-item-title")
            p = (item.select_one(".price-wrapper .newPrice ins")
                 or item.select_one(".newPrice ins")
                 or item.select_one(".price-wrapper .newPrice")
                 or item.select_one(".newPrice")
                 or item.select_one(".price-currency, .basket-price, .price-area"))
            img = item.select_one("img")
            mid = re.search(r"-(\d+)$", href.rstrip("/"))
            out.append({
                "url": href.split("?")[0],
                "title": t.get_text(strip=True) if t else None,
                "price": _price(p.get_text() if p else None),
                "product_id": mid.group(1) if mid else None,
                "image_url": img.get("src") if img else None,
            })
        except Exception as e:
            log.debug(f"[cross/n11] parse hatası: {e}")
    return out


async def _search_hb(q) -> list:
    # Önce HB'nin listing JSON API'sini dene (JS render gerekmez)
    try:
        api_url = f"https://www.hepsiburada.com/listing?q={quote_plus(q)}&pageSize=5"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            r = await c.get(api_url, headers={
                **HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.hepsiburada.com/",
            })
        if r.status_code == 200:
            data = r.json()
            products = (data.get("products") or data.get("data", {}).get("products") or [])
            if products:
                out = []
                for p in products[:5]:
                    slug = p.get("url") or p.get("productSlug") or p.get("slug") or ""
                    sku  = p.get("sku") or p.get("id") or ""
                    url  = f"https://www.hepsiburada.com{slug}" if slug.startswith("/") else slug
                    if not url and sku:
                        url = f"https://www.hepsiburada.com/p/{sku}"
                    price_val = p.get("basketPrice") or p.get("basketDiscountedPrice") or p.get("price") or p.get("salePrice") or p.get("originalPrice")
                    out.append({
                        "url": url.split("?")[0] if url else "",
                        "title": p.get("name") or p.get("title"),
                        "price": float(price_val) if price_val else None,
                        "product_id": str(sku),
                    })
                return [x for x in out if x["url"]]
    except Exception:
        pass

    # Fallback: HTML arama sayfası
    html = await _get(f"https://www.hepsiburada.com/ara?q={quote_plus(q)}",
                      {"Referer": "https://www.hepsiburada.com/"})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    # JSON içinde gömülü ürün verisi ara (SSR)
    m = re.search(r'"products"\s*:\s*(\[.*?\])\s*,\s*"(?:total|count|pagination)"', html, re.DOTALL)
    if m:
        try:
            products = json.loads(m.group(1))
            out = []
            for p in products[:5]:
                slug = p.get("url") or p.get("productSlug") or ""
                sku  = p.get("sku") or p.get("id") or ""
                url  = f"https://www.hepsiburada.com{slug}" if slug.startswith("/") else slug
                if not url and sku:
                    url = f"https://www.hepsiburada.com/p/{sku}"
                price_val = p.get("basketPrice") or p.get("basketDiscountedPrice") or p.get("price") or p.get("salePrice")
                out.append({
                    "url": url.split("?")[0] if url else "",
                    "title": p.get("name") or p.get("title"),
                    "price": float(price_val) if price_val else None,
                    "product_id": str(sku),
                })
            results = [x for x in out if x["url"]]
            if results:
                return results
        except Exception:
            pass

    # Son çare: CSS selector (HB HTML değiştiyse az sonuç verir)
    out = []
    selectors = [
        "li[class*='productListContent']",
        "[data-test-id='product-card-item']",
        "li[class*='ProductCard']",
        "div[class*='product-card']",
    ]
    items = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            break
    for item in items[:5]:
        try:
            a = item.select_one("a[href*='/']")
            if not a:
                continue
            href = a.get("href", "")
            if not href.startswith("http"):
                href = f"https://www.hepsiburada.com{href}"
            t = item.select_one("h2[class*='title'], h3[class*='title'], h2, h3, [data-test-id*='name']")
            p = (item.select_one("[data-test-id='basket-discount-price'],[class*='basketFinalPrice']") or
                 item.select_one("[class*='finalPrice'],[data-test-id*='final-price']") or
                 item.select_one("[class*='price']"))
            hbid = re.search(r"-(HB\w+)", href)
            out.append({
                "url": href.split("?")[0],
                "title": t.get_text(strip=True) if t else None,
                "price": _price(p.get_text() if p else None),
                "product_id": hbid.group(1) if hbid else None,
            })
        except Exception as e:
            log.debug(f"[cross/hepsiburada] parse hatası: {e}")
    return out


# ══ Fiyat Karşılaştırma ══════════════════════════════════════

def get_price_comparison(db, product_id: int) -> dict:
    member = db.execute("SELECT group_id FROM product_group_members WHERE product_id=?", (product_id,)).fetchone()
    if not member:
        return {"group": None, "products": [], "cheapest": None, "savings": None}

    group = db.execute("SELECT * FROM product_groups WHERE id=?", (member["group_id"],)).fetchone()
    members = db.execute("""
        SELECT p.*, pgm.confidence,
               (SELECT ph.price_value FROM price_history ph WHERE ph.product_id=p.id ORDER BY ph.id DESC LIMIT 1) as current_price,
               (SELECT ph.scraped_at  FROM price_history ph WHERE ph.product_id=p.id ORDER BY ph.id DESC LIMIT 1) as price_checked_at,
               (SELECT d.cart_discount FROM deals d WHERE d.product_id=p.id ORDER BY d.id DESC LIMIT 1) as cart_discount,
               (
                   SELECT CASE WHEN ph2.scraped_at >= datetime('now', '-2 hours') THEN 1 ELSE 0 END
                   FROM price_history ph2 WHERE ph2.product_id=p.id ORDER BY ph2.id DESC LIMIT 1
               ) as price_is_fresh
        FROM product_group_members pgm
        JOIN products p ON pgm.product_id = p.id
        WHERE pgm.group_id=?
          AND pgm.match_type != 'removed'
          AND (p.stock IS NULL OR p.stock != 'Stok Yok')
        ORDER BY current_price ASC NULLS LAST
    """, (member["group_id"],)).fetchall()

    products = [dict(m) for m in members]
    priced = [p for p in products if p.get("current_price")]
    # Cheapest hesaplamasında sadece güncel fiyatları (son 2 saat) kullan
    fresh_priced = [p for p in priced if p.get("price_is_fresh")]
    compare_pool = fresh_priced if fresh_priced else priced
    cheapest = min(compare_pool, key=lambda x: x["current_price"]) if compare_pool else None
    expensive = max(compare_pool, key=lambda x: x["current_price"]) if compare_pool else None
    savings = None
    if cheapest and expensive and cheapest["id"] != expensive["id"]:
        diff = expensive["current_price"] - cheapest["current_price"]
        savings = {"amount": round(diff,2),
                   "percent": round(diff/expensive["current_price"]*100, 1),
                   "cheapest_platform": cheapest["platform"]}

    return {"group": dict(group) if group else None, "products": products,
            "cheapest": cheapest, "savings": savings}


def _title_match_score(title_a: str, title_b: str) -> float:
    """İki ürün başlığı arasında kelime örtüşme skoru döner (0.0–1.0).
    Yanlış barkod-dışı eşleşmeleri engellemek için kullanılır."""
    def _tokens(t):
        t = re.sub(r"[^\w\s]", " ", (t or "").lower())
        stops = {"ve", "ile", "için", "bir", "bu", "da", "de", "the", "and", "with", "for"}
        return {w for w in t.split() if len(w) > 2 and w not in stops}
    a, b = _tokens(title_a), _tokens(title_b)
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def _price(text) -> Optional[float]:
    if not text: return None
    text = re.sub(r'[^\d.,]', '', text.strip())
    if not text: return None
    if "," in text and "." in text:
        # Türkçe format: 6.789,00 → binlik=nokta, ondalık=virgül
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        # Sadece virgül: 6789,00 → ondalık virgül
        text = text.replace(",", ".")
    elif "." in text:
        # Sadece nokta: Türkçe'de binlik ayraç olabilir (6.789)
        # Ondalık değilse (son kısım 3 rakam) → binlik ayraç
        parts = text.split(".")
        if len(parts[-1]) == 3:
            text = text.replace(".", "")
        # Aksi halde ondalık nokta kabul et (6.5 gibi)
    try: return float(text)
    except: return None
