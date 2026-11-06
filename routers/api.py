"""Scraper API: pending-jobs, webhook, dispatch mantığı, public API, health."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from affiliate import make_short_slug
from db import get_db
from email_utils import send_price_alert
from price_rules import (YOYO_CONFIRM_HOURS, DealDecision, evaluate_drop, is_price_sane, is_yoyo,
                         min_discount_for)
from telegram_pub import notify_pending_approval, notify_approved_deal_update

from config import HB_CART_BASELINE_UNTIL, SCAN_ONLY_PLATFORMS, SCAN_ONLY_SQL
from sync_status import paused_platforms

from .deps import now_str, require_admin

log = logging.getLogger("main")

router = APIRouter()

SCRAPER_SERVICE_URL = os.getenv("SCRAPER_SERVICE_URL", "")
SCRAPER_SERVICE_KEY = os.getenv("SCRAPER_SERVICE_KEY", "")
WEBHOOK_KEY = os.getenv("FRONTEND_WEBHOOK_KEY", "")
USE_LOCAL_SCRAPER = os.getenv("USE_LOCAL_SCRAPER", "false").lower() == "true"

_SCAN_STATE: dict = {
    "running": False, "current": 0, "total": 0,
    "title": "", "platform": "", "done": 0, "failed": 0, "errors": [],
}


class WebhookPayload(BaseModel):
    product_id: int
    data: dict = {}
    url: str = ""
    platform: str = ""


async def _run_cross_search(product_id, platforms=None):
    import asyncio
    from cross_search import cross_search_product
    for _ in range(18):
        await asyncio.sleep(5)
        row = get_db().execute("SELECT title FROM products WHERE id=?", (product_id,)).fetchone()
        if row and row["title"]:
            break
    await cross_search_product(get_db(), product_id, platforms)


_CROSS_SEM = None


async def _cross_search_throttled(product_id: int):
    """Barkodu yeni gelen ürün için çapraz arama — aynı anda TEK arama (binlerce ürün
    aynı saatte barkod alabiliyor; VPS'ten mağazalara istek fırtınası olmasın).
    Zaten başka mağazayla eşleşmişse atlanır."""
    global _CROSS_SEM
    if _CROSS_SEM is None:
        _CROSS_SEM = asyncio.Semaphore(1)
    async with _CROSS_SEM:
        try:
            db = get_db()
            matched = db.execute(
                "SELECT 1 FROM product_group_members a JOIN product_group_members b "
                "ON b.group_id=a.group_id AND b.product_id<>a.product_id WHERE a.product_id=? LIMIT 1",
                (product_id,)).fetchone()
            if matched:
                return
            from cross_search import cross_search_product
            await cross_search_product(db, product_id)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"[cross] #{product_id} çapraz arama hatası: {e}")


async def _dispatch_scrape(product_id: int, url: str, platform: str, background_tasks,
                           cross_search_plats: list = None):
    """Scraper servis varsa oraya gönder, USE_LOCAL_SCRAPER=true ise kuyruğa bırak, yoksa lokal çalıştır."""
    if SCRAPER_SERVICE_URL:
        background_tasks.add_task(_remote_scrape, product_id, url, platform)
    elif USE_LOCAL_SCRAPER:
        log.debug(f"[dispatch] #{product_id} scan_queue'da bekliyor (USE_LOCAL_SCRAPER=true)")
    else:
        background_tasks.add_task(scrape_and_save, product_id, url, platform)
    if cross_search_plats:
        background_tasks.add_task(_run_cross_search, product_id, cross_search_plats)


async def _remote_scrape(product_id: int, url: str, platform: str):
    import httpx
    headers = {"X-Api-Key": SCRAPER_SERVICE_KEY}
    payload = {"product_id": product_id, "url": url, "platform": platform}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{SCRAPER_SERVICE_URL}/scrape", json=payload, headers=headers)
            log.info(f"[scraper-svc] → {r.status_code} product_id={product_id}")
    except Exception as e:
        log.warning(f"[scraper-svc] ✘ Bağlantı hatası, lokal çalışıyor: {e}")
        await scrape_and_save(product_id, url, platform)


async def scrape_and_save(product_id: int, url: str, platform: str):
    import asyncio
    from scrapers.router import scrape_product
    try:
        data = await asyncio.wait_for(scrape_product(url, platform), timeout=75)
        if data:
            await _save_scraped_data(product_id, data)
            return
        log.warning(f"[scrape] #{product_id} veri yok")
    except asyncio.TimeoutError:
        log.warning(f"[scrape] Timeout #{product_id} ({platform}) — 75s aşıldı")
    except Exception as e:
        log.error(f"[scrape] Hata #{product_id}: {e}")
    db = get_db()
    db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?", (now_str(), product_id))
    db.commit()


def _price_points(db, product_id: int, days: int = 75) -> list:
    """Son `days` günün fiyat değişimleri + o pencereden önceki son fiyat
    (price_history sadece değişimleri tutar; pencere başındaki fiyat için gerekli)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    before = db.execute(
        "SELECT scraped_at, price_value FROM price_history WHERE product_id=? AND scraped_at<? "
        "ORDER BY scraped_at DESC LIMIT 1", (product_id, cutoff)
    ).fetchall()
    rows = db.execute(
        "SELECT scraped_at, price_value FROM price_history WHERE product_id=? AND scraped_at>=? "
        "ORDER BY scraped_at ASC", (product_id, cutoff)
    ).fetchall()
    return [(r["scraped_at"], r["price_value"]) for r in list(before) + list(rows)]


def _product_platform(db, product_id: int) -> str | None:
    row = db.execute("SELECT platform FROM products WHERE id=?", (product_id,)).fetchone()
    return row["platform"] if row else None


def _to_dt(s: str) -> datetime:
    return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")


# Fiyat doğrulama: şüpheli fiyat kaydedilmez, ürün hemen yeniden taranır; aynı fiyat
# ikinci kez gelirse gerçek kabul edilir. {product_id: (şüpheli_fiyat, deneme_sayısı)}
_PRICE_SUSPECTS: dict = {}
_MAX_SUSPECT_RETRIES = 3


def _check_price_sanity(db, product_id: int, price: float, now: str) -> bool:
    """False → fiyat kaydedilmemeli (yeniden tarama kuyruğa alındı ya da vazgeçildi)."""
    ok, med = is_price_sane(_price_points(db, product_id, 30), price, _to_dt(now))
    if ok:
        _PRICE_SUSPECTS.pop(product_id, None)
        return True
    prev = _PRICE_SUSPECTS.get(product_id)
    if prev and abs(prev[0] - price) / max(price, 1) < 0.05:
        log.warning(f"[webhook] ✔ #{product_id} şüpheli fiyat {price} tekrar okundu — gerçek kabul edildi (medyan={med:.2f})")
        _PRICE_SUSPECTS.pop(product_id, None)
        return True
    tries = (prev[1] + 1) if prev else 1
    if tries > _MAX_SUSPECT_RETRIES:
        log.warning(f"[webhook] ✖ #{product_id} {tries-1} kez tutarsız şüpheli fiyat — bu tur atlandı")
        _PRICE_SUSPECTS.pop(product_id, None)
        return False
    _PRICE_SUSPECTS[product_id] = (price, tries)
    log.warning(f"[webhook] ⚠ #{product_id} şüpheli fiyat {price} (30g medyan={med:.2f}) — kaydedilmedi, yeniden taranacak")
    prod = db.execute("SELECT source_url, platform FROM products WHERE id=?", (product_id,)).fetchone()
    db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
    # Beymen'i Windows scraper taramaz (beymen_sync.py liste sayfasından günde bir) —
    # yeniden tarama kuyruğu yerine ertesi günkü eşitleme fiyatı teyit eder
    if prod and prod["platform"] != "beymen":
        db.execute(
            "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) VALUES(?,?,?,'pending',-1,?)",
            (product_id, prod["source_url"], prod["platform"], now)
        )
    db.commit()
    return False


def _smart_price_analysis(db, product_id: int, price: float, prev_price: float):
    # KULLANILMIYOR (2026-09-30): yerini price_rules.evaluate_drop aldı (30 gün kuralı).
    rows = db.execute(
        "SELECT price_value, scraped_at FROM price_history "
        "WHERE product_id=? AND scraped_at::timestamp >= NOW() - INTERVAL '90 days' "
        "ORDER BY scraped_at ASC", (product_id,)
    ).fetchall()
    prices = [r[0] for r in rows]

    if len(prices) < 3:
        return price < prev_price, prev_price, "veri az"

    sorted_p = sorted(prices)
    median = sorted_p[len(sorted_p) // 2]

    def _seen_count(p, ps):
        return sum(1 for x in ps if abs(x - p) / max(p, 1) < 0.03)

    clean = [p for p in prices if
             (abs(p - median) / max(median, 1) <= 0.45 or _seen_count(p, prices) > 2)
             and p >= median * 0.20
             and p <= median * 5.0]
    if not clean:
        clean = prices
    clean_sorted = sorted(clean)

    best_p, best_cnt = clean[0], 0
    for p in clean:
        cnt = sum(1 for x in clean if abs(x - p) / max(p, 1) < 0.05)
        if cnt > best_cnt:
            best_cnt, best_p = cnt, p
    modal_price = best_p

    cutoff_14 = db.execute("SELECT TO_CHAR(NOW() - INTERVAL '14 days', 'YYYY-MM-DD HH24:MI:SS')").fetchone()[0]
    recent_14 = [r[0] for r in rows if r[1] >= cutoff_14]
    pre_spike_price = None
    if len(recent_14) >= 2 and recent_14[-1] > prev_price:
        older = [p for p in clean if p < prev_price * 0.90]
        if older:
            pre_spike_price = max(older)

    ref_price = pre_spike_price or modal_price or max(clean_sorted)
    if ref_price <= price:
        ref_price = prev_price

    p30 = clean_sorted[max(0, int(len(clean_sorted) * 0.30) - 1)]
    if price >= p30:
        return False, ref_price, f"tarihsel dusuk degil (esik={p30:.0f})"

    return True, ref_price, "gercek indirim"


def _update_product_fields(db, product_id: int, data: dict, stock, now: str):
    variants_json = json.dumps(data["variants"], ensure_ascii=False) if data.get("variants") else None
    ai_summary = data.get("ai_review_summary") or None
    # title ve image_url write-once: bir kez kaydedildikten sonra üzerine yazılmaz
    db.execute(
        "UPDATE products SET"
        " title=CASE WHEN (title IS NOT NULL AND LENGTH(TRIM(title))>3) THEN title ELSE COALESCE(?,title) END,"
        " image_url=CASE WHEN (image_url IS NOT NULL AND LENGTH(image_url)>10) THEN image_url ELSE COALESCE(?,image_url) END,"
        " description=COALESCE(?,description), rating=COALESCE(?,rating),"
        " review_count=COALESCE(?,review_count), brand=COALESCE(?,brand),"
        " barcode=COALESCE(?,barcode), stock=COALESCE(?,stock),"
        " seller=COALESCE(?,seller),"
        " variants=COALESCE(?,variants),"
        " ai_review_summary=COALESCE(?,ai_review_summary),"
        " last_seen_at=? WHERE id=?",
        (data.get("title"), data.get("image_url"), data.get("description"),
         data.get("rating"), data.get("review_count"), data.get("brand"),
         data.get("barcode"), stock, (data.get("seller") or None),
         variants_json, ai_summary, now, product_id)
    )


_OOS_KEYWORDS = ["mevcut değil", "tükendi", "stokta yok", "out of stock", "sold out", "unavailable"]


def _is_oos(stock: str) -> bool:
    if not stock:
        return False
    return stock == "Stok Yok" or any(kw in stock.lower() for kw in _OOS_KEYWORDS)


# Varsayılan 1: ilk "stok yok"ta sil. İstisnalar:
#  amazon 3      — fiyat ve sepet düğmesi yoksa ürün bitmiş sayılır (2026-11-06 kullanıcı
#                  isteği); fiyat gelince sayaç sıfırlanır
#  hepsiburada 3 — aynı ürün dakikalar içinde satıcısız/satıcılı gidip geliyor
#                  (#2322: 20:27 stok yok, 20:28 29.999 TL); fiyat gelince sayaç sıfırlanır
#  beymen 3      — beymen_sync'in tasarımı; günlük liste eşitlemesi silineni geri ekliyor
OOS_DELETE_AFTER = {"amazon": 3, "hepsiburada": 3, "beymen": 3}
# Amazon'da iki "stok yok" sayımı arasında en az bu kadar saat olmalı (2026-11-07). Fiyatsız
# ürün yeniden denemesi 3 sayımı ortalama 104 dakikaya sıkıştırıp 559 ürünü iki saatte sildi.
OOS_MIN_GAP_HOURS = {"amazon": 24}


def _handle_out_of_stock(db, product_id: int, price: float, now: str):
    prev_s = db.execute(
        "SELECT price_value FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 1",
        (product_id,)
    ).fetchone()
    # Fiyat yoksa son bilinen fiyatı kullan — OOS'u hata olarak kaydetme
    effective_price = price if (price and price > 0) else (prev_s["price_value"] if prev_s else None)
    if effective_price and (not prev_s or prev_s["price_value"] != effective_price):
        db.execute(
            "INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
            (product_id, effective_price, "TRY", now)
        )
    for d in db.execute("SELECT id FROM deals WHERE product_id=? AND active=1", (product_id,)).fetchall():
        db.execute("UPDATE deals SET active=0, status='expired', expires_at=? WHERE id=?", (now, d["id"]))
        log.info(f"[webhook] ⏹ Deal #{d['id']} kapatıldı — stok yok")

    row = db.execute("SELECT oos_count, platform, oos_last_at FROM products WHERE id=?", (product_id,)).fetchone()
    gap_hours = OOS_MIN_GAP_HOURS.get(row["platform"] if row else "")
    if gap_hours and row["oos_last_at"] and _to_dt(now) - _to_dt(row["oos_last_at"]) < timedelta(hours=gap_hours):
        db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
        db.commit()
        return
    oos = (row["oos_count"] or 0) + 1 if row else 1
    # 2026-10-31 kullanıcı isteği: stoğu olmayan ürün silinsin (eskiden 3 teyit).
    # Silme sınırı platforma göre OOS_DELETE_AFTER'da.
    limit = OOS_DELETE_AFTER.get(row["platform"] if row else "", 1)
    if oos >= limit:
        log.info(f"[webhook] 🗑 Ürün #{product_id} {oos} kez stok yok — siliniyor")
        from .admin import _delete_product_cascade
        _delete_product_cascade(db, product_id, "stok_yok")
    else:
        db.execute("UPDATE products SET oos_count=?, oos_last_at=? WHERE id=?", (oos, now, product_id))
        db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
        db.commit()


def _close_expired_deals(db, product_id: int, price: float, now: str):
    for deal in db.execute(
        "SELECT id, new_price, cart_discount FROM deals WHERE product_id=? AND active=1", (product_id,)
    ).fetchall():
        close_threshold = 1.15 if deal["cart_discount"] else 1.02
        if price > deal["new_price"] * close_threshold:
            db.execute(
                "UPDATE deals SET active=0, status='expired', expires_at=? WHERE id=?", (now, deal["id"])
            )
            log.info(f"[webhook] ⏹ Deal #{deal['id']} kapandı — fiyat yükseldi (eşik=%{int((close_threshold-1)*100)})")


def _hb_cart_baseline(db, product_id: int, price: float, data: dict, now: str) -> bool:
    """Geçiş dönemi: Hepsiburada sepet fiyatının ilk okunuşu gerçek bir düşüş değil,
    fırsat üretmez. Bekleyen fırsat varsa yanlış (sepetsiz) fiyatı düzeltilir."""
    if (not data.get("cart_discount") or now >= HB_CART_BASELINE_UNTIL
            or _product_platform(db, product_id) != "hepsiburada"):
        return False
    db.execute(
        "UPDATE deals SET new_price=?, cart_discount=1, "
        "discount_pct=ROUND(((old_price-?)/old_price*100)::numeric,1) "
        "WHERE product_id=? AND status='pending' AND old_price>0",
        (price, price, product_id)
    )
    return True


async def _process_deal(db, product_id: int, price: float, prev_price_stable: float,
                        data: dict, now: str, decision: DealDecision = None) -> bool:
    """Deal oluşturma/güncelleme mantığı. False döndürürse caller erken çıkmalı.

    Fırsat kuralı (price_rules.evaluate_drop): fiyat son 30 günün en düşük fiyatının
    altına inmeli ve 30 günlük (süre ağırlıklı) medyana göre en az %10 ucuz olmalı
    (platforma özel eşik: price_rules.PLATFORM_MIN_DISCOUNT_PCT — Karaca %3).
    old_price = 30 günlük medyan → gösterilen indirim oranı gerçek indirim.
    Yo-yo ürünlerde fırsat hemen açılmaz; fiyat YOYO_CONFIRM_HOURS sabit kalınca
    _maybe_confirm_yoyo_deal açar."""
    platform = _product_platform(db, product_id)
    min_pct = min_discount_for(platform)   # Karaca %3, diğerleri %10
    if decision is None:
        points = _price_points(db, product_id)
        decision = evaluate_drop(points, price, _to_dt(now), min_pct, platform)
        if decision.is_deal and is_yoyo(points, _to_dt(now)):
            log.info(f"[webhook] ⏳ #{product_id} yo-yo ürün — %{decision.pct:.1f} düşüş "
                     f"{YOYO_CONFIRM_HOURS} saat sabit kalırsa fırsat olacak")
            return False
    if not decision.is_deal:
        log.info(f"[webhook] ⏭ #{product_id} deal degil: {decision.reason}")
        return False

    old_price = round(decision.ref_price, 2)
    pct = decision.pct
    stock = data.get("stock", "")

    if pct > 90:
        log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim şüpheli (>90), deal oluşturulmadı")
        return True
    if stock != "Stokta Var" and pct > 75:
        log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim + stok={stock!r} şüpheli, atlandı")
        return True
    if pct < min(5, min_pct):
        return True

    already_published = db.execute("""
        SELECT id FROM deal_publish_log
        WHERE product_id=? AND discount_pct=?
        AND published_at::timestamp >= NOW() - INTERVAL '1 day'
        LIMIT 1
    """, (product_id, round(pct))).fetchone()

    if already_published:
        log.info(f"[webhook] ⏭ #{product_id} %{round(pct)} indirim son 24 saatte yayınlandı, atlandı")
        return True

    existing_deal = db.execute("""
        SELECT d.id, sl.slug FROM deals d
        LEFT JOIN short_links sl ON sl.deal_id = d.id
        WHERE d.product_id=? AND d.status = 'pending'
        ORDER BY d.id DESC LIMIT 1
    """, (product_id,)).fetchone()

    if existing_deal:
        deal_id = existing_deal["id"]
        db.execute(
            "UPDATE deals SET old_price=?,new_price=?,discount_pct=?,cart_discount=?,created_at=? WHERE id=?",
            (old_price, price, round(pct, 1), 1 if data.get("cart_discount") else 0, now, deal_id)
        )
        if not existing_deal["slug"]:
            db.execute(
                "INSERT INTO short_links(deal_id,slug,created_at) VALUES(?,?,?)",
                (deal_id, make_short_slug(), now)
            )
    else:
        slug = make_short_slug()
        cart_disc = 1 if data.get("cart_discount") else 0
        coupon_text = data.get("coupon") or None
        cur = db.execute(
            "INSERT INTO deals(product_id,old_price,new_price,discount_pct,active,status,cart_discount,coupon,created_at) "
            "VALUES(?,?,?,?,0,'pending',?,?,?)",
            (product_id, old_price, price, round(pct, 1), cart_disc, coupon_text, now)
        )
        deal_id = cur.lastrowid
        db.execute(
            "INSERT INTO short_links(deal_id,slug,created_at) VALUES(?,?,?)",
            (deal_id, slug, now)
        )
        db.execute(
            "INSERT INTO deal_publish_log(product_id,discount_pct,published_at,deal_id) VALUES(?,?,?,?)",
            (product_id, round(pct), now, deal_id)
        )

    db.commit()
    log.info(f"[webhook] 📋 Deal #{deal_id} onay bekliyor: #{product_id} %{pct:.1f}")

    # Yalnızca taranan platform: indirim admin listesinde durur, kimseye bildirim gitmez
    if platform in SCAN_ONLY_PLATFORMS:
        return True

    # Onaylı (yayında) deal varsa admin'e [DEAL] etiketiyle ayrı bildirim at
    approved_deal = db.execute("""
        SELECT d.id FROM deals d
        WHERE d.product_id=? AND d.status='approved' AND d.active=1
        ORDER BY d.id DESC LIMIT 1
    """, (product_id,)).fetchone()
    if approved_deal:
        try:
            prod_row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if prod_row:
                prod_dict = dict(prod_row)
                prod_dict["deal_id"] = approved_deal["id"]
                prod_dict["cart_discount"] = bool(data.get("cart_discount"))
                await notify_approved_deal_update(
                    approved_deal["id"], prod_dict, price, old_price, round(pct, 1)
                )
        except Exception as e:
            log.warning(f"[webhook] Onaylı deal güncelleme bildirimi hatası: {e}")

    if not existing_deal:
        try:
            pending_count = db.execute(
                "SELECT COUNT(*) FROM deals WHERE status='pending'"
            ).fetchone()[0]
            prod_row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if prod_row:
                prod_dict = dict(prod_row)
                prod_dict["deal_id"] = deal_id
                prod_dict["cart_discount"] = bool(data.get("cart_discount"))
                await notify_pending_approval(
                    deal_id, prod_dict, price, old_price, round(pct, 1), pending_count
                )
        except Exception as e:
            log.warning(f"[webhook] Telegram bildirimi hatası: {e}")

        try:
            site_url = os.getenv("SITE_URL", "https://firsatvakti.com")
            prod_t = db.execute(
                "SELECT title FROM products WHERE id=?", (product_id,)
            ).fetchone()
            title_str = (prod_t["title"] or f"Ürün #{product_id}") if prod_t else f"Ürün #{product_id}"
            group_row = db.execute(
                "SELECT group_id FROM product_group_members WHERE product_id=?", (product_id,)
            ).fetchone()
            gids = [r["product_id"] for r in db.execute(
                "SELECT product_id FROM product_group_members WHERE group_id=?",
                (group_row["group_id"],)
            ).fetchall()] if group_row else [product_id]
            ph = ",".join("?" * len(gids))
            watchers = db.execute(
                f"SELECT DISTINCT u.email, u.username FROM product_watchlist pw "
                f"JOIN users u ON pw.user_id=u.id WHERE pw.product_id IN ({ph})", gids
            ).fetchall()
            for w in watchers:
                try:
                    await asyncio.to_thread(
                        send_price_alert,
                        to=w["email"], username=w["username"],
                        product_title=title_str, old_price=old_price,
                        new_price=price, pct=pct,
                        deal_url=f"{site_url}/deal/{deal_id}"
                    )
                except Exception as e:
                    log.warning(f"[webhook] Email hatası ({w['email']}): {e}")
        except Exception as e:
            log.warning(f"[webhook] Watchlist bildirimi hatası: {e}")

    return True


async def _maybe_confirm_yoyo_deal(db, product_id: int, price: float, since: str,
                                   data: dict, now: str) -> None:
    """Yo-yo ürün: düşük fiyat `since`'ten beri YOYO_CONFIRM_HOURS sabit kaldıysa fırsatı
    şimdi aç. Pencere, fiyatın ilk göründüğü andan ÖNCEKİ 30 gün (kendi kaydı hariç)."""
    try:
        first_seen = _to_dt(since)
        now_dt = _to_dt(now)
        if now_dt - first_seen < timedelta(hours=YOYO_CONFIRM_HOURS):
            return
        points = _price_points(db, product_id)
        if not is_yoyo(points, first_seen):
            return   # yo-yo değilse fırsat kararı zaten fiyat ilk geldiğinde verildi
        already = db.execute(
            "SELECT 1 FROM deals WHERE product_id=? AND new_price=? AND created_at>=? LIMIT 1",
            (product_id, price, since)
        ).fetchone()
        if already:
            return
        platform = _product_platform(db, product_id)
        decision = evaluate_drop(points, price, first_seen, min_discount_for(platform), platform)
        if not decision.is_deal:
            return
        log.info(f"[webhook] ✔ #{product_id} yo-yo fiyatı {YOYO_CONFIRM_HOURS}+ saat sabit kaldı — fırsat açılıyor")
        await _process_deal(db, product_id, price, None, data, now, decision=decision)
    except Exception as e:
        log.warning(f"[webhook] yo-yo onay hatası #{product_id}: {e}")


async def _save_scraped_data(product_id: int, data: dict):
    """Scraping sonucunu işle: fiyat geçmişi, deal yönetimi, Telegram + email bildirimleri."""
    db = get_db()
    now = now_str()
    stock = data.get("stock")
    price = data.get("price")

    # OOS tespiti — fiyat yoksa bile OOS state'i geçerli, hata değil
    oos = _is_oos(stock)

    if data.get("error") or not price or price <= 0:
        if oos:
            # Stok yok: hata olarak loglanmasın, oos_count mekanizması çalışsın
            _handle_out_of_stock(db, product_id, price or 0, now)
        else:
            db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
            db.commit()
        return

    new_barcode = data.get("barcode")
    had_barcode = True
    if new_barcode:
        prev = db.execute("SELECT barcode FROM products WHERE id=?", (product_id,)).fetchone()
        had_barcode = bool(prev and prev["barcode"])

    _update_product_fields(db, product_id, data, stock, now)

    # Ürüne İLK KEZ barkod geldiyse çapraz aramayı başlat. Çapraz arama yalnızca
    # barkodla eşleştiriyor; toplu eklenen ürünlerde ekleme anındaki arama barkod
    # olmadığı için atlanıyordu (2026-11-01: son 1 saatte 2.092 N11 ürününden 1 eşleşme).
    if new_barcode and not had_barcode:
        db.commit()
        asyncio.create_task(_cross_search_throttled(product_id))

    if oos:
        _handle_out_of_stock(db, product_id, price, now)
        return

    db.execute("UPDATE products SET oos_count=0, oos_last_at=NULL WHERE id=? AND oos_count>0", (product_id,))

    recent_rows = db.execute(
        "SELECT price_value, scraped_at FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 3",
        (product_id,)
    ).fetchall()

    if recent_rows and recent_rows[0]["price_value"] == price:
        _PRICE_SUSPECTS.pop(product_id, None)
        await _maybe_confirm_yoyo_deal(db, product_id, price, recent_rows[0]["scraped_at"], data, now)
        db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
        db.commit()
        return

    # Fiyat doğrulama: 30 günlük medyanın 4 katı dışı → kaydetme, yeniden tara
    if not _check_price_sanity(db, product_id, price, now):
        return

    db.execute(
        "INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
        (product_id, price, "TRY", now)
    )

    if recent_rows:
        _vals = sorted(r["price_value"] for r in recent_rows)
        prev_price_stable = _vals[len(_vals) // 2]
    else:
        prev_price_stable = None

    if prev_price_stable and price < prev_price_stable:
        if _hb_cart_baseline(db, product_id, price, data, now):
            db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
            db.commit()
            return
        should_continue = await _process_deal(db, product_id, price, prev_price_stable, data, now)
        if not should_continue:
            db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
            db.commit()
            return

    _close_expired_deals(db, product_id, price, now)
    db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
    db.commit()


def _auto_delete_if_over_limit(db, product_id: int, platform: str, threshold: int = 100):
    """Ürünün toplam hata sayısı eşiği aşarsa cascade sil."""
    try:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM scraper_errors WHERE product_id=?", (product_id,)
        ).fetchone()
        count = row["cnt"] if row else 0
        if count >= threshold:
            log.warning(f"[webhook] 🗑 Ürün #{product_id} ({platform}) {count} hata — otomatik siliniyor")
            from .admin import _delete_product_cascade
            _delete_product_cascade(db, product_id, "cok_hata")
            return True
    except Exception as ex:
        log.error(f"[webhook] auto-delete kontrol hatası #{product_id}: {ex}")
    return False


def _delete_if_removed(db, product_id: int, platform: str, confirmations: int = 2) -> bool:
    """Scraper ürünün satıştan kaldırıldığını (HTTP yönlendirmesiyle kategori/arama
    sayfasına gidiyor) son 14 günde en az iki kez gördüyse cascade sil. Tek sinyalle
    silinmez — bkz. eski yanlış-pozitif dead_url vakası."""
    try:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM scraper_errors WHERE product_id=? AND error_msg LIKE 'kaldirildi%' "
            "AND occurred_at::timestamp > now() - interval '14 days'", (product_id,)
        ).fetchone()
        if row and row["cnt"] >= confirmations:
            log.warning(f"[webhook] 🗑 Ürün #{product_id} ({platform}) satıştan kaldırılmış ({row['cnt']} teyit) — siliniyor")
            from .admin import _delete_product_cascade
            _delete_product_cascade(db, product_id, "kaldirildi")
            return True
    except Exception as ex:
        log.error(f"[webhook] kaldırıldı kontrol hatası #{product_id}: {ex}")
    return False


async def _save_scraped_data_async(product_id, data, url, platform):
    # dead_url → ürün kalıcı olarak ölmüş, anında sil
    if data.get("error") == "dead_url":
        try:
            db = get_db()
            log.warning(f"[webhook] ☠ Ürün #{product_id} ({platform}) dead_url — anında siliniyor")
            from .admin import _delete_product_cascade
            _delete_product_cascade(db, product_id, "olu_link")
        except Exception as e:
            log.error(f"[webhook-bg] dead_url sil hatası #{product_id}: {e}")
        return

    try:
        await _save_scraped_data(product_id, data)
        # Hata varsa scraper_errors tablosuna kaydet + 100 eşiği kontrolü
        # OOS (stok yok) durumu hata DEĞİL — error field yoksa loglanmaz
        err = data.get("error")
        # Boş sonuç (fiyat da hata da yok — çoğunlukla captcha/blok): eskiden sessizce
        # yutuluyordu; hata olarak kaydet ki izlenebilsin ve scheduler (adım 2) hiç
        # fiyatı alınamamış ürünü öncelikle yeniden denesin.
        if not err and not (data.get("price") and data.get("price") > 0) and not _is_oos(data.get("stock")):
            err = "bos_yanit: fiyat okunamadi"
        if err and not _is_oos(data.get("stock")):
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO scraper_errors(product_id,platform,url,error_msg,occurred_at) VALUES(?,?,?,?,?)",
                    (product_id, platform, url, str(err), now_str())
                )
                db.commit()
                if str(err).startswith("kaldirildi") and _delete_if_removed(db, product_id, platform):
                    return
                _auto_delete_if_over_limit(db, product_id, platform)
            except Exception:
                pass
    except Exception as e:
        log.error(f"[webhook-bg] {product_id} error: {e}")
        try:
            db = get_db()
            db.execute(
                "INSERT INTO scraper_errors(product_id,platform,url,error_msg,occurred_at) VALUES(?,?,?,?,?)",
                (product_id, platform, url, str(e), now_str())
            )
            db.commit()
            _auto_delete_if_over_limit(db, product_id, platform)
        except Exception:
            pass


