#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arka plan zamanlayıcı — VPS tarafı.
Sadece deal expire + DB temizliği yapar.
Scraping doğrudan scraper_worker.py tarafından yapılır (scan_queue yok).
"""

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta

import sync_status
from db import get_db, init_db
from telegram_pub import send_admin_alert
from config import AUTO_EXPIRE_HOURS

log = logging.getLogger("scheduler")


# ── Tazelik hedefleri (saat) — 2026-10-13 revizyonu, bkz. _requeue_worker 3-4 ──────
# 2026-11-01: 4/6/12 → 1/2/2 (kullanıcı kararı, "sonra yükseltiriz") — scraper
# kuyruğu sürekli boş bekliyordu; darboğaz tarayıcı değil bu aralıklardı. Bu,
# scraper kapasitesinin sınırı (2-4sn → platform başına saatte ~1.000-1.200
# istek): Trendyol/Hepsiburada'da engel riski artar, panelde izlenmeli.
# Amazon 2026-11-05: 12 → 96 saat. Katalog 3.300+ ürün, ev IP'si için güvenli hız
# saatte ~40 (bkz. scrapers/crawlee_base.py RL); 12 saat hedefi saatte 275 istek demekti.
ACTIVE_DEAL_HOURS = 1    # aktif fırsatlı ürün
IMPORTANT_HOURS   = 2    # şu an dipte, takip listesi, fırsat karşılaştırma eşleri
STANDARD_HOURS    = 12   # tarama turu kimliği (çerez sıfırlama) — tarama aralığı değil
_STANDARD_HOURS_DEFAULT = 2
_STANDARD_HOURS_BY_PLATFORM = {"amazon": 96}
AMAZON_MIN_HOURS  = 12   # Amazon'da fırsat/dipte/takip ürünleri de bundan sık taranmaz
PACE_MAX_BURST    = 20   # platform başına tek seferde kuyruğa en fazla bu kadar standart ürün


def _standard_hours(platform: str) -> int:
    return _STANDARD_HOURS_BY_PLATFORM.get(platform, _STANDARD_HOURS_DEFAULT)

_BASE_SQL = "p.platform IS NOT NULL AND p.platform <> 'beymen' AND p.source_url IS NOT NULL"
# Amazon'da "stok yok" dönen ürün 24 saat dinlenir: stok yok sonucu ne last_seen_at ne de
# hata kaydı yazdığı için aynı ürün iki saatte üç kez taranıyordu (2026-11-07).
_AMAZON_OOS_REST = ("NOT (p.platform = 'amazon' AND p.oos_last_at IS NOT NULL "
                    "AND p.oos_last_at::timestamp >= NOW() - INTERVAL '24 hours')")
_pace_state = {"last": _time.monotonic(), "credit": {}}
_dipte_cache = {"ids": [], "ts": 0.0}


def _due_rows(db, sql: str, hours: int) -> list:
    """sql içindeki {base}/{due} yer tutucularını doldurup vadesi gelmiş ürünleri döndürür.
    Şu an işlenmekte olanlar hariç; kuyrukta bekleyenler _enqueue'da öne çekilir/atlanır."""
    h = int(hours)
    hours_sql = f"(CASE WHEN p.platform = 'amazon' THEN {max(h, AMAZON_MIN_HOURS)} ELSE {h} END)"
    due = (f"(p.last_seen_at IS NULL OR p.last_seen_at::timestamp < NOW() - {hours_sql} * INTERVAL '1 hour') "
           "AND NOT EXISTS (SELECT 1 FROM scan_queue sq WHERE sq.product_id = p.id AND sq.status = 'processing') "
           f"AND {_AMAZON_OOS_REST} "
           # Hata alan ürünün last_seen_at'i güncellenmez → hep "en eski" görünüp her
           # fırsatta yeniden denenir ve diğerlerinin sırasını yerdi (captcha sırasında
           # engeli de büyütür). 2 saat dinlendir; fiyatı hiç alınamamışlar adım 2'de.
           "AND NOT EXISTS (SELECT 1 FROM scraper_errors e WHERE e.product_id = p.id "
           "AND e.occurred_at::timestamp >= NOW() - INTERVAL '2 hours')")
    return db.execute(sql.replace("{base}", _BASE_SQL).replace("{due}", due)).fetchall()


