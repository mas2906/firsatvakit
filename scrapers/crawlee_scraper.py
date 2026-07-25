#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# DEPRECATED — yeni mimari: crawlee_base + amazon/trendyol/n11/hepsiburada + router

from scrapers.amazon      import scrape_amazon
from scrapers.trendyol    import scrape_trendyol
from scrapers.n11         import scrape_n11
from scrapers.hepsiburada import scrape_hepsiburada

__all__ = ["scrape_amazon", "scrape_trendyol", "scrape_n11", "scrape_hepsiburada"]
