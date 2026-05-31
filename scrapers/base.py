#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform bazlı scraper arayüzü ve platform implementasyonları.

Mimari (fallback zinciri):
  1. TLS Fingerprint    — curl_cffi Chrome/Safari JA3 taklidi
  2. JS Rendering       — Playwright headless Chrome
  3. Retry + Backoff    — üstel bekleme + jitter, domain concurrency limiti
  4. Headers & Cookies  — fingerprint.py PROFILES'tan platform uyumlu header seti

Her platform:
  - Mevcut scrape_* fonksiyonunu çağıran thin wrapper
  - `parse()` metodu: ham HTML → ProductResult (LayeredFetcher fallback için)
  - `scrape()` metodu: tam döngü (rate limit → domain limit → fetch → retry)

Proxy / IP rotasyonu YOK.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Optional

from scrapers.models  import ProductResult, ScraperConfig
from scrapers.engine  import RetryEngine, TerminalError, BlockedError, get_limiter
from scrapers.fingerprint import LayeredFetcher, content_ok

log = logging.getLogger("scraper.base")

# Domain → hostname eşleşmesi (DomainLimiter için)
_DOMAIN_MAP: dict[str, str] = {
    "amazon":      "www.amazon.com.tr",
    "trendyol":    "www.trendyol.com",
    "hepsiburada": "www.hepsiburada.com",
    "n11":         "www.n11.com",
}


def _to_product(raw: Optional[dict], url: str, platform: str) -> Optional[ProductResult]:
    """Mevcut scraper dict çıktısını ProductResult'a dönüştür."""
    if not raw or not raw.get("title"):
        return None
    return ProductResult(
        url          = url,
        platform     = platform,
        title        = raw.get("title"),
        price        = raw.get("price"),
        stock        = raw.get("stock"),
        image_url    = raw.get("image_url"),
        barcode      = raw.get("barcode"),
        cart_discount = bool(raw.get("cart_discount")),
        coupon       = raw.get("coupon"),
    )


async def _request_delay(cfg: ScraperConfig) -> None:
    """İstekler arası yapılandırılabilir gecikme (rate limiting)."""
    await asyncio.sleep(random.uniform(cfg.request_delay_min, cfg.request_delay_max))


# ═══════════════════════════════════════════════════════════════
# TABAN SINIF
# ═══════════════════════════════════════════════════════════════