def _enqueue(db, rows, priority: int, now: str) -> tuple[int, int]:
    """Ürünleri kuyruğa ekler; kuyrukta daha düşük öncelikle bekliyorsa öne çeker.
    Dönüş: (eklenen, öne_çekilen)."""
    uniq = {}
    for r in rows:
        uniq.setdefault(r["id"], r)
    if not uniq:
        return 0, 0
    ids = ",".join(str(int(i)) for i in uniq)
    pending = {r["product_id"]: r for r in db.execute(
        f"SELECT product_id, MIN(priority) AS pr FROM scan_queue WHERE status='pending' "
        f"AND product_id IN ({ids}) GROUP BY product_id").fetchall()}
    added = bumped = 0
    for pid, r in uniq.items():
        cur = pending.get(pid)
        if cur is None:
            db.execute(
                "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) "
                "VALUES(?,?,?,'pending',?,?)", (pid, r["source_url"], r["platform"], priority, now))
            added += 1
        elif cur["pr"] > priority:
            db.execute("UPDATE scan_queue SET priority=? WHERE product_id=? AND status='pending' AND priority>?",
                       (priority, pid, priority))
            bumped += 1
    return added, bumped


def _dipte_ids(db) -> list:
    """Şu an tüm zamanların en düşüğünde ve medyanın %15+ altında olan ürünler
    (/dipte sayfasıyla aynı mantık). ~1 sn'lik sorgu — 10 dk önbellek."""
    if _time.monotonic() - _dipte_cache["ts"] < 600:
        return _dipte_cache["ids"]
    try:
        rows = db.execute("""
            WITH ph AS (SELECT product_id, price_value,
                               ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY scraped_at DESC, id DESC) AS rn
                        FROM price_history WHERE price_value > 0),
                 agg AS (SELECT product_id, MIN(price_value) AS mn,
                                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_value) AS med, COUNT(*) AS n
                         FROM ph GROUP BY product_id)
            SELECT a.product_id FROM agg a JOIN ph c ON c.product_id = a.product_id AND c.rn = 1
            WHERE a.n >= 4 AND c.price_value <= a.mn * 1.005
              AND c.price_value <= a.med * 0.85 AND c.price_value >= a.med * 0.25
        """).fetchall()
        _dipte_cache["ids"] = [r["product_id"] for r in rows]
    except Exception as e:
        log.warning(f"[requeue] dipte listesi alınamadı: {e}")
    _dipte_cache["ts"] = _time.monotonic()
    return _dipte_cache["ids"]


def now_str():
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Platforma göre "kontrol edilmedi" süresi. Beymen günde bir (24 sa) eşitleniyor,
# Karaca 12 saatte bir taranıyor — sabit 12 saat onaylanan Beymen fırsatlarını bir
# sonraki eşitlemeyi beklemeden kapatıyordu (2026-10-13). Tarama aralığı + pay.
_EXPIRE_HOURS_BY_PLATFORM = {"beymen": 30, "karaca": 18}


async def auto_expire_stale_deals():
    """Platformun tarama aralığı + pay kadar süredir kontrol edilmemiş aktif deal'ları
    pasife al (varsayılan AUTO_EXPIRE_HOURS)."""
    db = get_db()
    now = now_str()
    hours_case = "CASE p.platform " + " ".join(
        f"WHEN '{pl}' THEN {h}" for pl, h in _EXPIRE_HOURS_BY_PLATFORM.items()
    ) + f" ELSE {int(AUTO_EXPIRE_HOURS)} END"
    expired = db.execute(f"""
        SELECT d.id, p.platform FROM deals d
        JOIN products p ON d.product_id = p.id
        WHERE d.active = 1
          AND (
            p.last_seen_at IS NULL
            OR p.last_seen_at::timestamp < NOW() - ({hours_case}) * INTERVAL '1 hour'
          )
    """).fetchall()
    for row in expired:
        db.execute("UPDATE deals SET active=0, status='expired', expires_at=? WHERE id=?", (now, row["id"]))
        h = _EXPIRE_HOURS_BY_PLATFORM.get(row["platform"], AUTO_EXPIRE_HOURS)
        log.info(f"Deal #{row['id']} pasife alındı — {h} saattir kontrol edilmedi")
    if expired:
        db.commit()
        log.info(f"[expire] Toplam {len(expired)} deal pasife alındı")


