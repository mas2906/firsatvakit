#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Platform tespiti ve URL yönlendirme."""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse
from db import commit_with_retry

# Bilinen kısa URL domainleri (mobil uygulamaların "Paylaş" butonu bunları üretir)
SHORT_URL_HOSTS = {"ty.gl", "app.hb.biz", "amzn.to", "amzn.eu", "a.co"}

# Redirect zincirinde asıl ürün URL'sini taşıyan parametreler (Adjust tracker vb.)
_EMBEDDED_URL_PARAMS = ("adjust_redirect", "adj_redirect", "redirect", "url", "u", "target")

_PRODUCT_HOSTS = ("amazon.com.tr", "trendyol.com", "hepsiburada.com", "n11.com", "karaca.com")

# /dp/X, /gp/product/X, /gp/aw/d/X (mobil), /-/tr/dp/X, /exec/obidos/ASIN/X
_AMAZON_ASIN_RE = r"/(?:dp|gp/product|gp/aw/d|o/ASIN|exec/obidos/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)"


def is_short_url(url: str) -> bool:
    """Kısa link (ty.gl, app.hb.biz vb.) mi?"""
    h = urlparse(url).netloc.lower()
    return any(h == s or h.endswith("." + s) for s in SHORT_URL_HOSTS)


def extract_url_from_text(text: str) -> Optional[str]:
    """Paylaşım metninden ("Bu ürüne bak! https://ty.gl/abc") ilk URL'yi çıkarır.
    Şemasız "www.trendyol.com/..." girişlerini de kabul eder."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"https?://[^\s<>\"']+", text, re.IGNORECASE)
    if m:
        url = m.group(0)
    else:
        m = re.search(r"(?:www\.|m\.)?(?:amazon\.com\.tr|trendyol\.com|hepsiburada\.com|n11\.com|karaca\.com|"
                      r"ty\.gl|app\.hb\.biz|amzn\.to|amzn\.eu|a\.co)/[^\s<>\"']*", text, re.IGNORECASE)
        if not m:
            return None
        url = "https://" + m.group(0)
    # Boşluksuz yapıştırılmış iki link ("...p-123https://www.trendyol.com/...")
    # tek URL sanılıp bozuk kaydediliyordu (ürün #29928) — ilkini al.
    nxt = re.search(r"https?://", url[8:], re.IGNORECASE)
    if nxt:
        url = url[:8 + nxt.start()]
    url = url.rstrip(".,;:!?)]}»\"'")
    # iOS klavyesi ilk harfi büyütebiliyor: "Https://" → "https://"
    scheme, rest = url.split("://", 1)
    return scheme.lower() + "://" + rest


def _embedded_product_url(url: str) -> Optional[str]:
    """Adjust/deeplink tracker URL'sinin query'sinde gömülü ürün URL'sini bulur."""
    from urllib.parse import parse_qs, unquote
    params = parse_qs(urlparse(url).query)
    for key in _EMBEDDED_URL_PARAMS:
        for val in params.get(key, []):
            val = unquote(val)
            if val.startswith("http") and any(h in urlparse(val).netloc.lower() for h in _PRODUCT_HOSTS):
                return val
    return None


def normalize_product_url(url: str, platform: str) -> str:
    """Mobil/alt domain varyantlarını masaüstü canonical forma çevirir
    (m.trendyol.com / stage.trendyol.com → www.trendyol.com, /gp/aw/d/ASIN → /dp/ASIN).
    Aynı ürünün farklı URL'lerle iki kez eklenmesini de önler."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if platform == "amazon":
        asin = extract_product_id(url, "amazon")
        return f"https://www.amazon.com.tr/dp/{asin}" if asin else url
    canonical_host = {"trendyol": "www.trendyol.com", "hepsiburada": "www.hepsiburada.com",
                      "n11": "www.n11.com", "karaca": "www.karaca.com"}.get(platform)
    # urun.n11.com gerçek bir ürün alt domain'i — dokunma
    if canonical_host and host != canonical_host and not host.startswith("urun."):
        return parsed._replace(scheme="https", netloc=canonical_host).geturl()
    return parsed._replace(scheme="https").geturl()


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
    """Kısa URL'yi redirect takip ederek gerçek URL'ye çevir.

    Redirect'ler elle, adım adım takip edilir: zincirde bir e-ticaret ürün
    URL'si (ya da onu parametre olarak taşıyan Adjust tracker'ı) görüldüğü an
    durulur. Trendyol/Hepsiburada VPS IP'sine 403/timeout döndüğü için hedef
    sayfanın kendisine hiç istek atılmaz.

    DİKKAT: VPS'te IPv6 çıkışı yok ama ty.gl / app.hb.biz AAAA kaydı da
    döndürüyor — httpx "No address associated with hostname" ile düşüyor ve
    kısa link hiç çözülmüyordu (telefondan eklenen linkler "Desteklenmeyen
    site" hatası alıyordu). local_address="0.0.0.0" IPv4'e zorlar."""
    import httpx
    from urllib.parse import parse_qs, urljoin, urlparse as _up, unquote
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9",
    }
    try:
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
        async with httpx.AsyncClient(follow_redirects=False, timeout=10, headers=headers,
                                     transport=transport) as client:
            current = url
            for _ in range(8):
                parsed = _up(current)
                host = parsed.netloc.lower()

                # ty.gl → adj.st?...&adjust_redirect=https://www.trendyol.com/...-p-123
                embedded = _embedded_product_url(current)
                if embedded:
                    return embedded

                # app.hb.biz → Adjust tracker'a yönlenirse SKU'dan Hepsiburada URL'si oluştur
                if "adj.st" in host or "adjust.com" in host:
                    params = parse_qs(parsed.query)
                    deep = (params.get("adj_deep_link") or params.get("adjust_deep_link")
                            or params.get("adjust_deeplink") or [None])[0]
                    sku = None
                    if deep:
                        deep_params = parse_qs(_up(unquote(deep)).query)
                        sku = (deep_params.get("sku") or [None])[0]
                    if not sku:
                        sku = (params.get("sku") or [None])[0]
                    if sku:
                        try:
                            canonical = await _hb_canonical_from_sku(sku, client)
                            if canonical:
                                return canonical
                        except Exception:
                            pass
                        # "/p/{sku}" 404 veriyor — çalışan format "/-p-{sku}"
                        # (app.hb.biz linkleri artık hepsiburada_link.py'de çözülüyor)
                        return f"https://www.hepsiburada.com/-p-{sku}"

                # Ürün sitesine ulaştık — hedef sayfayı çekmeye gerek yok
                if any(host == h or host.endswith("." + h) for h in _PRODUCT_HOSTS):
                    return current

                r = await client.get(current)
                loc = r.headers.get("location")
                if r.is_redirect and loc:
                    current = urljoin(current, loc)
                    continue
                return str(r.url)
            return current
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
    # karaca.com 2026-11-02'de platformdan çıkarıldı — yeni link kabul edilmez
    if "hepsiburada.com" in h:
        return "hepsiburada"
    return None