# ── Endpoint'ler ──────────────────────────────────────────────

@router.get("/api/scan-status")
async def scan_status(request: Request):
    require_admin(request)
    db = get_db()
    queue_pending = db.execute(
        "SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending','processing')"
    ).fetchone()[0]
    return JSONResponse({**_SCAN_STATE, "queue_pending": queue_pending})


@router.get("/api/admin-live")
async def admin_live(request: Request):
    """Admin paneli canlı istatistikler — pending deals, stats."""
    require_admin(request)
    db = get_db()
    pending_count = db.execute(
        "SELECT COUNT(*) FROM deals d JOIN products p ON d.product_id=p.id "
        "WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok') "
        f"AND p.platform NOT IN {SCAN_ONLY_SQL}"
    ).fetchone()[0]
    queue_count = db.execute(
        "SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending','processing')"
    ).fetchone()[0]
    active_deals = db.execute("SELECT COUNT(*) FROM deals WHERE active=1").fetchone()[0]
    products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return JSONResponse({
        "pending_deals": pending_count,
        "queue_pending": queue_count,
        "active_deals": active_deals,
        "products": products,
    })


MAIN_PLATFORMS = ("amazon", "trendyol", "hepsiburada", "n11")    # "Tümünü Durdur" bunları kapsar
SCRAPER_PLATFORMS = MAIN_PLATFORMS + ("karaca", "beymen")
# Günde bir çalışan işler: (platform → settings'teki son çalışma anahtarı)
DAILY_JOB_KEYS = {"karaca": "karaca_discovery_at", "beymen": "beymen_sync_at"}


