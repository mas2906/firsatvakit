#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hepsiburada kısa link (app.hb.biz) çözücü ve filtresi.

Hepsiburada mobil uygulamasının "Paylaş" butonu https://app.hb.biz/XXXXXXXXXXXX
şeklinde kısa link üretir. Bu link bir Adjust takip adresine (xxxx.adj.st)
yönlenir ve adresin PATH'i linkin türünü belirler:

    /product?sku=HBCV000086WACD&merchantName=bkmkitap   → ürün  (kabul)
    /myList?url=https://listelerim.hepsiburada.com/...  → liste (red)
    /tag?tagId=...                                      → kampanya/etiket (red)
    https://www.hepsiburada.com  (doğrudan ana sayfa)   → süresi dolmuş link (red)

NEDEN curl_cffi: app.hb.biz ve hepsiburada.com Akamai bot koruması arkasında;
httpx/requests/curl'ün TLS parmak izini tanıyıp 403 ("Hepsiburada | Güvenlik")
döndürüyor — hem VPS'ten hem ev IP'sinden. curl_cffi gerçek bir Safari/Chrome
TLS parmak izi taklit ettiği için geçiyor (2026-09-29'da doğrulandı).

DİKKAT: "https://www.hepsiburada.com/p/{SKU}" formatı 404 verir. Geçerli
formatlar: "/-p-{SKU}" (slug'sız) ve "/{slug}-p-{SKU}" (varyant) ya da
"/{slug}-pm-{HBC…}" (ürün ana kaydı).
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

HB_SHORT_HOSTS = ("app.hb.biz",)
_IMPERSONATE = "safari17_2_ios"
_TIMEOUT = 15

# SKU: HBCV000086WACD, HBV00000YBSJX, HBC00006F3MDG …
_SKU_RE = re.compile(r"^HB[A-Z0-9]{8,}$")
_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.IGNORECASE)


@dataclass
class HBResult:
    url: Optional[str] = None      # Çözülen ürün URL'si (başarılıysa)
    error: Optional[str] = None    # Kullanıcıya gösterilecek hata mesajı (başarısızsa)

    @property
    def ok(self) -> bool:
        return bool(self.url)


def is_hb_short_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in HB_SHORT_HOSTS)


def _product_url_from_sku(sku: str, merchant: Optional[str] = None, slug: Optional[str] = None) -> str:
    path = f"/{slug}-p-{sku}" if slug else f"/-p-{sku}"
    url = "https://www.hepsiburada.com" + path
    return url + "?" + urlencode({"magaza": merchant}) if merchant else url


def _classify_adjust(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Adjust tracker URL'sini sınıflandırır → (sku, merchant, hata_mesajı)."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").lower()
    params = parse_qs(parsed.query)

    sku = (params.get("sku") or [None])[0]
    if not sku:
        # Bazı linklerde SKU sadece deeplink'in içinde: hbapp://product?sku=…
        deep = (params.get("adj_deep_link") or params.get("adjust_deep_link") or [None])[0]
        if deep:
            sku = (parse_qs(urlparse(unquote(deep)).query).get("sku") or [None])[0]
    merchant = (params.get("merchantName") or params.get("merchantname") or [None])[0]

    if sku and _SKU_RE.match(sku.upper()):
        return sku.upper(), merchant, None
    if path == "mylist":
        return None, None, "Bu bir Hepsiburada liste linki, tek bir ürün değil. Listeden ürünü açıp o ürünün linkini paylaş."
    if path == "tag":
        return None, None, "Bu bir Hepsiburada kampanya/etiket linki, tek bir ürün değil. Ürün sayfasını açıp linkini paylaş."
    return None, None, "Bu Hepsiburada linki bir ürün sayfasına gitmiyor. Ürün sayfasını açıp linkini paylaş."


def _is_hb_product_url(url: str) -> bool:
    return bool(re.search(r"-pm?-HB[A-Z0-9]+", urlparse(url).path))


async def _slug_for_sku(session, sku: str) -> Optional[str]:
    """/-p-{SKU} sayfasının canonical'ından ürün slug'ını alır
    (".../huzurun-hantalligi-isil-dirican-pm-HBC000086WACC" → "huzurun-hantalligi-isil-dirican").
    Slug'lı URL scraper ve dedup için daha sağlıklı; alınamazsa None."""
    try:
        r = await session.get(_product_url_from_sku(sku), allow_redirects=True)
        if r.status_code != 200:
            return None
        m = _CANONICAL_RE.search(r.text)
        if not m:
            return None
        cm = re.search(r"/([a-z0-9-]+)-pm?-HB[A-Z0-9]+", urlparse(m.group(1)).path)
        return cm.group(1) if cm else None
    except Exception:
        return None


async def resolve_hb_short_url(url: str) -> HBResult:
    """app.hb.biz kısa linkini Hepsiburada ürün URL'sine çevirir ya da
    neden kabul edilmediğini açıklayan bir hata döndürür."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return HBResult(error="Hepsiburada kısa linkleri şu an çözülemiyor. Lütfen ürünün tam linkini (hepsiburada.com/...) yapıştır.")

    try:
        async with AsyncSession(impersonate=_IMPERSONATE, timeout=_TIMEOUT) as session:
            current = url
            for _ in range(6):
                host = urlparse(current).netloc.lower()

                if "adj.st" in host or "adjust.com" in host:
                    sku, merchant, err = _classify_adjust(current)
                    if err:
                        return HBResult(error=err)
                    slug = await _slug_for_sku(session, sku)
                    return HBResult(url=_product_url_from_sku(sku, merchant, slug))

                if host.endswith("hepsiburada.com"):
                    if _is_hb_product_url(current):
                        return HBResult(url=current)
                    # Süresi dolmuş linkler doğrudan ana sayfaya düşüyor
                    return HBResult(error="Bu Hepsiburada linkinin süresi dolmuş ya da ürün kaldırılmış. Ürünü uygulamada açıp yeniden paylaş.")

                r = await session.get(current, allow_redirects=False)
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    current = urljoin(current, loc)
                    continue
                if r.status_code == 403:
                    return HBResult(error="Hepsiburada linki şu an doğrulanamadı. Lütfen birazdan tekrar dene ya da ürünün tam linkini yapıştır.")
                break
    except Exception as e:
        print(f"[hepsiburada_link] {url} çözülemedi: {type(e).__name__}: {e}")
        return HBResult(error="Hepsiburada linki çözülemedi (zaman aşımı). Lütfen tekrar dene ya da ürünün tam linkini yapıştır.")

    return HBResult(error="Bu Hepsiburada linki bir ürün sayfasına gitmiyor. Ürün sayfasını açıp linkini paylaş.")


def normalize_hb_url(url: str) -> str:
    """Eski/bozuk "/p/{SKU}" formatını (404 veriyor) çalışan "/-p-{SKU}" formatına çevirir."""
    parsed = urlparse(url)
    m = re.fullmatch(r"/p/(HB[A-Z0-9]+)/?", parsed.path)
    if m:
        return parsed._replace(path=f"/-p-{m.group(1)}").geturl()
    return url


if __name__ == "__main__":
    # Manuel test: python hepsiburada_link.py https://app.hb.biz/XXXX [...]
    import asyncio
    import sys
    for arg in sys.argv[1:]:
        res = asyncio.run(resolve_hb_short_url(arg))
        print(arg, "->", res.url or f"HATA: {res.error}")
