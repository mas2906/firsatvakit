#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper veri modelleri — ProductResult ve ScraperConfig.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ProductResult:
    """
    Tüm platform scraper'larının döndürdüğü standart ürün verisi.
    dead_url / not_found → ürün kaldırılmış, DB'den pasife al.
    """
    url:          str
    platform:     str
    title:        Optional[str]   = None
    price:        Optional[float] = None
    stock:        Optional[str]   = None
    image_url:    Optional[str]   = None
    barcode:      Optional[str]   = None
    cart_discount: bool           = False
    coupon:       Optional[str]   = None
    dead_url:     bool            = False
    not_found:    bool            = False

    @property
    def is_valid(self) -> bool:
        """Fiyat ve başlık var, ürün yaşıyor."""
        return bool(self.title and self.price and not self.dead_url and not self.not_found)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScraperConfig:
    """
    Retry, backoff, concurrency ve rate limit yapılandırması.
    Tüm platform scraper'ları bu config'i paylaşır veya kendi kopyasını alır.
    """
    max_retries:       int   = 3      # İstek başarısız olursa kaç kez tekrar dene
    backoff_base:      float = 2.0    # İlk retry bekleme süresi (saniye)
    backoff_max:       float = 60.0   # Bekleme üst sınırı (saniye)
    jitter_factor:     float = 0.3    # Backoff'a eklenen rastgele oran (0–1)
    concurrency:       int   = 2      # Domain başına eş zamanlı maksimum istek
    timeout:           float = 30.0   # Tek istek timeout (saniye)
    request_delay_min: float = 0.8    # İstekler arası minimum bekleme (saniye)
    request_delay_max: float = 3.5    # İstekler arası maksimum bekleme (saniye)
