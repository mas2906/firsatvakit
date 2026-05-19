#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FırsatVakti — Merkezi sabitler.
Dağınık hardcoded değerler yerine burayı kullan.
"""

import os
import logging

# ── Loglama ──────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)

def get_logger(name: str) -> logging.Logger:
    """Modül logger'ı döndür."""
    return logging.getLogger(name)


# ── Scraper ───────────────────────────────────────────────────────
# Platform başına istekler arası min bekleme (saniye)
PLATFORM_DELAY = {
    "amazon":      3.0,
    "trendyol":    2.0,
    "hepsiburada": 5.0,
    "n11":         3.0,
}

# Platform başına maksimum eş zamanlı Playwright instance
PLAYWRIGHT_CONCURRENCY = {
    "amazon":      1,
    "trendyol":    1,
    "hepsiburada": 1,
    "n11":         1,
}

# Scraper timeout (saniye)
SCRAPER_TIMEOUT = 75

# Başarısız scrape'den sonra "dead" sayılmak için max deneme
MAX_RETRY = 3

# ── Yeniden tarama aralıkları (dakika) ──────────────────────────
RESCAN_WATCHLIST    = 20    # Günde 72 kez
RESCAN_ACTIVE_DEAL  = 10    # Günde 144 kez (aktif deal)
RESCAN_NORMAL       = 30    # Günde 48 kez
RESCAN_OUT_OF_STOCK = 360   # Stokta yok → 6 saatte bir
RESCAN_VOLATILE     = 15    # Çok sık değişen

# ── Deal kuralları ───────────────────────────────────────────────
# Kaç saattir kontrol edilmemiş deal'lar pasife alınsın
AUTO_EXPIRE_HOURS = 12

# Minimum indirim yüzdesi (deal oluşturmak için)
MIN_DISCOUNT_PCT = 5.0

# Şüpheli indirim eşiği (bu üzerinde stok doğrulanmalı)
SUSPICIOUS_DISCOUNT_PCT = 75.0

# Muhtemelen veri hatası (bu üzerinde deal oluşturma)
MAX_DISCOUNT_PCT = 90.0

# ── Kullanıcı limitleri ──────────────────────────────────────────
GUEST_DAILY_SUBMIT_LIMIT  = 3
MEMBER_DAILY_SUBMIT_LIMIT = 20
MAX_WATCHLIST_PER_USER    = 100

# Yorum cooldown (saniye)
COMMENT_COOLDOWN_SECS = 60

# Günlük max yorum
COMMENT_DAILY_MAX = 20

# ── Rate limiting ────────────────────────────────────────────────
# Belirli bir endpoint için window içinde max istek sayısı
RATE_LIMIT_WINDOW_SECS = 60
RATE_LIMIT_MAX_REQUESTS = {
    "submit":   5,    # Dakikada 5 link gönderme
    "contact":  3,    # Dakikada 3 iletişim formu
    "login":    10,   # Dakikada 10 giriş denemesi
    "register": 3,    # Dakikada 3 kayıt denemesi
    "comment":  5,    # Dakikada 5 yorum
}

# ── Cache ────────────────────────────────────────────────────────
INDEX_CACHE_TTL_SECS = 60   # Ana sayfa sorgusu için TTL
