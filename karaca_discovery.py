#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Karaca kategori keşfi — yeni ürünleri products tablosuna ekler (VPS, günde bir).

Kategoriler (2026-10-01, kullanıcı kararı):
  * /cok-satanlar        → tamamı (~378 ürün, 10 sayfa)
  * /indirimli-urunler   → ilk 10 sayfa (~400 ürün; kategorinin tamamı 4.600+)

Her liste sayfası schema.org ItemList ld+json'u taşıyor (40 ürün: ad, url, görsel,
fiyat, stok). Yeni ürün ilk fiyatıyla eklenir; sonraki fiyat takibi normal tarama
turunda Windows scraper'ı (scrapers/karaca.py) tarafından yapılır.

karaca.com VPS'ten erişilebiliyor (Cloudflare var ama bot engeli yok); yine de
curl_cffi ile tarayıcı TLS parmak izi kullanılır (bkz. memory: vps-network-gotchas).

Manuel çalıştırma:  venv/bin/python3 karaca_discovery.py [--force]
"""

import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import sync_status
from db import get_db
from scraper_router import clean_tracking_params, extract_product_id

log = logging.getLogger("scheduler")

BASE = "https://www.karaca.com"
CATEGORIES = [
    # (yol, en fazla kaç sayfa)
    ("/cok-satanlar", 50),        # tamamı — sayfa sayısı siteden okunur
    ("/indirimli-urunler", 10),
]
INTERVAL_HOURS = 24
_SETTINGS_KEY = "karaca_discovery_at"

_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_TOTAL_PAGES_RE = re.compile(r'"totalPages"\s*:\s*(\d+)')


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_listing(html: str) -> tuple[list[dict], int | None]:
    """Liste sayfasından (ürünler, toplam_sayfa)."""
    items = []
    for m in _LD_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if not (isinstance(d, dict) and d.get("@type") == "ItemList"):
            continue
        for el in d.get("itemListElement") or []:
            p = (el or {}).get("item") or {}
            offers = p.get("offers") or {}
            url = p.get("url") or offers.get("url")
            if not url or "/urun/" not in url:
                continue
            try:
                price = float(offers.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            img = p.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            items.append({
                "url": url, "title": (p.get("name") or "").strip(), "image_url": img,
                "price": price,
                "in_stock": "instock" in str(offers.get("availability") or "").lower(),
            })
    mt = _TOTAL_PAGES_RE.search(html.replace('\\"', '"'))
    return items, (int(mt.group(1)) if mt else None)


def _fetch(session, url: str) -> str | None:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            log.warning(f"[karaca-discovery] HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"[karaca-discovery] {url} hata: {type(e).__name__}: {e}")
        time.sleep(10 * (attempt + 1))
    return None


def discover() -> dict:
    """Kategorileri tarar, yeni ürünleri ekler. Senkron — scheduler'da thread'de çalışır."""
    sync_status.start("karaca")
    try:
        stats = _discover()
    except Exception as e:
        sync_status.fail("karaca", f"{type(e).__name__}: {e}")
        raise
    sync_status.finish("karaca", stats)
    return stats


def _discover() -> dict:
    from curl_cffi import requests as cffi

    session = cffi.Session(impersonate="chrome124")
    seen: dict[str, dict] = {}
    pages_read = 0
    for path, max_pages in CATEGORIES:
        page, total = 1, None
        while page <= max_pages and (total is None or page <= total):
            url = BASE + path + (f"?page={page}" if page > 1 else "")
            html = _fetch(session, url)
            if html is None:
                break
            items, tp = parse_listing(html)
            total = total or tp
            pages_read += 1
            sync_status.set_status("karaca", phase=f"sayfalar okunuyor ({path})", done=page,
                                   total=min(total or page, max_pages), products_found=len(seen))
            if not items:
                break
            for it in items:
                seen.setdefault(clean_tracking_params(it["url"], "karaca"), it)
            page += 1
            time.sleep(2 + random.random() * 2)

    db = get_db()
    now = _now()
    added = 0
    for clean_url, it in seen.items():
        slug = extract_product_id(clean_url, "karaca")
        if not slug or not it["title"]:
            continue
        exists = db.execute(
            "SELECT 1 FROM products WHERE source_url=? OR (platform='karaca' AND asin_or_id=?)",
            (clean_url, slug)
        ).fetchone()
        if exists:
            continue
        cur = db.execute(
            "INSERT INTO products(platform, asin_or_id, source_url, title, image_url, stock, first_seen_at, last_seen_at) "
            "VALUES('karaca',?,?,?,?,?,?,?)",
            (slug, clean_url, it["title"][:300], it["image_url"],
             "Stokta Var" if it["in_stock"] else "Stok Yok", now, now)
        )
        pid = cur.lastrowid
        if pid and it["price"] > 0:
            db.execute(
                "INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
                (pid, it["price"], "TRY", now)
            )
        added += 1
    db.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_SETTINGS_KEY, now, now)
    )
    db.commit()
    stats = {"pages": pages_read, "found": len(seen), "added": added}
    log.info(f"[karaca-discovery] {pages_read} sayfa, {len(seen)} ürün bulundu, {added} yeni ürün eklendi")
    return stats


def is_due() -> bool:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (_SETTINGS_KEY,)).fetchone()
    if not row or not row["value"]:
        return True
    last = datetime.strptime(str(row["value"])[:19], "%Y-%m-%d %H:%M:%S")
    return datetime.utcnow() - last >= timedelta(hours=INTERVAL_HOURS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if "--force" in sys.argv or is_due():
        print(discover())
    else:
        print("Henüz zamanı gelmedi (son çalışma 24 saatten yeni). Zorlamak için --force.")