async def _expire_worker():
    """Stale deal temizliği — startup'ta hemen, sonra 30 dakikada bir çalışır."""
    try:
        await auto_expire_stale_deals()
    except Exception as e:
        log.error(f"[expire] Startup hatası: {e}", exc_info=True)
    while True:
        await asyncio.sleep(1800)
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
                "DELETE FROM scraper_errors WHERE occurred_at::timestamp < NOW() - INTERVAL '24 hours'"
            )
            deleted = cur.rowcount
            db.commit()
            if deleted:
                log.info(f"[cleanup] scraper_errors={deleted} silindi")
        except Exception as e:
            log.error(f"[cleanup] Hata: {e}", exc_info=True)
        await asyncio.sleep(21600)  # 6 saatte bir



def _job_due(job, name: str) -> bool:
    """Günlük iş panelden durdurulmadıysa ve vakti geldiyse çalışır."""
    return not sync_status.is_paused(name) and job.is_due()


async def _karaca_discovery_worker():
    """Karaca kategori keşfi (çok satanlar + indirimli ilk 10 sayfa) — günde bir.
    Son çalışma zamanı settings'te; servis yeniden başlasa da 24 saat kuralı korunur."""
    import karaca_discovery
    await asyncio.sleep(60)  # başlangıçta diğer işçilere öncelik ver
    while True:
        try:
            if await asyncio.to_thread(_job_due, karaca_discovery, "karaca"):
                await asyncio.to_thread(karaca_discovery.discover)
        except Exception as e:
            log.error(f"[karaca-discovery] Hata: {e}", exc_info=True)
        await asyncio.sleep(60)  # panelden "Başlat" en geç 1 dk içinde çalıştırsın


async def _beymen_sync_worker():
    """Beymen eşitleme (keşif + fiyat) — günde bir. Uzun sürer (~266 sayfa + ~12k
    webhook ≈ 30-40 dk); thread'de çalışır, diğer işçileri bloklamaz.
    Başlangıçta Karaca keşfiyle çakışmasın diye 5 dk bekler."""
    import beymen_sync
    await asyncio.sleep(300)
    while True:
        try:
            if await asyncio.to_thread(_job_due, beymen_sync, "beymen"):
                await asyncio.to_thread(beymen_sync.sync)
        except Exception as e:
            log.error(f"[beymen] Hata: {e}", exc_info=True)
        await asyncio.sleep(60)  # panelden "Başlat" en geç 1 dk içinde çalıştırsın


