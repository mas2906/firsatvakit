#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Platform tespiti ve URL yönlendirme."""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse
from db import commit_with_retry

# Bilinen kısa URL domainleri
SHORT_URL_HOSTS = {"ty.gl", "app.hb.biz", "amzn.to", "amzn.eu"}


def is_short_url(url: str) -> bool:
    """Kısa link (ty.gl, app.hb.biz vb.) mi?"""
    h = urlparse(url).netloc.lower()
    return any(h == s or h.endswith("." + s) for s in SHORT_URL_HOSTS)


async def _hb_canonical_from_sku(sku: str, client) -> Optional[str]:
    """HB API'den SKU'ya karşılık gelen canonical URL'yi döndürür."""
    for endpoint in [
        f"https://www.hepsiburada.com/api/listing/sku/{sku}",
        f"https://www.hepsiburada.com/api/product/detail/{sku}",
    ]:
        try:
            r = await client.get(endpoint, headers={"Accept": "application/json", "Referer": "https://www.hepsiburada.com/"})
            if r.status_code != 200:
                continue
            data = r.json()
            product = data.get("product") or data.get("data") or data
            if isinstance(product, dict) and "product" in product:
                product = product["product"]
            canonical = product.get("canonicalUrl") or product.get("url") or product.get("productUrl")
            if canonical:
                if not canonical.startswith("http"):
                    canonical = f"https://www.hepsiburada.com{canonical}"
                return canonical.split("?")[0]
        except Exception:
            continue
    return None


async def resolve_short_url(url: str) -> str:
    """Kısa URL'yi redirect takip ederek gerçek URL'ye çevir."""
    import httpx
    from urllib.parse import parse_qs, urlparse as _up, unquote
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10, headers=headers) as client:
            r = await client.get(url)
            final = str(r.url)

            # app.hb.biz → Adjust tracker'a yönlenirse SKU'dan Hepsiburada URL'si oluştur
            parsed = _up(final)
            if "adj.st" in parsed.netloc or "adjust.com" in parsed.netloc:
                params = parse_qs(parsed.query)
                # Önce adj_deep_link içindeki sku'ya bak
                deep = params.get("adj_deep_link", params.get("adjust_deep_link", [None]))[0]
                sku = None
                if deep:
                    deep_params = parse_qs(_up(unquote(deep)).query)
                    sku = (deep_params.get("sku") or [None])[0]
                # Yoksa doğrudan sku parametresine bak
                if not sku:
                    sku = (params.get("sku") or [None])[0]
                if sku:
                    # Canonical URL almayı dene, başarısız olursa /p/ formatını döndür
                    try:
                        canonical = await _hb_canonical_from_sku(sku, client)
                        if canonical:
                            return canonical
                    except Exception:
                        pass
                    return f"https://www.hepsiburada.com/p/{sku}"

            # amzn.to / amzn.eu → Amazon ASIN'ini çıkar, canonical URL döndür
            if "amazon.com" in parsed.netloc or "amazon.com.tr" in parsed.netloc:
                m = re.search(r'/dp/([A-Z0-9]{10})', final)
                if m:
                    return f"https://www.amazon.com.tr/dp/{m.group(1)}"

            return final
    except Exception:
        return url


def detect_platform(url: str) -> str | None:
    """URL'den platform tespit et. None = desteklenmiyor."""
    h = urlparse(url).netloc.lower()
    if "amazon.com.tr" in h or "amzn" in h:
        return "amazon"
    if "trendyol.com" in h:
        return "trendyol"
    if "n11.com" in h:
        return "n11"
    if "hepsiburada.com" in h:
        return "hepsiburada"
    return None


