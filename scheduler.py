#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arka plan zamanlayıcı.
Çalıştırma: python scheduler.py
(main.py ile ayrı process olarak çalıştırın)
"""

import asyncio, time
from datetime import datetime

from db import get_db, init_db
from scraper_router import detect_platform
from scrapers.router import scrape_product
from affiliate import make_short_slug
from telegram_pub import publish_deal
from email_utils import send_price_alert

SCAN_INTERVAL_SEC = 10  # Bir tur bitince 10s bekle, sonra tekrar başla


def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def run_all_pending():
    """Kuyrukta bekleyen tüm URL'leri tara."""
    db = get_db()
    pending = db.execute("""
        SELECT sq.*, p.source_url, p.platform, p.id as pid
        FROM scan_queue sq
        JOIN products p ON sq.product_id = p.id
        WHERE sq.status IN ('pending','failed')
        ORDER BY sq.priority ASC, sq.created_at ASC
        LIMIT 50
    """).fetchall()

    print(f"[scheduler] {len(pending)} URL taranacak")
    for row in pending:
        await _scan_one(db, row)
        await asyncio.sleep(2)  # İstekler arası bekleme


async def run_active_products():
    """Aktif ürünleri periyodik tara — watchlist ürünleri önce."""
    db = get_db()
    products = db.execute("""
        SELECT p.*,
               CASE WHEN EXISTS(SELECT 1 FROM product_watchlist pw WHERE pw.product_id=p.id) THEN 0 ELSE 1 END as wl_priority
        FROM products p
        ORDER BY wl_priority ASC, last_seen_at ASC
        LIMIT 100
    """).fetchall()

    watchlist_count = sum(1 for p in products if p["wl_priority"] == 0)
    print(f"[scheduler] {len(products)} ürün taranıyor ({watchlist_count} watchlist öncelikli)")
    for p in products:
        await _scan_product(db, p)
        await asyncio.sleep(1.5)


async def _scan_one(db, queue_row):
    """Tek kuyruk kaydını tara."""
    url      = queue_row["url"]
    platform = queue_row["platform"]
    pid      = queue_row["pid"]

    db.execute("UPDATE scan_queue SET status='running', updated_at=? WHERE id=?",
               (now_str(), queue_row["id"]))
    db.commit()

    try:
        data = await asyncio.wait_for(scrape_product(url, platform), timeout=75)
        if data:
            await _process_scraped(db, pid, platform, data)
            db.execute("UPDATE scan_queue SET status='done', updated_at=? WHERE id=?",
                       (now_str(), queue_row["id"]))
        else:
            db.execute("UPDATE scan_queue SET status='failed', updated_at=? WHERE id=?",
                       (now_str(), queue_row["id"]))
        db.commit()
    except asyncio.TimeoutError:
        print(f"[scheduler] Timeout pid={pid} ({platform}) — 75s aşıldı")
        db.execute("UPDATE scan_queue SET status='failed', updated_at=? WHERE id=?",
                   (now_str(), queue_row["id"]))
        db.commit()
    except Exception as e:
        print(f"[scheduler] Hata pid={pid}: {e}")
        db.execute("UPDATE scan_queue SET status='failed', updated_at=? WHERE id=?",
                   (now_str(), queue_row["id"]))
        db.commit()


async def _scan_product(db, product):
    """Mevcut ürünü tara ve fiyat değişikliği varsa deal oluştur."""
    try:
        data = await asyncio.wait_for(
            scrape_product(product["source_url"], product["platform"]), timeout=75
        )
        if data:
            await _process_scraped(db, product["id"], product["platform"], data)
    except asyncio.TimeoutError:
        print(f"[scheduler] Timeout id={product['id']} ({product['platform']}) — 75s aşıldı")
    except Exception as e:
        print(f"[scheduler] Ürün tarama hatası id={product['id']}: {e}")