async def _requeue_worker():
    """Sürekli döngü — tazelik hedefli kuyruk (bkz. adım 3-4 ve ACTIVE_DEAL_HOURS vb.)."""
    log.info("[requeue] başladı")
    while True:
        try:
            db = get_db()
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # 1. 10+ dakikadır processing'te takılı olanları pending'e al
            stale = db.execute("""
                UPDATE scan_queue SET status='pending', updated_at=?
                WHERE status='processing'
                  AND updated_at::timestamp < NOW() - INTERVAL '10 minutes'
            """, (now,)).rowcount
            if stale:
                log.info(f"[requeue] {stale} takılı iş pending'e döndürüldü")

            # 2. Yeni eklenen linkler — fiyatı HİÇ alınamamış ürünler (priority=0).
            # DİKKAT: eskiden "last_seen_at IS NULL" bakılıyordu ama enqueue_url ürünü
            # eklerken last_seen_at=now yazıyor → bu adım hiç çalışmıyordu; ilk denemesi
            # captcha'ya takılan yeni link 500 işlik normal kuyruğun sonuna düşüp ~7 saat
            # bekliyordu (2026-10-08). 24 saatte 5'ten fazla hata alan ürün atlanır ki
            # gerçekten ölü bir link kuyruğu tıkamasın.
            new_rows = db.execute(f"""
                SELECT p.id, p.source_url, p.platform
                FROM products p
                WHERE p.platform IS NOT NULL AND p.platform <> 'beymen'
                  AND p.source_url IS NOT NULL
                  AND {_AMAZON_OOS_REST}
                  AND NOT EXISTS (SELECT 1 FROM price_history h WHERE h.product_id = p.id)
                  AND (SELECT COUNT(*) FROM scraper_errors e WHERE e.product_id = p.id
                       AND e.occurred_at::timestamp >= NOW() - INTERVAL '24 hours') < 5
                  -- son hatadan en az 30 dk geçmeli: captcha varken art arda denemek
                  -- engeli büyütüyor ve 5 deneme hakkını dakikalar içinde tüketiyordu
                  AND NOT EXISTS (SELECT 1 FROM scraper_errors e2 WHERE e2.product_id = p.id
                       AND e2.occurred_at::timestamp >= NOW() - INTERVAL '30 minutes')
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
            # Kuyrukta normal öncelikle bekleyenleri öne çek (yoksa ~500 işin arkasında kalır)
            bumped = db.execute(f"""
                UPDATE scan_queue SET priority = 0
                WHERE status = 'pending' AND priority > 0
                  AND product_id IN (
                      SELECT p.id FROM products p
                      WHERE p.platform IS NOT NULL AND p.platform <> 'beymen'
                        AND {_AMAZON_OOS_REST}
                        AND NOT EXISTS (SELECT 1 FROM price_history h WHERE h.product_id = p.id)
                        AND (SELECT COUNT(*) FROM scraper_errors e WHERE e.product_id = p.id
                             AND e.occurred_at::timestamp >= NOW() - INTERVAL '24 hours') < 5
                        AND NOT EXISTS (SELECT 1 FROM scraper_errors e2 WHERE e2.product_id = p.id
                             AND e2.occurred_at::timestamp >= NOW() - INTERVAL '30 minutes')
                  )
            """).rowcount
            if new_rows or bumped:
                db.commit()
                log.info(f"[requeue] fiyatı alınamamış ürün: {len(new_rows)} kuyruğa eklendi, "
                         f"{bumped} öne çekildi (priority=0)")

            # ── 3-4. TAZELİK HEDEFLİ KUYRUK (2026-10-13 revizyonu) ──────────────────
            # Eski model: tüm ürünler tek "tur"da, kuyruk boşalınca catchup hemen baştan
            # dolduruyordu → küçük platformlar günde 3-5 kez, büyükler ~2.5 kez taranıyor,
            # aktif fırsatlar 24 saatte bir kontrol ediliyor, "şu an dipte" ürünlerin
            # özel önceliği yoktu, Karaca 12 saatte bir topluca (~2 saatte 900 ürün)
            # taranıyordu. Yeni model: her ürün grubu için hedef tazelik.
            #   priority 1 — aktif fırsat: 4 sa | dipte + takip listesi + fırsat
            #                karşılaştırma eşleri: 6 sa  (kuyruktaysa öne çekilir)
            #   priority 2 — standart ürünler (Karaca dahil): 12 sa, platform başına
            #                EŞİT HIZLA beslenir (patlama yok, engel riski düşük)
            # Beymen hariç tümü (o liste eşitlemesiyle günde bir güncellenir).
            tier1 = []
            tier1 += _due_rows(db, """
                SELECT DISTINCT p.id, p.source_url, p.platform FROM products p
                JOIN deals d ON d.product_id = p.id AND d.active = 1
                WHERE {base} AND {due}""", ACTIVE_DEAL_HOURS)
            tier1 += _due_rows(db, """
                SELECT DISTINCT p.id, p.source_url, p.platform FROM products p
                JOIN product_watchlist w ON w.product_id = p.id
                WHERE {base} AND {due}""", IMPORTANT_HOURS)
            tier1 += _due_rows(db, """
                SELECT DISTINCT p.id, p.source_url, p.platform FROM products p
                JOIN product_group_members pgm ON pgm.product_id = p.id AND pgm.match_type != 'removed'
                WHERE {base} AND {due}
                  AND EXISTS (SELECT 1 FROM product_group_members pgm2
                              JOIN deals d ON d.product_id = pgm2.product_id AND d.active = 1
                              WHERE pgm2.group_id = pgm.group_id AND pgm2.product_id <> p.id)""",
                IMPORTANT_HOURS)
            dipte_ids = _dipte_ids(db)
            if dipte_ids:
                ph = ",".join(str(int(i)) for i in dipte_ids)
                tier1 += _due_rows(db, f"""
                    SELECT p.id, p.source_url, p.platform FROM products p
                    WHERE p.id IN ({ph}) AND {{base}} AND {{due}}""", IMPORTANT_HOURS)
            added1, bumped1 = _enqueue(db, tier1, 1, now)
            if added1 or bumped1:
                db.commit()
                log.info(f"[requeue] önemli ürünler (fırsat/dipte/takip): {added1} eklendi, {bumped1} öne çekildi")

            # Standart: platform başına eşit hız — ürün_sayısı / _standard_hours(platform)
            paced = []
            elapsed = max(0.0, _time.monotonic() - _pace_state["last"])
            _pace_state["last"] = _time.monotonic()
            for pr in db.execute(
                "SELECT platform, COUNT(*) AS cnt FROM products WHERE platform IS NOT NULL "
                "AND platform <> 'beymen' AND source_url IS NOT NULL GROUP BY platform"
            ).fetchall():
                # DİKKAT: db katmanının satırları dict gibi — tuple açma (for a, b in ...)
                # sütun ADLARINI verir, değerleri değil
                plat, count = pr["platform"], pr["cnt"]
                std_h = _standard_hours(plat)
                rate_per_s = count / (std_h * 3600)
                credit = min(_pace_state["credit"].get(plat, 0.0) + rate_per_s * elapsed, PACE_MAX_BURST)
                take = int(credit)
                if take > 0:
                    rows = _due_rows(db, """
                        SELECT p.id, p.source_url, p.platform FROM products p
                        WHERE p.platform = '{plat}' AND {{base}} AND {{due}}
                        ORDER BY COALESCE(p.last_seen_at, '2000-01-01') ASC
                        LIMIT {take}""".format(plat=plat, take=take), std_h)
                    paced += rows
                    # vadesi gelmiş ürün kotadan azsa kalan krediyi biriktirme (sonradan patlama olmasın)
                    credit = 0.0 if len(rows) < take else credit - len(rows)
                _pace_state["credit"][plat] = max(credit, 0.0)
            added2, _ = _enqueue(db, paced, 2, now)
            if added2:
                db.commit()

            # 5. Zaman bazlı temizlik — 48 saatlik done/failed kayıtları sil
            cleaned = db.execute(
                "DELETE FROM scan_queue WHERE status IN ('done', 'failed')"
                " AND updated_at::timestamp < NOW() - INTERVAL '48 hours'"
            ).rowcount
            if cleaned:
                db.commit()
                log.info(f"[requeue] {cleaned} eski done/failed kayıt temizlendi")

            # 6. Tarama turu kimliği — artık "tüm ürünler bitti" diye beklenmiyor (sürekli,
            # tazelik hedefli tarama); STANDARD_HOURS'ta bir yenilenir. Windows scraper
            # X-Scan-Round başlığıyla bunu izleyip tur değişince çerezleri temizliyor.
            rr = db.execute("SELECT value FROM settings WHERE key='scan_round_started_at'").fetchone()
            if not rr or datetime.utcnow() - datetime.strptime(str(rr["value"])[:19], "%Y-%m-%d %H:%M:%S") >= timedelta(hours=STANDARD_HOURS):
                db.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    ("scan_round_started_at", now, now)
                )
                db.commit()
                log.info("[requeue] yeni tarama turu kimliği (çerez temizliği sinyali)")

            # PostgreSQL'de açık transaction'ı kapat — CREATE INDEX gibi DDL'lerin
            # lock beklemesini önlemek için her döngü sonunda commit zorunlu.
            db.commit()

            # 30s -> 5s: yeni-link/deal-yenileme/tur taraması işleri artık
            # scan_queue'ya çok daha sık düşüyor, taranan bir platform bir
            # sonraki işi 30s yerine en fazla 5s içinde bulabiliyor.
            await asyncio.sleep(5)

        except Exception as e:
            log.error(f"[requeue] Hata: {e}", exc_info=True)
            await asyncio.sleep(5)


async def main_loop():
    """Deal expire + DB temizliği. Scraping scraper_worker.py tarafından yapılır."""
    init_db()
    log.info("Scheduler başladı — deal expire + DB temizliği (scraping=scraper_worker.py)")
    try:
        await asyncio.gather(
            _expire_worker(),
            _cleanup_worker(),
            _requeue_worker(),
            _karaca_discovery_worker(),
            _beymen_sync_worker(),
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
