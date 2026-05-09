#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Affiliate link oluşturucu.
Her platform için tag/partner ID'yi buradan ayarlayın.
"""

import os, secrets, string
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

# ── Affiliate tag'leri — kendi ID'lerinizi buraya girin ──────────
AMAZON_AFFILIATE_TAG      = os.getenv("AMAZON_TAG",    "firsatvakti-21")
TRENDYOL_AFFILIATE_ID     = os.getenv("TRENDYOL_AFF",  "firsatvakti")
N11_AFFILIATE_ID          = os.getenv("N11_AFF",        "firsatvakti")
HEPSIBURADA_AFFILIATE_ID  = os.getenv("HB_AFF",         "firsatvakti")
# ─────────────────────────────────────────────────────────────────


def build_affiliate_url(source_url: str, platform: str, asin_or_id: str = None) -> str:
    """Kaynak URL'ye platform'a özgü affiliate parametreleri ekle."""
    if platform == "amazon":
        return _amazon_affiliate(source_url, asin_or_id)
    if platform == "trendyol":
        return _trendyol_affiliate(source_url)
    if platform == "n11":
        return _n11_affiliate(source_url)
    if platform == "hepsiburada":
        return _hepsiburada_affiliate(source_url)
    return source_url


def _amazon_affiliate(url: str, asin: str = None) -> str:
    """Amazon affiliate linki. tag parametresi ekler."""
    if asin:
        base = f"https://www.amazon.com.tr/dp/{asin}"
    else:
        base = url.split("?")[0]
    return f"{base}?tag={AMAZON_AFFILIATE_TAG}&linkCode=ogi&th=1&psc=1"


def _trendyol_affiliate(url: str) -> str:
    """Trendyol affiliate linki."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    params["affiliateId"] = [TRENDYOL_AFFILIATE_ID]
    params["channelCode"] = ["AFFILIATE"]
    new_query = urlencode(params, doseq=True)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, ""
    ))


def _n11_affiliate(url: str) -> str:
    """N11 affiliate linki."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    # magaza= parametresini kaldır — affiliate link her zaman genel ürün sayfasına gitsin
    for k in list(params.keys()):
        if k.lower() in {"magaza", "magaza_id", "seller", "sellerid"}:
            del params[k]
    partner_id = N11_AFFILIATE_ID.strip()
    params["partnerId"] = [partner_id]
    new_query = urlencode(params, doseq=True)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, ""
    ))


def _hepsiburada_affiliate(url: str) -> str:
    """Hepsiburada affiliate linki."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    params["magaza"] = [HEPSIBURADA_AFFILIATE_ID]
    new_query = urlencode(params, doseq=True)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, ""
    ))


def make_short_slug(length: int = 7) -> str:
    """7 karakterlik benzersiz kısa slug üret. örn: aX3kP9m"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