async def _process_scraped(db, product_id: int, platform: str, data: dict):
    """Scrape sonucunu işle, gerekirse deal oluştur."""
    now = now_str()

    # Ürünü güncelle
    db.execute("""
        UPDATE products SET
            title        = COALESCE(?, title),
            image_url    = COALESCE(?, image_url),
            rating       = COALESCE(?, rating),
            review_count = COALESCE(?, review_count),
            last_seen_at = ?
        WHERE id=?
    """, (data.get("title"), data.get("image_url"),
          data.get("rating"), data.get("review_count"),
          now, product_id))

    price = data.get("price")
    if not price or price <= 0:
        db.commit()
        return

    # Önceki fiyatı kontrol et — aynıysa kaydetme
    prev = db.execute("""
        SELECT price_value FROM price_history
        WHERE product_id=?
        ORDER BY id DESC LIMIT 1
    """, (product_id,)).fetchone()

    if prev and prev["price_value"] == price:
        # Fiyat değişmedi, kaydetme
        db.commit()
        return

    # Fiyat geçmişine ekle (yeni veya değişmiş fiyat)
    db.execute(
        "INSERT INTO price_history(product_id, price_value, currency, scraped_at) VALUES(?,?,?,?)",
        (product_id, price, "TRY", now)
    )

    if prev and prev["price_value"] and price < prev["price_value"]:
        old_price = prev["price_value"]
        pct = (old_price - price) / old_price * 100

        if pct >= 5:
            # Mevcut pending/aktif deal varsa güncelle, yoksa yeni oluştur
            existing_deal = db.execute("""
                SELECT d.id, d.status, sl.slug FROM deals d
                LEFT JOIN short_links sl ON sl.deal_id = d.id
                WHERE d.product_id=? AND d.status IN ('pending','approved')
                ORDER BY d.id DESC LIMIT 1
            """, (product_id,)).fetchone()

            if existing_deal:
                deal_id = existing_deal["id"]
                slug = existing_deal["slug"] or make_short_slug()
                db.execute("""
                    UPDATE deals SET old_price=?, new_price=?, discount_pct=?, created_at=?
                    WHERE id=?
                """, (old_price, price, round(pct, 1), now, deal_id))
                if not existing_deal["slug"]:
                    db.execute(
                        "INSERT INTO short_links(deal_id, slug, created_at) VALUES(?,?,?)",
                        (deal_id, slug, now)
                    )
            else:
                # Yeni deal — pending olarak oluştur, admin onayı bekle
                slug = make_short_slug()
                cur = db.execute("""
                    INSERT INTO deals(product_id, old_price, new_price, discount_pct,
                                      active, status, created_at)
                    VALUES(?,?,?,?,0,'pending',?)
                """, (product_id, old_price, price, round(pct, 1), now))
                deal_id = cur.lastrowid
                db.execute(
                    "INSERT INTO short_links(deal_id, slug, created_at) VALUES(?,?,?)",
                    (deal_id, slug, now)
                )

            db.commit()
            print(f"[scheduler] 📋 Deal #{deal_id} onay bekliyor: {product_id} %{pct:.1f}")
            # NOT: Telegram yayını artık admin onayından sonra yapılır

            # Watchlist kullanıcılarına e-posta bildirimi gönder
            await _notify_watchers(db, product_id, old_price, price, pct, deal_id)

    # ── Fiyat yükseldiyse aktif deal'ları kapat ──────────────
    # Güncel fiyat, deal'ın indirimli fiyatından yüksekse → fırsat geçmiş demektir
    active_deals = db.execute("""
        SELECT id, new_price, discount_pct FROM deals
        WHERE product_id=? AND active=1
    """, (product_id,)).fetchall()

    for deal in active_deals:
        if price > deal["new_price"] * 1.02:  # %2 tolerans (küçük dalgalanma görmezden gel)
            db.execute("""
                UPDATE deals SET active=0, expires_at=? WHERE id=?
            """, (now, deal["id"]))
            print(f"[scheduler] ⏹ Deal #{deal['id']} pasife alındı — fiyat yükseldi: {deal['new_price']} → {price}")

    db.commit()


async def _notify_watchers(db, product_id: int, old_price: float,
                           new_price: float, pct: float, deal_id: int):
    """Bu ürünü takip eden tüm kullanıcılara e-posta bildirimi gönder."""
    watchers = db.execute("""
        SELECT u.email, u.username
        FROM product_watchlist pw
        JOIN users u ON pw.user_id = u.id
        WHERE pw.product_id = ?
    """, (product_id,)).fetchall()

    if not watchers:
        return

    product = db.execute("SELECT title FROM products WHERE id=?", (product_id,)).fetchone()
    title = (product["title"] or f"Ürün #{product_id}") if product else f"Ürün #{product_id}"

    import os
    site_url = os.getenv("SITE_URL", "https://firsatvakti.com")
    deal_url = f"{site_url}/deal/{deal_id}"

    print(f"[scheduler] 📧 {len(watchers)} takipçiye bildirim gönderiliyor (ürün #{product_id})")
    for w in watchers:
        try:
            send_price_alert(
                to=w["email"],
                username=w["username"],
                product_title=title,
                old_price=old_price,
                new_price=new_price,
                pct=pct,
                deal_url=deal_url,
            )
        except Exception as e:
            print(f"[scheduler] ⚠ Bildirim hatası ({w['email']}): {e}")


async def main_loop():
    """Sonsuz döngü."""
    init_db()
    print(f"[scheduler] Başladı. Interval={SCAN_INTERVAL_SEC}s")
    while True:
        try:
            await run_all_pending()
            await run_active_products()
        except Exception as e:
            print(f"[scheduler] Döngü hatası: {e}")
        print(f"[scheduler] Bekleniyor {SCAN_INTERVAL_SEC}s...")
        await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main_loop())
