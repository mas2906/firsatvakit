"""Scraper API: pending-jobs, webhook, dispatch mantığı, public API, health."""
import json
import logging
import os
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from affiliate import make_short_slug
from db import get_db
from email_utils import send_price_alert
from telegram_pub import notify_pending_approval, notify_approved_deal_update

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


def _smart_price_analysis(db, product_id: int, price: float, prev_price: float):
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
        " variants=COALESCE(?,variants),"
        " ai_review_summary=COALESCE(?,ai_review_summary),"
        " last_seen_at=? WHERE id=?",
        (data.get("title"), data.get("image_url"), data.get("description"),
         data.get("rating"), data.get("review_count"), data.get("brand"),
         data.get("barcode"), stock, variants_json, ai_summary, now, product_id)
    )


_OOS_KEYWORDS = ["mevcut değil", "tükendi", "stokta yok", "out of stock", "sold out", "unavailable"]


def _is_oos(stock: str) -> bool:
    if not stock:
        return False
    return stock == "Stok Yok" or any(kw in stock.lower() for kw in _OOS_KEYWORDS)


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

    row = db.execute("SELECT oos_count FROM products WHERE id=?", (product_id,)).fetchone()
    oos = (row["oos_count"] or 0) + 1 if row else 1
    if oos >= 3:
        log.info(f"[webhook] 🗑 Ürün #{product_id} üst üste 3 kez stok yok — siliniyor")
        from .admin import _delete_product_cascade
        _delete_product_cascade(db, product_id)
    else:
        db.execute("UPDATE products SET oos_count=? WHERE id=?", (oos, product_id))
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


async def _process_deal(db, product_id: int, price: float, prev_price_stable: float,
                        data: dict, now: str) -> bool:
    """Deal oluşturma/güncelleme mantığı. False döndürürse caller erken çıkmalı."""
    is_deal, old_price, reason = _smart_price_analysis(db, product_id, price, prev_price_stable)
    if not is_deal:
        log.info(f"[webhook] ⏭ #{product_id} deal degil: {reason}")
        return False

    pct = (old_price - price) / old_price * 100
    stock = data.get("stock", "")

    if pct > 90:
        log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim şüpheli (>90), deal oluşturulmadı")
        return True
    if stock != "Stokta Var" and pct > 75:
        log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim + stok={stock!r} şüpheli, atlandı")
        return True
    if pct < 5:
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
            "UPDATE deals SET old_price=?,new_price=?,discount_pct=?,created_at=? WHERE id=?",
            (old_price, price, round(pct, 1), now, deal_id)
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

    _update_product_fields(db, product_id, data, stock, now)

    if oos:
        _handle_out_of_stock(db, product_id, price, now)
        return

    db.execute("UPDATE products SET oos_count=0 WHERE id=? AND oos_count>0", (product_id,))

    recent_rows = db.execute(
        "SELECT price_value FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 3",
        (product_id,)
    ).fetchall()

    if recent_rows and recent_rows[0]["price_value"] == price:
        db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
        db.commit()
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
            _delete_product_cascade(db, product_id)
            return True
    except Exception as ex:
        log.error(f"[webhook] auto-delete kontrol hatası #{product_id}: {ex}")
    return False


async def _save_scraped_data_async(product_id, data, url, platform):
    # dead_url → ürün kalıcı olarak ölmüş, anında sil
    if data.get("error") == "dead_url":
        try:
            db = get_db()
            log.warning(f"[webhook] ☠ Ürün #{product_id} ({platform}) dead_url — anında siliniyor")
            from .admin import _delete_product_cascade
            _delete_product_cascade(db, product_id)
        except Exception as e:
            log.error(f"[webhook-bg] dead_url sil hatası #{product_id}: {e}")
        return

    try:
        await _save_scraped_data(product_id, data)
        # Hata varsa scraper_errors tablosuna kaydet + 100 eşiği kontrolü
        # OOS (stok yok) durumu hata DEĞİL — error field yoksa loglanmaz
        err = data.get("error")
        if err and not _is_oos(data.get("stock")):
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO scraper_errors(product_id,platform,url,error_msg,occurred_at) VALUES(?,?,?,?,?)",
                    (product_id, platform, url, str(err), now_str())
                )
                db.commit()
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
        "WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok')"
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
    _KNOWN = {"amazon", "trendyol", "hepsiburada", "n11"}
    platform_param = request.query_params.get("platform", "")
    if platform_param in _KNOWN:
        extra_conds.append("p.platform = ?")
        extra_vals.append(platform_param)
    extra_sql = "".join(f" AND {c}" for c in extra_conds)
    db = get_db()
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
    return JSONResponse([{
        "product_id": r["product_id"],
        "url": r["source_url"],
        "platform": r["platform"],
        "priority": r["priority"],
        "last_seen_at": r["last_seen_at"],
        "price_only": r["priority"] == 1 or r["has_metadata"] == 1,
        "cached_image": r["cached_image"] or "",
    } for r in rows])


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
        " (SELECT COUNT(*) FROM products) as products,"
        " (SELECT COUNT(*) FROM clicks) as total_clicks,"
        " (SELECT COUNT(*) FROM product_groups) as comparisons"
    ).fetchone())


@router.get("/api/scraper-monitor")
async def scraper_monitor(request: Request):
    """Scraper sağlık durumu: son tarama zamanı, hata oranı, IP blok tespiti."""
    require_admin(request)
    db = get_db()
    platforms = ["amazon", "trendyol", "hepsiburada", "n11"]
    result = {}

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

        # CAPTCHA/block özelinde sayı
        block_30m = db.execute(
            "SELECT COUNT(*) as cnt FROM scraper_errors WHERE platform=? "
            "AND occurred_at::timestamp >= NOW() - INTERVAL '30 minutes' "
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
        }

    # Genel alive durumu: herhangi bir platformda son 30 dakikada başarılı tarama var mı
    any_alive = db.execute(
        "SELECT COUNT(*) as cnt FROM price_history WHERE scraped_at::timestamp >= NOW() - INTERVAL '30 minutes'"
    ).fetchone()

    # Son 20 başarılı tarama (aktivite akışı)
    recent_ok = db.execute("""
        SELECT ph.scraped_at, ph.price_value, p.title, p.platform, p.id as product_id
        FROM price_history ph
        JOIN products p ON p.id = ph.product_id
        ORDER BY ph.id DESC LIMIT 20
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