def extract_product_id(url: str, platform: str) -> str | None:
    """URL'den ürün ID / ASIN çıkar."""
    if platform == "amazon":
        m = re.search(_AMAZON_ASIN_RE, url)
        return m.group(1) if m else None
    if platform == "trendyol":
        # https://www.trendyol.com/marka/urun-p-12345678
        m = re.search(r"-p-(\d+)", url)
        return m.group(1) if m else None
    if platform == "n11":
        # https://www.n11.com/urun/urun-adi-123456789
        m = re.search(r"(\d{8,})", url)
        return m.group(1) if m else None
    if platform == "karaca":
        # https://www.karaca.com/urun/karaca-hatir-perfetto-espresso-...-inox → slug
        m = re.search(r"/urun/([a-z0-9-]+)", url.split("?")[0])
        return m.group(1) if m else None
    if platform == "hepsiburada":
        # https://www.hepsiburada.com/p/HBCV000048L6HL
        m = re.search(r"/p/([A-Z0-9]+)", url.split("?")[0])
        if m: return m.group(1)
        # https://www.hepsiburada.com/urun-adi-pm-HBC00001234567
        m = re.search(r"-(pm-[A-Z0-9]+)$", url.split("?")[0])
        if m: return m.group(1)
        # https://www.hepsiburada.com/urun-adi-p-HBV00000YBSJX (varyant SKU; HBV… da olabilir)
        m = re.search(r"-p-(HB[A-Z0-9]+)", url.split("?")[0])
        if m: return m.group(1)
        m = re.search(r"(HBC[A-Z0-9]+)", url)
        return m.group(1) if m else None
    return None


_INVALID_URL_PATTERNS = {
    "amazon": [
        r"/gp/bestsellers", r"/gp/buyagain", r"/gp/browse", r"/gp/cart",
        r"/gp/search", r"/s\?", r"/b\?", r"/b/", r"/stores/",
    ],
    "trendyol": [r"/sr\?", r"/butik/", r"/kampanya/"],
    "hepsiburada": [r"/ara\?", r"/liste/", r"/kampanya/"],
    "n11": [r"/liste/", r"/arama\?"],
}


def is_valid_product_url(url: str, platform: str) -> bool:
    """Ürün sayfası URL'si mi, yoksa kategori/liste sayfası mı?"""
    patterns = _INVALID_URL_PATTERNS.get(platform, [])
    for pat in patterns:
        if re.search(pat, url, re.IGNORECASE):
            return False
    # Platform bazlı ürün URL'si kontrolü
    if platform == "amazon" and not re.search(r"/dp/[A-Z0-9]{10}", url):
        return False
    if platform == "trendyol" and not re.search(r"-p-\d+", url):
        return False
    if platform == "karaca" and not re.search(r"/urun/[a-z0-9-]+", url):
        return False
    if platform == "hepsiburada" and not re.search(r"(/p/[A-Z0-9]+|HBC[A-Z0-9]+|-pm?-HB[A-Z0-9]+)", url):
        return False
    return True


def enqueue_url(db, url: str, platform: str) -> int:
    """Ürünü DB'ye kaydet ve tarama kuyruğuna ekle. product_id döner."""
    if not is_valid_product_url(url, platform):
        raise ValueError(f"Geçersiz ürün URL'si ({platform}): {url[:80]}")
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

    # Kuyruğa ekle — admin eklediği için en yüksek öncelik (-1)
    # Zaten kuyrukta varsa önceliğini -1'e çek (öne geç)
    existing = db.execute(
        "SELECT id, priority FROM scan_queue WHERE product_id=? AND status IN ('pending','processing')",
        (product_id,)
    ).fetchone()
    if existing:
        if existing["priority"] > -1:
            db.execute("UPDATE scan_queue SET priority=-1 WHERE id=?", (existing["id"],))
            commit_with_retry(db)
    else:
        db.execute("""
            INSERT INTO scan_queue(product_id, url, platform, status, priority, created_at)
            VALUES(?,?,?,'pending',-1,?)
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

    # Karaca: ürün slug'ı path'te, query sadece takip parametresi
    if platform == "karaca":
        return urlunparse(("https", "www.karaca.com", parsed.path.rstrip("/"), "", "", ""))

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