@router.get("/api/pending-jobs")
async def pending_jobs(request: Request):
    """Local scraper bu endpoint'i polling yaparak bekleyen işleri alır."""
    key = request.headers.get("X-Api-Key", "")
    if SCRAPER_SERVICE_KEY:
        if key != SCRAPER_SERVICE_KEY:
            raise HTTPException(401, "Geçersiz API anahtarı")
    elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(401, "SCRAPER_SERVICE_KEY tanımlı değil")
    try:
        limit = max(1, min(int(request.query_params.get("limit", 10)), 30))
    except (ValueError, TypeError):
        limit = 10
    priority_param = request.query_params.get("priority")
    extra_conds, extra_vals = [], []
    if priority_param is not None:
        try:
            extra_conds.append("sq.priority = ?")
            extra_vals.append(int(priority_param))
        except (ValueError, TypeError):
            pass
    _KNOWN = {"amazon", "trendyol", "hepsiburada", "n11", "karaca"}
    platform_param = request.query_params.get("platform", "")
    if platform_param in _KNOWN:
        extra_conds.append("p.platform = ?")
        extra_vals.append(platform_param)
    db = get_db()
    # Durdurulan platforma iş verilmez: scraper süreci açık kalır, kuyruk boşmuş gibi bekler.
    # Süreci VPS'ten kapatmıyoruz, çünkü kapanan scraper'ı panelden yeniden başlatmak mümkün değil.
    paused = paused_platforms(db)
    if paused:
        extra_conds.append(f"p.platform NOT IN ({','.join('?' * len(paused))})")
        extra_vals.extend(paused)
    extra_sql = "".join(f" AND {c}" for c in extra_conds)
    rows = db.execute(
        f"""SELECT sq.id AS sq_id, sq.product_id, sq.priority, p.source_url, p.platform, p.last_seen_at,
                   p.image_url as cached_image,
                   (CASE WHEN p.title IS NOT NULL AND LENGTH(TRIM(p.title))>3 AND p.image_url IS NOT NULL AND LENGTH(p.image_url)>10 THEN 1 ELSE 0 END) as has_metadata
            FROM scan_queue sq
            JOIN products p ON p.id = sq.product_id
            WHERE sq.status = 'pending'{extra_sql}
            ORDER BY sq.priority ASC, sq.created_at ASC
            LIMIT ?""",
        extra_vals + [limit]
    ).fetchall()
    sq_ids = [r["sq_id"] for r in rows]
    if sq_ids:
        db.execute(
            f"UPDATE scan_queue SET status='processing', updated_at=? WHERE id IN ({','.join('?'*len(sq_ids))})",
            [now_str()] + sq_ids
        )
        db.commit()
    # Mevcut tarama turunun kimliği (scheduler.py yeni tur başlatınca değişir).
    # Scraper bunu izleyip tur başa döndüğünde çerezlerini temizliyor. Gövde
    # yerine başlıkta: eski scraper sürümleri liste formatını bekliyor.
    round_row = db.execute("SELECT value FROM settings WHERE key='scan_round_started_at'").fetchone()
    headers = {"X-Scan-Round": round_row["value"]} if round_row and round_row["value"] else None
    return JSONResponse([{
        "product_id": r["product_id"],
        "url": r["source_url"],
        "platform": r["platform"],
        "priority": r["priority"],
        "last_seen_at": r["last_seen_at"],
        "price_only": r["priority"] == 1 or r["has_metadata"] == 1,
        "cached_image": r["cached_image"] or "",
    } for r in rows], headers=headers)


