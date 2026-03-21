#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Platform tespiti ve URL yönlendirme."""

import re
from datetime import datetime
from urllib.parse import urlparse


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
        # https://www.hepsiburada.com/urun-adi-pm-HBC00001234567
        m = re.search(r"-(pm-[A-Z0-9]+)$", url.split("?")[0])
        if m: return m.group(1)
        m = re.search(r"(HBC\d+)", url)
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
    db.commit()

    if cur.lastrowid:
        product_id = cur.lastrowid
    else:
        row = db.execute("SELECT id FROM products WHERE source_url=?", (clean_url,)).fetchone()
        product_id = row["id"]

    # Kuyruğa ekle
    db.execute("""
        INSERT INTO scan_queue(product_id, url, platform, status, priority, created_at)
        VALUES(?,?,?,'pending',1,?)
    """, (product_id, clean_url, platform, now))
    db.commit()

    return product_id


def clean_tracking_params(url: str, platform: str) -> str:
    """Tracking parametrelerini temizle, temiz URL döndür."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)

    # Silinecek parametreler
    remove_keys = {
        "ref", "ref_", "tag", "linkCode", "camp", "creative",
        "creativeASIN", "ascsubtag", "utm_source", "utm_medium",
        "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "dclid", "_ga",
        # Trendyol spesifik
        "boutiqueId", "merchantId", "sav",
    }

    clean = {k: v for k, v in params.items() if k.lower() not in remove_keys}

    # Hepsiburada için temizle
    if platform == "hepsiburada":
        base = url.split("?")[0]
        return base

    # Amazon için sadece /dp/{ASIN} kısmını tut
    if platform == "amazon":
        m = re.search(r"(/dp/[A-Z0-9]{10})", parsed.path)
        if m:
            return f"https://www.amazon.com.tr{m.group(1)}"

    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(clean, doseq=True), ""
    ))
