#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arka plan zamanlayıcı — VPS tarafı.
Sadece deal expire + DB temizliği yapar.
Scraping doğrudan scraper_worker.py tarafından yapılır (scan_queue yok).
"""

import asyncio
import logging
from datetime import datetime

from db import get_db, init_db
from telegram_pub import send_admin_alert
from config import AUTO_EXPIRE_HOURS

log = logging.getLogger("scheduler")


def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def auto_expire_stale_deals():
    """AUTO_EXPIRE_HOURS süredir kontrol edilmemiş ürünlerin deal'larını pasife al."""
    db = get_db()
    now = now_str()
    expired = db.execute(f"""
        SELECT d.id FROM deals d
        JOIN products p ON d.product_id = p.id
        WHERE d.active = 1
          AND (
            p.last_seen_at IS NULL
            OR p.last_seen_at < datetime('now', '-{AUTO_EXPIRE_HOURS} hours')
          )
    """).fetchall()
    for row in expired:
        db.execute("UPDATE deals SET active=0, expires_at=? WHERE id=?", (now, row["id"]))
        log.info(f"Deal #{row['id']} pasife alındı — {AUTO_EXPIRE_HOURS}s kontrol edilmedi")
    if expired:
        db.commit()


async def _expire_worker():
    """Stale deal temizliği — saatte bir çalışır."""
    while True:
        await asyncio.sleep(3600)
        try:
            await auto_expire_stale_deals()
        except Exception as e:
            log.error(f"[expire] Hata: {e}", exc_info=True)


async def _cleanup_worker():
    """scraper_errors temizliği — startup'ta hemen, sonra 6 saatte bir."""
    while True:
        try:
            db = get_db()
            cur = db.execute(
                "DELETE FROM scraper_errors WHERE occurred_at < datetime('now', '-24 hours')"
            )
            deleted = cur.rowcount
            db.commit()
            if deleted:
                log.info(f"[cleanup] scraper_errors={deleted} silindi")
        except Exception as e:
            log.error(f"[cleanup] Hata: {e}", exc_info=True)
        await asyncio.sleep(21600)  # 6 saatte bir