class BaseScraper(ABC):
    """
    Tüm platform scraper'ları için ortak arayüz.

    Kullanım (mevcut kod):
        scraper = TrendyolScraper(ScraperConfig(max_retries=3, concurrency=2))
        result  = await scraper.scrape(url, pool=browser_pool)

    Kullanım (factory):
        scraper = get_scraper("trendyol")
        result  = await scraper.scrape(url, pool=pool)

    Subclass gereksinimi:
        platform: str      — sınıf değişkeni, küçük harf platform adı
        parse()            — HTML → ProductResult
        scrape()           — override ile mevcut scrape_* çağrılabilir (önerilir)
    """

    platform: str = ""

    def __init__(self, config: Optional[ScraperConfig] = None):
        self.cfg      = config or ScraperConfig()
        self._fetcher = LayeredFetcher(self.platform, timeout=self.cfg.timeout)
        self._retry   = RetryEngine(self.cfg)

    # ── Soyut metotlar ──────────────────────────────────────────

    @abstractmethod
    async def parse(self, html: str, url: str) -> Optional[ProductResult]:
        """
        Ham HTML'den ürün verisini çıkar.
        Platform başına override edilir.
        Sadece BaseScraper.scrape() tarafından (LayeredFetcher path) çağrılır.
        """

    # ── Varsayılan scrape döngüsü (LayeredFetcher tabanlı) ───────

    async def scrape(self, url: str, pool=None) -> Optional[ProductResult]:
        """
        Tam scrape döngüsü:
          request delay → domain concurrency limit → LayeredFetcher (curl→PW) → parse → retry

        Platform subclass'ları bunu override ederek mevcut
        scrape_* fonksiyonlarını doğrudan çağırabilir.
        """
        domain = _DOMAIN_MAP.get(self.platform, self.platform)
        label  = f"{self.platform}:{url.split('/')[-1][:25]}"

        async def _attempt() -> Optional[ProductResult]:
            await _request_delay(self.cfg)

            async with get_limiter(self.cfg.concurrency).limit(domain):
                r = await self._fetcher.fetch(url, pool=pool)

            if r.dead_url:
                log.warning(f"[{self.platform}] dead_url: {url[:70]}")
                raise TerminalError("dead_url")
            if r.not_found:
                log.info(f"[{self.platform}] not_found: {url[:70]}")
                raise TerminalError("not_found")
            if not r.ok:
                log.debug(f"[{self.platform}] fetch başarısız layer={r.layer}")
                return None
            if not content_ok(r.html, self.platform):
                log.info(f"[{self.platform}] block algılandı layer={r.layer}")
                raise BlockedError(r.layer)

            log.info(f"[{self.platform}] ✔ fetch layer={r.layer} {len(r.html):,}b")
            return await self.parse(r.html, url)

        return await self._retry.run(_attempt, label=label)

    # ── Yardımcı: mevcut scrape_* sarmalayıcı ───────────────────

    async def _wrap(
        self,
        url:  str,
        pool: object,
        fn:   object,           # scrape_trendyol / scrape_amazon vb.
        label: str = "",
    ) -> Optional[ProductResult]:
        """
        Mevcut `scrape_*` fonksiyonlarını RetryEngine + DomainLimiter ile sar.
        Platform subclass'larının scrape() override'ında kullanılır.

        Mevcut scrape_* fonksiyonları kendi iç rate limit ve layer
        yönetimini yapar; bu method sadece dış retry + concurrency ekler.
        """
        domain = _DOMAIN_MAP.get(self.platform, self.platform)

        async def _attempt() -> Optional[ProductResult]:
            await _request_delay(self.cfg)
            async with get_limiter(self.cfg.concurrency).limit(domain):
                raw = await fn(url, pool=pool)  # type: ignore[call-arg]

            if raw is None:
                return None
            if raw.get("dead_url"):
                raise TerminalError("dead_url")
            if raw.get("not_found"):
                raise TerminalError("not_found")
            if raw.get("blocked"):
                raise BlockedError("blocked")
            return _to_product(raw, url, self.platform)

        return await self._retry.run(_attempt, label=label or self.platform)


# ═══════════════════════════════════════════════════════════════
# PLATFORM SCRAPERS
# ═══════════════════════════════════════════════════════════════

class TrendyolScraper(BaseScraper):
    """
    Trendyol scraper.

    Öncelik sırası (scrape_trendyol içinde):
      1. iOS Safari TLS curl_cffi + __envoy__PROPS JSON
      2. Chrome curl_cffi stream_fetch
      3. Playwright arama motoru
    """

    platform = "trendyol"

    async def parse(self, html: str, url: str) -> Optional[ProductResult]:
        """HTML → ProductResult. Sadece LayeredFetcher path için."""
        try:
            from scrapers.trendyol import _parse as _ty_parse
            data = await asyncio.to_thread(_ty_parse, html, url)
            return _to_product(data, url, self.platform)
        except Exception as e:
            log.debug(f"[trendyol/parse] {e}")
            return None

    async def scrape(self, url: str, pool=None) -> Optional[ProductResult]:
        """scrape_trendyol'u RetryEngine ile sar."""
        from scrapers.trendyol import scrape_trendyol
        return await self._wrap(url, pool, scrape_trendyol, label="trendyol")