def extract_product_id(url: str, platform: str) -> str | None:
    """URL'den ürün ID / ASIN çıkar."""
    if platform == "amazon":
        m = re.search(r"/dp/([A-Z0-9]{10})", url)
        return m.group(1) if m else None
    if platform == "trendyol":
        # https://www.trendyol.com/marka/urun-p-12345678
        m = re.search(r"-p-(\d+)", url)
        return m.group(1) if m else None
    if platform == "n11":
        # https://www.n11.com/urun/urun-adi-123456789
        m = re.search(r"(\d{8,})", url)
        return m.group(1) if m else None
    if platform == "hepsiburada":
        # https://www.hepsiburada.com/p/HBCV000048L6HL
        m = re.search(r"/p/([A-Z0-9]+)", url.split("?")[0])
        if m: return m.group(1)
        # https://www.hepsiburada.com/urun-adi-pm-HBC00001234567
        m = re.search(r"-(pm-[A-Z0-9]+)$", url.split("?")[0])
        if m: return m.group(1)
        m = re.search(r"(HBC[A-Z0-9]+)", url)
        return m.group(1) if m else None
    return None


def enqueue_url(db, url: str, platform: str) -> int:
    """Ürünü DB'ye kaydet ve tarama kuyruğuna ekle. product_id döner."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    product_id_str = extract_product_id(url, platform)

    # Canonical URL oluştur (tracking parametrelerini temizle)
    clean_url = clean_tracking_params(url, platform)

    # Ürün kaydı
    cur = db.execute("""
        INSERT OR IGNORE INTO products(platform, asin_or_id, source_url, first_seen_at, last_seen_at)
        VALUES(?,?,?,?,?)
    """, (platform, product_id_str, clean_url, now, now))
    commit_with_retry(db)

    if cur.rowcount > 0:
        product_id = cur.lastrowid
    else:
        row = db.execute("SELECT id FROM products WHERE source_url=?", (clean_url,)).fetchone()
        product_id = row["id"]

    # Kuyruğa ekle (zaten pending/processing'de varsa tekrar ekleme)
    existing = db.execute(
        "SELECT id FROM scan_queue WHERE product_id=? AND status IN ('pending','processing')",
        (product_id,)
    ).fetchone()
    if not existing:
        db.execute("""
            INSERT INTO scan_queue(product_id, url, platform, status, priority, created_at)
            VALUES(?,?,?,'pending',0,?)
        """, (product_id, clean_url, platform, now))
        commit_with_retry(db)

    return product_id


def clean_tracking_params(url: str, platform: str) -> str:
    """Tracking parametrelerini temizle, temiz URL döndür."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)

    # Silinecek parametreler (hepsi lowercase — k.lower() ile karşılaştırılır)
    remove_keys = {
        "ref", "ref_", "tag", "linkcode", "camp", "creative",
        "creativeasin", "ascsubtag", "utm_source", "utm_medium",
        "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "dclid", "_ga",
        # Trendyol spesifik
        "boutiqueid", "merchantid", "sav",
        # Amazon spesifik
        "pd_rd_w", "pd_rd_r", "pd_rd_wg", "pf_rd_p", "pf_rd_r",
        "content-id", "_encoding", "aref", "sp_csd", "psc",
    }

    clean = {k: v for k, v in params.items() if k.lower() not in remove_keys}

    # Hepsiburada için temizle — magaza (satıcı) parametresini koru
    if platform == "hepsiburada":
        from urllib.parse import urlencode
        base = url.split("?")[0]
        keep = {k: v for k, v in params.items() if k.lower() == "magaza"}
        return (base + "?" + urlencode(keep, doseq=True)) if keep else base

    # Amazon için sadece /dp/{ASIN} kısmını tut
    if platform == "amazon":
        m = re.search(r"(/dp/[A-Z0-9]{10})", parsed.path)
        if m:
            return f"https://www.amazon.com.tr{m.group(1)}"

    # Trendyol için query param'ları tamamen temizle (sadece path yeterli)
    if platform == "trendyol":
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    # N11 için magaza (satıcı) parametresini koru — farklı satıcılar farklı fiyat gösterir
    if platform == "n11":
        base = url.split("?")[0]
        keep = {k: v for k, v in params.items() if k.lower() == "magaza"}
        # Varyant parametrelerini de koru — renk/beden/kapasite farklı fiyat gösterebilir
        _n11_variants = {"renk", "beden", "kapasite", "boyut", "model"}
        keep.update({k: v for k, v in params.items() if k.lower() in _n11_variants})
        return (base + "?" + urlencode(keep, doseq=True)) if keep else base

    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(clean, doseq=True), ""
    ))