@router.post("/api/reset-stale-jobs")
async def reset_stale_jobs(request: Request):
    """Processing kalan işleri pending'e döndür. force=true → süre sınırı olmadan tümünü sıfırla."""
    key = request.headers.get("X-Api-Key", "")
    if SCRAPER_SERVICE_KEY:
        if key != SCRAPER_SERVICE_KEY:
            raise HTTPException(401, "Geçersiz API anahtarı")
    elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(401, "SCRAPER_SERVICE_KEY tanımlı değil")
    force = request.query_params.get("force", "false").lower() == "true"
    db = get_db()
    if force:
        cur = db.execute(
            "UPDATE scan_queue SET status='pending', updated_at=? WHERE status IN ('processing','failed')",
            (now_str(),)
        )
    else:
        cur = db.execute("""
            UPDATE scan_queue SET status='pending', updated_at=?
            WHERE status='processing' AND updated_at::timestamp < NOW() - INTERVAL '10 minutes'
        """, (now_str(),))
    db.commit()
    return JSONResponse({"reset": cur.rowcount, "force": force})


@router.post("/api/scraper-webhook")
async def scraper_webhook(payload: WebhookPayload, request: Request, background_tasks: BackgroundTasks):
    """Scraper servisi webhook (timeout 5s, log metrics)."""
    key = request.headers.get("X-Webhook-Key", "")
    if WEBHOOK_KEY and key != WEBHOOK_KEY:
        raise HTTPException(401, "Geçersiz webhook anahtarı")
    start = time.time()
    log.info(f"[webhook] {payload.product_id} {payload.platform} started ({time.time()-start:.1f}s)")
    background_tasks.add_task(_save_scraped_data_async, payload.product_id, payload.data, payload.url, payload.platform)
    log.info(f"[webhook] {payload.product_id} done ({time.time()-start:.1f}s)")
    return {"ok": True, "processed": True}