class HepsiburadaScraper(BaseScraper):
    """
    HepsiB scraper.

    Akamai Bot Manager karşı katmanlar (scrape_hepsiburada içinde):
      1. iOS Safari TLS curl_cffi impersonation
         - Yanıt sonrası _abck cookie ~-1~ ise Playwright fallback
      2. Playwright headless + playwright-stealth

    parse(): __NEXT_DATA__ JSON (Next.js) birincil, HTML BS4 fallback.
    """

    platform = "hepsiburada"

    async def parse(self, html: str, url: str) -> Optional[ProductResult]:
        """HTML → ProductResult. __NEXT_DATA__ önce, sonra BS4 fallback."""
        # 1) __NEXT_DATA__ (HepsiB bazı sayfalarında Next.js kullanıyor)
        result = await asyncio.to_thread(self._parse_next_data, html, url)
        if result and result.price:
            return result

        # 2) Mevcut HB parse fonksiyonuna devret
        try:
            from scrapers.hepsiburada import _parse as _hb_parse
            data = await asyncio.to_thread(_hb_parse, html)
            return _to_product(data, url, self.platform)
        except Exception as e:
            log.debug(f"[hepsiburada/parse] {e}")

        # 3) Minimal BS4 fallback
        return await asyncio.to_thread(self._parse_bs4, html, url)

    def _parse_next_data(self, html: str, url: str) -> Optional[ProductResult]:
        """<script id="__NEXT_DATA__"> JSON'undan ürün verisi çıkar."""
        import json, re
        from scrapers.utils import parse_price_tr_clean
        m = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.+?\})\s*</script>',
            html, re.S
        )
        if not m:
            return None
        try:
            nd      = json.loads(m.group(1))
            pp      = nd.get("props", {}).get("pageProps", {})
            product = (
                pp.get("product")
                or pp.get("productDetail", {}).get("product")
                or pp.get("productInfo", {}).get("product")
            )
            if not product:
                return None

            # Fiyat — farklı alan adları denenir
            price: Optional[float] = None
            for key in ("price", "salePrice", "currentPrice", "discountedPrice"):
                raw = product.get(key)
                if isinstance(raw, (int, float)) and raw > 0:
                    price = float(raw)
                    break
                if isinstance(raw, dict):
                    for sub in ("value", "sellingPrice", "amount"):
                        v = raw.get(sub)
                        if isinstance(v, (int, float)) and v > 0:
                            price = float(v)
                            break
                if price:
                    break
                if isinstance(raw, str):
                    price = parse_price_tr_clean(raw)
                    if price:
                        break

            title = product.get("name") or product.get("displayName") or product.get("title")
            if not title or not price:
                return None

            stock_raw = product.get("isAvailable") or product.get("inStock") or product.get("hasStock")
            stock     = "Stokta Var" if stock_raw else "Stok Yok"

            images    = product.get("images") or []
            image_url = images[0].get("url") if images and isinstance(images[0], dict) else (
                images[0] if images and isinstance(images[0], str) else None
            )

            return ProductResult(
                url      = url,
                platform = self.platform,
                title    = str(title),
                price    = price,
                stock    = stock,
                image_url = image_url,
                barcode  = product.get("sku") or product.get("barcode"),
            )
        except Exception as e:
            log.debug(f"[hepsiburada/__NEXT_DATA__] parse hatası: {e}")
            return None

    def _parse_bs4(self, html: str, url: str) -> Optional[ProductResult]:
        """Minimal BeautifulSoup fallback — başlık + fiyat yeterli."""
        from bs4 import BeautifulSoup
        from scrapers.utils import parse_price_tr_clean
        soup = BeautifulSoup(html, "lxml")

        title_el = (
            soup.select_one("h1.product-name")
            or soup.select_one("h1[data-test-id='title']")
            or soup.select_one("h1[class*='product']")
            or soup.select_one("h1")
        )
        price_el = (
            soup.select_one("[data-test-id='price-current-price']")
            or soup.select_one(".price-value")
            or soup.select_one("[class*='price']")
        )
        title = title_el.get_text(strip=True) if title_el else None
        price = parse_price_tr_clean(price_el.get_text()) if price_el else None

        if not title:
            return None
        return ProductResult(url=url, platform=self.platform, title=title, price=price)

    async def scrape(self, url: str, pool=None) -> Optional[ProductResult]:
        """scrape_hepsiburada'yı RetryEngine ile sar."""
        from scrapers.hepsiburada import scrape_hepsiburada
        return await self._wrap(url, pool, scrape_hepsiburada, label="hepsiburada")


