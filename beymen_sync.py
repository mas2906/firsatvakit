#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Beymen kategori eşitleme — keşif + fiyat takibi tek adımda (VPS, günde bir).

Kategoriler (2026-10-01, kullanıcı isteği: "sonuna kadar"):
  * Küçük Ev Aletleri & Beyaz Eşya  (~1.960 ürün, 41 sayfa)
  * Teknoloji                       (~10.760 ürün, 225 sayfa)

Diğer platformlardan farkı: Beymen liste sayfası (BEYMEN.productListMain JSON'u) her
ürünün güncel fiyatını, liste fiyatını, "Sepette" kampanya fiyatını ve stok durumunu
taşıyor (48 ürün/sayfa). Bu yüzden ürün sayfalarını tek tek taramaya gerek yok —
Windows scraper'ı Beymen'e HİÇ dokunmaz; scheduler.py Beymen ürünlerini scan_queue'ya
almaz. Fiyatlar her gün buradan /api/scraper-webhook'a gönderilir; 30 gün fırsat
kuralı, fiyat doğrulama ve bildirimler diğer platformlarla aynı yoldan işler.

Fiyat = güncel fiyat, ya da koşulsuz "Sepette" kampanyası varsa onun fiyatı
(cart_discount=True). "Visa ile %5" gibi karta bağlı indirimler SAYILMAZ.

Manuel çalıştırma:  venv/bin/python3 beymen_sync.py [--force] [--pages N]
"""

import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import sync_status
from db import get_db

load_dotenv()
log = logging.getLogger("scheduler")
logging.getLogger("httpx").setLevel(logging.WARNING)  # ~12k webhook × 1 satır log olmasın

BASE = "https://www.beymen.com"
CATEGORIES = [
    "/tr/yasam-stil-onerisi-kucuk-ev-aletleri--beyaz-esya-110726",
    "/tr/teknoloji-95935",
]
MAX_PAGES = 400                      # güvenlik sınırı; asıl sınır sitenin totalPage'i
INTERVAL_HOURS = 24
IMAGE_SIZE = "600/800"
WEBHOOK_URL = "http://127.0.0.1:8000/api/scraper-webhook"
WEBHOOK_DELAY = 0.1                  # web uygulamasını boğmamak için (12k ürün ≈ 20+ dk)
_SETTINGS_KEY = "beymen_sync_at"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _extract_list_json(html: str) -> dict | None:
    """`BEYMEN.productListMain = {...};` nesnesini parantez sayarak çıkarır."""
    i = html.find("BEYMEN.productListMain")
    if i < 0:
        return None
    start = html.find("{", i)
    depth, in_str, esc = 0, False, False
    for n in range(start, len(html)):
        ch = html[n]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:n + 1])
                except Exception:
                    return None
    return None


def to_product_data(p: dict) -> dict | None:
    """Liste ürününü webhook `data` formatına çevirir."""
    try:
        actual = float(p.get("actualPrice") or 0)
    except (TypeError, ValueError):
        return None
    if actual <= 0 or not p.get("productUrl"):
        return None
    price, cart = actual, False
    for pr in p.get("productPromotions") or []:
        # Sadece koşulsuz sepet indirimi; "Visa ile" vb. karta bağlı olanlar hariç
        if (pr.get("campaignDesc") or "").strip().lower() != "sepette" or not pr.get("showPrice"):
            continue
        try:
            pp = float(pr.get("promotedPrice") or 0)
        except (TypeError, ValueError):
            continue
        if 0 < pp < price:
            price, cart = pp, True
    try:
        original = float(p.get("originalPrice") or 0)
    except (TypeError, ValueError):
        original = 0
    brand = (p.get("brandName") or "").strip()
    name = (p.get("displayName") or "").strip()
    title = name if (not brand or name.lower().startswith(brand.lower())) else f"{brand} {name}"
    images = p.get("images") or []
    image = images[0].replace("{width}/{height}", IMAGE_SIZE) if images else None
    # Sadece açık bayrak: 3 gün üst üste "Stok Yok" ürünü sildirir (api._handle_out_of_stock),
    # productStatus'un bilinmeyen bir değeri yüzünden canlı ürün silinmesin.
    out_of_stock = p.get("isOutOfStock") is True
    return {
        "product_key": str(p["productId"]),
        "url": BASE + p["productUrl"].split("?")[0],
        "title": title[:300] or None,
        "price": round(price, 2),
        "was_price": original if original > price * 1.01 else None,
        "image_url": image,
        "brand": brand or None,
        "stock": "Stok Yok" if out_of_stock else "Stokta Var",
        "cart_discount": cart,
        "coupon": None,
    }


def crawl(session, max_pages: int = MAX_PAGES) -> dict[str, dict]:
    found: dict[str, dict] = {}
    pages_done = 0
    for ci, path in enumerate(CATEGORIES, 1):
        page, total = 1, None
        while page <= max_pages and (total is None or page <= total):
            url = BASE + path + (f"?sayfa={page}" if page > 1 else "")
            data = None
            for attempt in range(3):
                try:
                    r = session.get(url, timeout=40)
                    if r.status_code == 200:
                        data = _extract_list_json(r.text)
                        if data:
                            break
                    log.warning(f"[beymen] HTTP {r.status_code} / veri yok: {url}")
                except Exception as e:
                    log.warning(f"[beymen] {url} hata: {type(e).__name__}: {e}")
                time.sleep(15 * (attempt + 1))
            if not data:
                log.warning(f"[beymen] {path} sayfa {page} alınamadı — kategori yarıda bırakıldı")
                break
            total = total or data.get("totalPage")
            prods = data.get("products") or []
            if not prods:
                break
            for p in prods:
                d = to_product_data(p)
                if d:
                    found.setdefault(d["product_key"], d)
            pages_done += 1
            sync_status.set_status("beymen", phase=f"sayfalar okunuyor (kategori {ci}/{len(CATEGORIES)})",
                                   done=page, total=min(total or page, max_pages),
                                   pages_read=pages_done, products_found=len(found))
            page += 1
            time.sleep(1.5 + random.random() * 1.5)
        log.info(f"[beymen] {path}: {page - 1}/{total} sayfa okundu, toplam benzersiz ürün={len(found)}")
    return found


def sync(max_pages: int = MAX_PAGES) -> dict:
    """Senkron — scheduler'da thread'de çalışır."""
    import httpx
    from curl_cffi import requests as cffi

    started = time.time()
    # Zamanı BAŞTA yaz: eşitleme ~40 dk sürüyor; sonda yazılırsa bu sırada başka bir
    # tetikleyici (elle çalıştırma / scheduler yeniden başlatma) is_due()=True görüp
    # ikinci bir kopya başlatıyordu.
    _mark_run(_now())
    sync_status.start("beymen")
    try:
        return _sync(cffi, httpx, started, max_pages)
    except Exception as e:
        sync_status.fail("beymen", f"{type(e).__name__}: {e}")
        raise


def _sync(cffi, httpx, started: float, max_pages: int) -> dict:
    found = crawl(cffi.Session(impersonate="chrome124"), max_pages)
    sync_status.set_status("beymen", phase="yeni ürünler ekleniyor", done=0, total=len(found))

    db = get_db()
    now = _now()
    ids: dict[str, int] = {}
    added = 0
    for key, d in found.items():
        row = db.execute(
            "SELECT id FROM products WHERE platform='beymen' AND asin_or_id=?", (key,)
        ).fetchone()
        if row:
            ids[key] = row["id"]
            continue
        cur = db.execute(
            "INSERT INTO products(platform, asin_or_id, source_url, title, image_url, brand, stock, first_seen_at, last_seen_at) "
            "VALUES('beymen',?,?,?,?,?,?,?,?)",
            (key, d["url"], d["title"], d["image_url"], d["brand"], d["stock"], now, now)
        )
        ids[key] = cur.lastrowid
        added += 1
    db.commit()
    sync_status.set_status("beymen", phase="fiyatlar işleniyor", done=0, total=len(found), added=added)

    # Fiyatları normal webhook yolundan işle (price_history, 30 gün kuralı, fırsat, bildirim)
    sent = failed = 0
    headers = {"X-Webhook-Key": os.getenv("FRONTEND_WEBHOOK_KEY", "")}
    with httpx.Client(timeout=30) as client:
        for key, d in found.items():
            pid = ids.get(key)
            if not pid:
                continue
            payload = {"product_id": pid, "url": d["url"], "platform": "beymen",
                       "data": {k: v for k, v in d.items() if k not in ("product_key", "url")}}
            try:
                r = client.post(WEBHOOK_URL, json=payload, headers=headers)
                sent += r.status_code == 200
                failed += r.status_code != 200
            except Exception:
                failed += 1
            if (sent + failed) % 200 == 0:
                sync_status.set_status("beymen", done=sent + failed, failed=failed)
            time.sleep(WEBHOOK_DELAY)

    stats = {"products": len(found), "added": added, "webhooks": sent, "failed": failed,
             "minutes": round((time.time() - started) / 60, 1)}
    log.info(f"[beymen] eşitleme bitti: {stats}")
    sync_status.finish("beymen", stats)
    return stats


def _mark_run(ts: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_SETTINGS_KEY, ts, ts)
    )
    db.commit()


def is_due() -> bool:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (_SETTINGS_KEY,)).fetchone()
    if not row or not row["value"]:
        return True
    last = datetime.strptime(str(row["value"])[:19], "%Y-%m-%d %H:%M:%S")
    return datetime.utcnow() - last >= timedelta(hours=INTERVAL_HOURS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pages = MAX_PAGES
    if "--pages" in sys.argv:
        pages = int(sys.argv[sys.argv.index("--pages") + 1])
    if "--force" in sys.argv or is_due():
        print(sync(pages))
    else:
        print("Henüz zamanı gelmedi (son çalışma 24 saatten yeni). Zorlamak için --force.")