@router.get("/api/deals")
async def api_deals(platform: str = "", limit: int = Query(default=20, ge=1, le=100)):
    db = get_db()
    w, p = "WHERE d.active=1", []
    if platform:
        w += " AND p.platform=?"
        p.append(platform)
    return [dict(r) for r in db.execute(
        f"SELECT d.id,d.old_price,d.new_price,d.discount_pct,d.created_at,p.title,p.image_url,p.platform,sl.slug"
        f" FROM deals d JOIN products p ON d.product_id=p.id LEFT JOIN short_links sl ON sl.deal_id=d.id"
        f" {w} ORDER BY d.created_at DESC LIMIT ?",
        (*p, limit)
    ).fetchall()]


@router.get("/api/stats")
async def api_stats():
    return dict(get_db().execute(
        "SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals,"
        f" (SELECT COUNT(*) FROM products WHERE platform NOT IN {SCAN_ONLY_SQL}) as products,"
        " (SELECT COUNT(*) FROM clicks) as total_clicks,"
        " (SELECT COUNT(*) FROM product_groups) as comparisons"
    ).fetchone())


@router.get("/api/scraper-monitor")
async def scraper_monitor(request: Request):
    """Scraper sağlık durumu: son tarama zamanı, hata oranı, IP blok tespiti."""
    require_admin(request)
    db = get_db()
    platforms = list(SCRAPER_PLATFORMS)
    daily_jobs = DAILY_JOB_KEYS
    result = {}
    paused = paused_platforms(db)

    for p in platforms:
        # Son başarılı tarama — price_history değişmese bile last_seen_at güncellenir
        last_ok = db.execute(
            "SELECT MAX(pr.last_seen_at) as t FROM products pr WHERE pr.platform=? AND pr.last_seen_at IS NOT NULL",
            (p,)
        ).fetchone()
        last_ok_time = (last_ok["t"] if last_ok else None)

        # Son 30 dakikada hata sayısı
        errors_30m = db.execute(
            "SELECT COUNT(*) as cnt, STRING_AGG(error_msg, '|||') as msgs "
            "FROM scraper_errors WHERE platform=? "
            "AND occurred_at::timestamp >= NOW() - INTERVAL '30 minutes'",
            (p,)
        ).fetchone()

        # CAPTCHA/block özelinde sayı. bos_yanit (fiyat okunamadı) ve kaldirildi
        # hariç: eskiden bos_yanit mesajı "(captcha/blok olabilir)" içerdiği için
        # tükenmiş/kaldırılmış her ürün "blok" sayılıyor, panel yanlışlıkla
        # "IP BLOK" gösteriyordu (2026-10-31).
        block_30m = db.execute(
            "SELECT COUNT(*) as cnt FROM scraper_errors WHERE platform=? "
            "AND occurred_at::timestamp >= NOW() - INTERVAL '30 minutes' "
            "AND error_msg NOT LIKE 'bos_yanit%' AND error_msg NOT LIKE 'kaldirildi%' "
            "AND (LOWER(error_msg) LIKE '%captcha%' OR LOWER(error_msg) LIKE '%block%' "
            "OR LOWER(error_msg) LIKE '%429%' OR LOWER(error_msg) LIKE '%forbidden%')",
            (p,)
        ).fetchone()

        # Kuyruk durumu
        queue = db.execute(
            "SELECT COUNT(*) as cnt FROM scan_queue sq "
            "JOIN products p2 ON p2.id=sq.product_id WHERE p2.platform=? AND sq.status='pending'",
            (p,)
        ).fetchone()

        # Son 10 dakikada kaç ürün başarıyla tarandı (tarama/dk)
        rate_10m = db.execute(
            "SELECT COUNT(*) as cnt FROM products pr WHERE pr.platform=? "
            "AND pr.last_seen_at IS NOT NULL "
            "AND pr.last_seen_at::timestamp >= NOW() - INTERVAL '10 minutes'",
            (p,)
        ).fetchone()

        result[p] = {
            "last_success": last_ok_time,
            "errors_30m": errors_30m["cnt"] if errors_30m else 0,
            "block_errors_30m": block_30m["cnt"] if block_30m else 0,
            "queue_pending": queue["cnt"] if queue else 0,
            "scan_rate_10m": round((rate_10m["cnt"] if rate_10m else 0) / 10, 1),
            "paused": p in paused,
        }

    # Genel alive durumu: herhangi bir platformda son 30 dakikada başarılı tarama var mı
    any_alive = db.execute(
        "SELECT COUNT(*) as cnt FROM price_history WHERE scraped_at::timestamp >= NOW() - INTERVAL '30 minutes'"
    ).fetchone()

    # Son 20 başarılı tarama (aktivite akışı) — platform başına en fazla 5 satır
    # garantilenir (ROW_NUMBER ile), yoksa yüksek hacimli platformlar (ör.
    # hepsiburada) düşük hacimli olanları (ör. trendyol) listeden tamamen
    # eleyebiliyordu; halbuki o platform da sorunsuz taramaya devam ediyor
    # olabilir, sadece fiyatı değişmemiş oluyordu.
    # İç sorgu (ORDER BY id DESC LIMIT 3000) price_history_pkey index'iyle
    # sabit maliyetli kalır — pencere fonksiyonu tüm tabloya (bu panel her
    # 10sn'de bir çağrıldığı için 142K+ satırda 500ms+'a çıkıp disk'e taşan
    # bir sort'a yol açıyordu) değil, en yeni 3000 satıra uygulanır. 3000,
    # en düşük hacimli platformun (trendyol) derinliğinin (~230) kat kat
    # üzerinde güvenli bir marj.
    # 2026-10-31: price_history yerine products.last_seen_at — price_history'e
    # yalnızca fiyat DEĞİŞİNCE satır yazılıyor, fiyatı aynı kalan başarılı
    # taramalar listede hiç görünmüyordu. last_seen_at her başarılı taramada
    # güncellenir; fiyat o ürünün son bilinen fiyatı.
    recent_ok = db.execute("""
        SELECT r.scraped_at, r.title, r.platform, r.product_id,
               (SELECT h.price_value FROM price_history h WHERE h.product_id = r.product_id
                ORDER BY h.id DESC LIMIT 1) AS price_value
        FROM (
            SELECT p.last_seen_at AS scraped_at, p.title, p.platform, p.id AS product_id,
                   ROW_NUMBER() OVER (PARTITION BY p.platform ORDER BY p.last_seen_at DESC) AS rn
            FROM (SELECT id, title, platform, last_seen_at FROM products
                  WHERE last_seen_at IS NOT NULL AND platform <> 'beymen'
                  ORDER BY last_seen_at DESC LIMIT 400) p
        ) r
        WHERE r.rn <= 5
        ORDER BY r.scraped_at DESC LIMIT 20
    """).fetchall()

    # Son 20 hata (aktivite akışı)
    recent_err = db.execute("""
        SELECT se.occurred_at, se.error_msg, se.platform, se.product_id,
               p.title
        FROM scraper_errors se
        LEFT JOIN products p ON p.id = se.product_id
        ORDER BY se.id DESC LIMIT 20
    """).fetchall()

    # Platform başına toplam ürün ve hata sayısı
    total_errors = db.execute("""
        SELECT platform, COUNT(*) as cnt
        FROM scraper_errors
        WHERE occurred_at::timestamp >= NOW() - INTERVAL '1 hour'
        GROUP BY platform
    """).fetchall()
    errors_1h = {r["platform"]: r["cnt"] for r in total_errors}

    # Otomatik silinmeye yakın ürünler (80-99 hata arası)
    near_limit = db.execute("""
        SELECT se.product_id, se.platform, COUNT(*) as cnt, p.title
        FROM scraper_errors se
        LEFT JOIN products p ON p.id = se.product_id
        GROUP BY se.product_id, se.platform, p.title
        HAVING COUNT(*) >= 80
        ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    for p in platforms:
        result[p]["errors_1h"] = errors_1h.get(p, 0)

    import sync_status
    for p, key in daily_jobs.items():
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        last_run = row["value"] if row else None
        next_run = None
        if last_run:
            try:
                next_run = (_to_dt(last_run) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        result[p]["daily_sync"] = {
            "last_run": last_run,
            "next_run": next_run,
            "products": db.execute("SELECT COUNT(*) FROM products WHERE platform=?", (p,)).fetchone()[0],
            # Beymen tamamen eşitlemeyle güncellenir (Windows scraper'ı yok); Karaca'nın
            # fiyatları Windows scraper'ından, eşitleme sadece yeni ürün keşfi
            "mode": "full" if p == "beymen" else "discovery",
            "status": sync_status.get_status(p),
        }

    result["_meta"] = {
        "alive": (any_alive["cnt"] > 0) if any_alive else False,
        "checked_at": now_str(),
        "recent_ok": [
            {
                "time": r["scraped_at"],
                "price": r["price_value"],
                "title": (r["title"] or "")[:40],
                "platform": r["platform"],
                "product_id": r["product_id"],
            }
            for r in recent_ok
        ],
        "recent_err": [
            {
                "time": r["occurred_at"],
                "msg": (r["error_msg"] or "")[:60],
                "platform": r["platform"],
                "product_id": r["product_id"],
                "title": (r["title"] or "")[:35],
            }
            for r in recent_err
        ],
        "near_limit": [
            {
                "product_id": r["product_id"],
                "platform": r["platform"],
                "count": r["cnt"],
                "title": (r["title"] or "")[:40],
            }
            for r in near_limit
        ],
    }
    return JSONResponse(result)


@router.get("/health")
async def health():
    try:
        get_db().execute("SELECT 1").fetchone()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    return JSONResponse({"status": "ok" if db_status == "ok" else "degraded", "db": db_status})