class AmazonScraper(BaseScraper):
    """
    Amazon TR scraper.

    Katmanlar (scrape_amazon içinde):
      1. Android Chrome TLS curl_cffi (iOS Safari → 403 alıyor)
      2. Desktop Chrome curl_cffi
      3. Playwright ASIN arama motoru
    """

    platform = "amazon"

    async def parse(self, html: str, url: str) -> Optional[ProductResult]:
        """HTML → ProductResult. Mevcut amazon._parse kullanır."""
        try:
            from scrapers.amazon import _parse as _az_parse
            data = await asyncio.to_thread(_az_parse, html)
            return _to_product(data, url, self.platform)
        except Exception as e:
            log.debug(f"[amazon/parse] {e}")
            return None

    async def scrape(self, url: str, pool=None) -> Optional[ProductResult]:
        """scrape_amazon'u RetryEngine ile sar."""
        from scrapers.amazon import scrape_amazon
        return await self._wrap(url, pool, scrape_amazon, label="amazon")


class N11Scraper(BaseScraper):
    """
    N11 scraper.

    Katmanlar:
      1. GraphQL API (/nss/api/graphql) — en hızlı, bot dostu
      2. curl_cffi + window.model JSON parse
      3. Playwright (Cloudflare varsa)
    """

    platform = "n11"

    async def parse(self, html: str, url: str) -> Optional[ProductResult]:
        """HTML → ProductResult. Mevcut N11 parse kullanır."""
        try:
            from scrapers.n11 import _parse_html as _n11_parse
            data = await asyncio.to_thread(_n11_parse, html, url)
            return _to_product(data, url, self.platform)
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"[n11/parse] {e}")
        return None

    async def scrape(self, url: str, pool=None) -> Optional[ProductResult]:
        """
        N11 özel akışı:
          1. scrape_n11 (GQL birincil) — mevcut tam scraper
          2. Başarısız → BaseScraper LayeredFetcher fallback
        """
        from scrapers.n11 import scrape_n11

        domain = _DOMAIN_MAP["n11"]
        label  = f"n11:{url.split('/')[-1][:25]}"

        async def _attempt() -> Optional[ProductResult]:
            await _request_delay(self.cfg)
            async with get_limiter(self.cfg.concurrency).limit(domain):
                raw = await scrape_n11(url, pool=pool)

            if raw is None:
                return None
            if raw.get("dead_url"):
                raise TerminalError("dead_url")
            if raw.get("not_found"):
                raise TerminalError("not_found")
            return _to_product(raw, url, self.platform)

        result = await self._retry.run(_attempt, label=label)
        if result is not None:
            return result

        # GQL + tüm katmanlar başarısız → LayeredFetcher HTML fetch dene
        log.info("[n11] Tüm katmanlar başarısız → LayeredFetcher HTML fallback")
        return await super().scrape(url, pool=pool)


# ═══════════════════════════════════════════════════════════════
# FACTORY
# ═══════════════════════════════════════════════════════════════

_SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "trendyol":    TrendyolScraper,
    "hepsiburada": HepsiburadaScraper,
    "amazon":      AmazonScraper,
    "n11":         N11Scraper,
}


def get_scraper(platform: str, config: Optional[ScraperConfig] = None) -> BaseScraper:
    """
    Platform adından yapılandırılmış scraper instance'ı döndür.

    Args:
        platform: "trendyol" | "hepsiburada" | "amazon" | "n11"
        config:   None → varsayılan ScraperConfig kullanılır

    Raises:
        ValueError: Bilinmeyen platform adı

    Örnek:
        cfg     = ScraperConfig(max_retries=3, concurrency=2, backoff_base=3.0)
        scraper = get_scraper("amazon", cfg)
        result  = await scraper.scrape(url, pool=browser_pool)
        if result and result.is_valid:
            print(result.title, result.price)
    """
    cls = _SCRAPER_REGISTRY.get(platform.lower())
    if cls is None:
        supported = list(_SCRAPER_REGISTRY)
        raise ValueError(f"Bilinmeyen platform: {platform!r}. Desteklenenler: {supported}")
    return cls(config)