async def _requeue_worker():
    """Sürekli döngü — 3 katmanlı öncelik sistemi."""
    log.info("[requeue] başladı")
    while True:
        try:
            db = get_db()
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # 1. 10+ dakikadır processing'te takılı olanları pending'e al
            stale = db.execute("""
                UPDATE scan_queue SET status='pending', updated_at=?
                WHERE status='processing'
                  AND updated_at < datetime('now', '-10 minutes')
            """, (now,)).rowcount
            if stale:
                log.info(f"[requeue] {stale} takılı iş pending'e döndürüldü")

            # 2. Yeni eklenen linkler — hiç taranmamış (priority=0)
            new_rows = db.execute("""
                SELECT p.id, p.source_url, p.platform
                FROM products p
                WHERE p.platform IS NOT NULL
                  AND p.source_url IS NOT NULL
                  AND p.last_seen_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM scan_queue sq
                      WHERE sq.product_id = p.id
                        AND sq.status IN ('pending', 'processing')
                  )
            """).fetchall()
            for r in new_rows:
                db.execute(
                    "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) "
                    "VALUES(?,?,?,'pending',0,?)",
                    (r["id"], r["source_url"], r["platform"], now)
                )
            if new_rows:
                db.commit()
                log.info(f"[requeue] {len(new_rows)} yeni link kuyruğa eklendi (priority=0)")

            # 3. Aktif deal ürünleri — 5 dakikada bir (priority=1)
            # last_seen_at IS NULL → hiç taranmamış ama aktif deal var, hemen ekle
            deal_rows = db.execute("""
                SELECT DISTINCT p.id, p.source_url, p.platform
                FROM products p
                JOIN deals d ON d.product_id = p.id
                WHERE d.active = 1
                  AND p.platform IS NOT NULL
                  AND p.source_url IS NOT NULL
                  AND (p.last_seen_at IS NULL OR p.last_seen_at < datetime('now', '-5 minutes'))
                  AND NOT EXISTS (
                      SELECT 1 FROM scan_queue sq
                      WHERE sq.product_id = p.id
                        AND sq.status IN ('pending', 'processing')
                        AND sq.priority <= 1
                  )
            """).fetchall()
            for r in deal_rows:
                db.execute(
                    "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) "
                    "VALUES(?,?,?,'pending',1,?)",
                    (r["id"], r["source_url"], r["platform"], now)
                )
            if deal_rows:
                db.commit()
                log.info(f"[requeue] {len(deal_rows)} aktif deal kuyruğa eklendi (priority=1)")

            # 3b. Aktif deal ürünlerinin priority=2 bekleyen girişlerini priority=1'e yükselt
            promoted = db.execute("""
                UPDATE scan_queue SET priority=1, updated_at=?
                WHERE status='pending' AND priority > 1
                  AND product_id IN (
                      SELECT DISTINCT p.id FROM products p
                      JOIN deals d ON d.product_id = p.id
                      WHERE d.active = 1
                  )
            """, (now,)).rowcount
            if promoted:
                db.commit()
                log.info(f"[requeue] {promoted} aktif deal girişi priority=1'e yükseltildi")

            # 4. Tüm ürünler — round tabanlı sıralı tarama (priority=2)
            # round_started_at: settings tablosunda tutulur
            # Bu turda taranmamış ürünler (last_seen_at < round_started_at) kuyruğa alınır
            round_row = db.execute(
                "SELECT value FROM settings WHERE key='scan_round_started_at'"
            ).fetchone()
            if not round_row:
                db.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    ("scan_round_started_at", now, now)
                )
                db.commit()
                round_started_at = now
            else:
                round_started_at = round_row["value"]

            total_products = db.execute(
                "SELECT COUNT(*) FROM products WHERE platform IS NOT NULL AND source_url IS NOT NULL"
            ).fetchone()[0]
            scanned_this_round = db.execute(
                "SELECT COUNT(*) FROM products p "
                "WHERE p.platform IS NOT NULL AND p.source_url IS NOT NULL "
                "AND p.last_seen_at IS NOT NULL AND p.last_seen_at >= ?",
                (round_started_at,)
            ).fetchone()[0]

            all_rows = db.execute("""
                SELECT p.id, p.source_url, p.platform
                FROM products p
                WHERE p.platform IS NOT NULL
                  AND p.source_url IS NOT NULL
                  AND (p.last_seen_at IS NULL OR p.last_seen_at < ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM scan_queue sq
                      WHERE sq.product_id = p.id
                        AND sq.status IN ('pending', 'processing')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM deals d
                      WHERE d.product_id = p.id AND d.active = 1
                  )
                ORDER BY COALESCE(p.last_seen_at, '2000-01-01') ASC
                LIMIT 2000
            """, (round_started_at,)).fetchall()
            for r in all_rows:
                db.execute(
                    "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) "
                    "VALUES(?,?,?,'pending',2,?)",
                    (r["id"], r["source_url"], r["platform"], now)
                )
            if all_rows:
                db.commit()
            log.info(f"[requeue] Tur: {scanned_this_round}/{total_products} tamamlandı | kuyruğa eklenen: {len(all_rows)}")

            # 5. Zaman bazlı temizlik — 48 saatlik done/failed kayıtları sil
            cleaned = db.execute(
                "DELETE FROM scan_queue WHERE status IN ('done', 'failed')"
                " AND updated_at < datetime('now', '-48 hours')"
            ).rowcount
            if cleaned:
                db.commit()
                log.info(f"[requeue] {cleaned} eski done/failed kayıt temizlendi")

            # 6. Tur tamamlandı mı? — tüm ürünler bu turda tarandıysa yeni tur başlat
            active = db.execute(
                "SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
            remaining = db.execute(
                "SELECT COUNT(*) FROM products p "
                "WHERE p.platform IS NOT NULL AND p.source_url IS NOT NULL "
                "AND (p.last_seen_at IS NULL OR p.last_seen_at < ?) "
                "AND NOT EXISTS (SELECT 1 FROM scan_queue sq WHERE sq.product_id=p.id AND sq.status IN ('pending','processing'))",
                (round_started_at,)
            ).fetchone()[0]
            if active == 0 and remaining == 0 and scanned_this_round > 0:
                new_round = now
                db.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    ("scan_round_started_at", new_round, new_round)
                )
                db.commit()
                log.info(f"[requeue] ✅ Tur tamamlandı! {scanned_this_round}/{total_products} ürün tarandı. Yeni tur başlıyor.")

            # PostgreSQL'de açık transaction'ı kapat — CREATE INDEX gibi DDL'lerin
            # lock beklemesini önlemek için her döngü sonunda commit zorunlu.
            db.commit()

            await asyncio.sleep(30)

        except Exception as e:
            log.error(f"[requeue] Hata: {e}", exc_info=True)
            await asyncio.sleep(30)


async def main_loop():
    """Deal expire + DB temizliği. Scraping scraper_worker.py tarafından yapılır."""
    init_db()
    log.info("Scheduler başladı — deal expire + DB temizliği (scraping=scraper_worker.py)")
    try:
        await asyncio.gather(
            _expire_worker(),
            _cleanup_worker(),
            _requeue_worker(),
        )
    except Exception as e:
        log.critical(f"Scheduler beklenmedik hatayla durdu: {e}", exc_info=True)
        try:
            await send_admin_alert(
                f"🚨 <b>Scheduler çöktü!</b>\n\n"
                f"<code>{type(e).__name__}: {e}</code>\n\n"
                f"<code>systemctl restart firsatvakti</code>"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main_loop())
