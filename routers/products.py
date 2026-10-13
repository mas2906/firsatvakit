"""Ürün & deal sayfaları: ana sayfa, arama, submit, detay, watchlist, karşılaştırma, kısa link."""
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from affiliate import build_affiliate_url, make_short_slug
from comments import get_product_comments
from cross_search import get_price_comparison
from db import get_db
from price_rules import find_pump_events
from hepsiburada_link import is_hb_short_url, normalize_hb_url, resolve_hb_short_url
from scraper_router import (clean_tracking_params, detect_platform, enqueue_url,
                             extract_product_id, extract_url_from_text, is_short_url,
                             is_valid_product_url, normalize_product_url, resolve_short_url)
from seo_content import build_meta_tags, generate_comparison_data

from .deps import (
    _GROUP_CHEAPEST_FILTER, _cache_get, _cache_set, _is_rate_limited, _verify_csrf,
    current_user, get_client_ip, get_ticker_items, now_str, templates,
)

router = APIRouter()

GUEST_DAILY_LIMIT = 3
MEMBER_DAILY_LIMIT = 20


def get_today_submit_count(db, user_id=None, ip=None):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if user_id:
        return db.execute(
            "SELECT COUNT(*) FROM submit_log WHERE user_id=? AND DATE(submitted_at)=?",
            (user_id, today)
        ).fetchone()[0]
    return db.execute(
        "SELECT COUNT(*) FROM submit_log WHERE user_id IS NULL AND ip=? AND DATE(submitted_at)=?",
        (ip, today)
    ).fetchone()[0]


def log_submit(db, user_id=None, ip=None, product_id=None):
    db.execute(
        "INSERT INTO submit_log(user_id,ip,product_id,submitted_at) VALUES(?,?,?,?)",
        (user_id, ip, product_id, now_str())
    )
    db.commit()


def _dedup_history(db, product_id):
    raw = db.execute(
        "SELECT price_value,scraped_at FROM price_history WHERE product_id=? ORDER BY scraped_at ASC",
        (product_id,)
    ).fetchall()
    out, last = [], None
    for r in raw:
        if r["price_value"] != last:
            out.append(r)
            last = r["price_value"]
    return out


def _pump_warning(history) -> dict | None:
    """Son 90 günde "önce zam, sonra indirim" örüntüsü varsa en yenisini döndürür."""
    try:
        events = find_pump_events([(r["scraped_at"], r["price_value"]) for r in history])
    except Exception:
        return None
    cutoff = datetime.utcnow() - timedelta(days=90)
    recent = [e for e in events if e.date >= cutoff]
    if not recent:
        return None
    e = recent[-1]
    return {"date": e.date.strftime("%d.%m.%Y"), "before": e.before, "raised": e.raised,
            "discounted": e.discounted, "days": e.days_high, "count": len(recent)}


def _refresh_stale_comparison(db, products) -> None:
    """Karşılaştırmadaki bayat fiyatları arka planda hemen yeniler.
    Aktif fırsatı olan ürünler için 5 dk, diğerleri için 2 saat eşik kullanılır.
    priority=-1 (express) ile kuyruğa eklenir — rotasyon sırasını beklemeden,
    normal iş akışının önüne geçerek taranır (bkz. scrapers/utils.py rotasyonu).
    Zaten kuyrukta düşük öncelikle bekleyen bir iş varsa onu da öne çeker."""
    threshold_active = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    threshold_normal = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    threshold_karaca = (datetime.utcnow() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    for p in products:
        if p.get("platform") == "beymen":
            continue  # Windows scraper taramaz; fiyat beymen_sync.py ile günde bir güncellenir
        has_active_deal = db.execute(
            "SELECT 1 FROM deals WHERE product_id=? AND active=1 LIMIT 1", (p["id"],)
        ).fetchone()
        threshold = threshold_active if has_active_deal else threshold_normal
        if p.get("platform") == "karaca":
            threshold = threshold_karaca  # günde iki tarama kuralı (scheduler 4a)
        last = p.get("last_seen_at") or ""
        if last >= threshold:
            continue
        existing = db.execute(
            "SELECT id, priority FROM scan_queue WHERE product_id=? AND status IN ('pending','processing')",
            (p["id"],)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) VALUES(?,?,?,'pending',-1,?)",
                (p["id"], p["source_url"], p["platform"], now_str())
            )
            changed = True
        elif existing["priority"] > -1:
            db.execute("UPDATE scan_queue SET priority=-1 WHERE id=?", (existing["id"],))
            changed = True
    if changed:
        db.commit()


_DIPTE_SQL = """
WITH ph AS (
  SELECT product_id, price_value, scraped_at,
         ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY scraped_at DESC, id DESC) AS rn
  FROM price_history WHERE price_value > 0
), agg AS (
  SELECT product_id, MIN(price_value) AS mn, MAX(price_value) AS mx,
         PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_value) AS med,
         MIN(scraped_at) AS first_at, COUNT(*) AS n
  FROM ph GROUP BY product_id
)
SELECT p.id, p.platform, p.title, p.image_url, cur.price_value AS cur, a.mn, a.med, a.mx,
       (a.med - cur.price_value) / a.med * 100 AS pct, cur.scraped_at AS since
FROM agg a
JOIN ph cur ON cur.product_id = a.product_id AND cur.rn = 1
JOIN products p ON p.id = a.product_id
WHERE a.first_at <= ?
  AND a.n >= 4
  AND cur.price_value <= a.mn * 1.005
  AND cur.price_value <= a.med * 0.85
  AND cur.price_value >= a.med * 0.25
  AND (p.stock IS NULL OR p.stock <> 'Stok Yok')
  AND p.title IS NOT NULL AND p.title <> ''
ORDER BY (a.med - cur.price_value) / a.med DESC
LIMIT 60
"""


def _dipte_items() -> list:
    """Tüm zamanların en düşüğünde + medyanın %15+ altında (30+ gün takipli) ürünler.
    Alt sınır (medyanın 1/4'ü) hatalı okumaları dışarıda tutar."""
    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(r) for r in get_db().execute(_DIPTE_SQL, (cutoff,)).fetchall()]


_dipte_cache = {"items": None, "ts": 0.0}


async def get_dipte_items() -> list:
    """/dipte sayfası ve ana sayfa şeridi için ortak, 15 dk önbellekli liste
    (~1 sn'lik sorgu — tüm fiyat geçmişi)."""
    import asyncio
    import time
    if _dipte_cache["items"] is None or time.time() - _dipte_cache["ts"] > 900:
        _dipte_cache["items"] = await asyncio.to_thread(_dipte_items)
        _dipte_cache["ts"] = time.time()
    return _dipte_cache["items"]


@router.get("/dipte")
async def dipte(request: Request):
    items = await get_dipte_items()
    meta = {"title": f"Şu An En Düşük Fiyatında Olan {len(items)} Ürün — Gerçek Fırsatlar | FırsatVakti",
            "description": ("Fiyat geçmişine göre şu anda tüm zamanların en düşük fiyatında olan ürünler: "
                            "Amazon, Trendyol, Hepsiburada ve N11'de medyan fiyatının en az %15 altındaki gerçek indirimler.")}
    return templates.TemplateResponse("dipte.html", {
        "request": request, "user": current_user(request), "items": items, "meta": meta,
    })


@router.get("/deals")
async def deals_redirect(request: Request):
    qs = request.url.query
    return RedirectResponse(url=f"/?{qs}" if qs else "/", status_code=301)


@router.get("/")
async def index(request: Request, platform: str = "", sort: str = "newest",
                page: int = 1, tab: str = "all"):
    db = get_db()
    per_page = 24

    # Sayfa verisi cache'i — aynı filtre kombinasyonu 60s içinde DB'ye gitmez
    page_key = f"index_page:{platform}:{sort}:{tab}:{page}"
    page_data = _cache_get(page_key)

    if page_data is None:
        offset = (page - 1) * per_page
        where, params = "WHERE d.active=1", []
        if platform:
            where += " AND p.platform=?"
            params.append(platform)

        if tab == "top_discount":
            order = "d.discount_pct DESC"
        elif tab == "most_clicked":
            order = "click_count DESC"
        elif tab == "cheapest":
            order = "d.new_price ASC"
        elif tab == "today":
            order = "d.created_at DESC"
            where += " AND DATE(d.created_at)=DATE('now')"
        else:
            order = {"newest": "d.created_at DESC", "discount": "d.discount_pct DESC",
                     "price": "d.new_price ASC"}.get(sort, "d.created_at DESC")

        where += _GROUP_CHEAPEST_FILTER

        deals = db.execute(f"""
            SELECT d.*, p.title, p.image_url, p.rating, p.review_count, p.platform, p.stock, p.last_seen_at,
                   (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count,
                   (SELECT pgm.group_id FROM product_group_members pgm WHERE pgm.product_id=d.product_id LIMIT 1) as compare_group_id,
                   (SELECT p2.platform FROM products p2
                    JOIN product_group_members pgm2 ON p2.id=pgm2.product_id
                    JOIN (SELECT product_id, price_value FROM price_history WHERE id IN (SELECT MAX(id) FROM price_history GROUP BY product_id)) lp ON lp.product_id=p2.id
                    WHERE pgm2.group_id=(SELECT pgm3.group_id FROM product_group_members pgm3 WHERE pgm3.product_id=d.product_id LIMIT 1)
                    AND (p2.stock IS NULL OR p2.stock != 'Stok Yok')
                    ORDER BY lp.price_value ASC LIMIT 1) as best_compare_platform,
                   (SELECT MIN(lp.price_value) FROM product_group_members pgm2
                    JOIN products p2x ON p2x.id=pgm2.product_id
                    JOIN (SELECT product_id, price_value FROM price_history WHERE id IN (SELECT MAX(id) FROM price_history GROUP BY product_id)) lp ON lp.product_id=pgm2.product_id
                    WHERE pgm2.group_id=(SELECT pgm3.group_id FROM product_group_members pgm3 WHERE pgm3.product_id=d.product_id LIMIT 1)
                    AND (p2x.stock IS NULL OR p2x.stock != 'Stok Yok')) as best_compare_price
            FROM deals d LEFT JOIN products p ON d.product_id = p.id
            INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id = best.max_id
            {where.replace("WHERE","AND")} ORDER BY {order} LIMIT ? OFFSET ?
        """, (*params, per_page, offset)).fetchall()

        total = db.execute(
            f"SELECT COUNT(DISTINCT d.product_id) FROM deals d LEFT JOIN products p ON d.product_id=p.id {where}",
            params
        ).fetchone()[0]

        page_data = {
            "deals": [dict(r) for r in deals],
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
        _cache_set(page_key, page_data)

    # Paylaşılan veriler (stats, ticker, yeni/popüler deallar) — ayrı cache
    shared = _cache_get("index_shared")
    if shared is None:
        stats = db.execute(
            "SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals,"
            " (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops,"
            " (SELECT COUNT(*) FROM product_watchlist) as watchlist_count,"
            " (SELECT COUNT(*) FROM product_groups) as tracked_products_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='karaca') as karaca_count,"
            " (SELECT COUNT(*) FROM products WHERE platform='beymen') as beymen_count"
        ).fetchone()
        new_deals = db.execute(
            "SELECT d.*, p.title, p.image_url, p.rating, p.platform, p.stock, p.last_seen_at,"
            " (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count"
            " FROM deals d LEFT JOIN products p ON d.product_id=p.id"
            " INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id"
            " WHERE d.active=1" + _GROUP_CHEAPEST_FILTER + " ORDER BY d.created_at DESC LIMIT 8"
        ).fetchall()
        popular_deals = db.execute(
            "SELECT d.*, p.title, p.image_url, p.rating, p.platform, p.stock, p.last_seen_at,"
            " (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count"
            " FROM deals d LEFT JOIN products p ON d.product_id=p.id"
            " INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id"
            " WHERE d.active=1" + _GROUP_CHEAPEST_FILTER + " ORDER BY click_count DESC LIMIT 8"
        ).fetchall()
        ticker = get_ticker_items(db)
        shared = {
            "stats": dict(stats) if stats else {},
            "new_deals": [dict(r) for r in new_deals],
            "popular_deals": [dict(r) for r in popular_deals],
            "ticker_items": [dict(r) for r in ticker],
        }
        _cache_set("index_shared", shared)

    user = current_user(request)
    user_min_discount = 5
    if user:
        from routers.misc import ensure_notify_table
        ensure_notify_table(db)
        ns = db.execute("SELECT min_discount FROM notify_settings WHERE user_id=?", (user["id"],)).fetchone()
        if ns:
            user_min_discount = ns["min_discount"]

    meta = build_meta_tags("index", {"platform": platform, "page": page})
    # Sıralama/sekme varyantları aynı içeriğin kopyası → platform + sayfa dışındaki
    # parametreleri canonical'dan at
    qs = "&".join(p for p in (f"platform={platform}" if platform else "",
                              f"page={page}" if page > 1 else "") if p)
    meta["canonical"] = "/" + (f"?{qs}" if qs else "")
    if tab != "all" or sort != "newest":
        meta["robots"] = "noindex, follow"
    return templates.TemplateResponse("index.html", {
        "request": request, "deals": page_data["deals"], "platform": platform, "sort": sort,
        "tab": tab, "page": page, "total_pages": page_data["total_pages"],
        "stats": shared["stats"], "user": user, "q": "",
        "ticker_items": shared["ticker_items"], "meta": meta,
        "user_min_discount": user_min_discount,
        "new_deals": shared["new_deals"], "popular_deals": shared["popular_deals"],
        # "Şu An Dipte" şeridi (mobilde de görünür) — platform filtresi seçiliyse ona göre
        "dipte_items": [d for d in await get_dipte_items() if not platform or d["platform"] == platform][:12],
    })


@router.get("/search")
async def search(request: Request, q: str = "", page: int = 1):
    db = get_db()
    deals, total_pages, per_page = [], 1, 24
    if q.strip():
        kw = f"%{q.strip()}%"
        offset = (page - 1) * per_page
        deals = db.execute("""
            SELECT d.*, p.title, p.image_url, p.rating, p.review_count, p.platform, p.stock
            FROM deals d LEFT JOIN products p ON d.product_id=p.id
            INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id
            WHERE d.active=1 AND (p.title LIKE ? OR p.platform LIKE ?) ORDER BY d.discount_pct DESC LIMIT ? OFFSET ?
        """, (kw, kw, per_page, offset)).fetchall()
        total = db.execute(
            "SELECT COUNT(DISTINCT d.product_id) FROM deals d LEFT JOIN products p ON d.product_id=p.id"
            " WHERE d.active=1 AND (p.title LIKE ? OR p.platform LIKE ?)", (kw, kw)
        ).fetchone()[0]
        total_pages = max(1, (total + per_page - 1) // per_page)
    stats = db.execute(
        "SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals,"
        " (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops,"
        " (SELECT COUNT(*) FROM product_watchlist) as watchlist_count,"
        " (SELECT COUNT(*) FROM product_groups) as tracked_products_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='karaca') as karaca_count,"
        " (SELECT COUNT(*) FROM products WHERE platform='beymen') as beymen_count"
    ).fetchone()
    return templates.TemplateResponse("index.html", {
        "request": request, "deals": deals, "platform": "", "sort": "discount",
        "page": page, "total_pages": total_pages, "stats": stats,
        "user": current_user(request), "q": q, "ticker_items": get_ticker_items(db),
        # Arama sonuç sayfaları ince/tekrarlı içerik — indekslenmesin, linkler takip edilsin
        "meta": {"title": f"“{q.strip()[:60]}” İndirimleri ve Fiyatları | FırsatVakti" if q.strip() else "Ara | FırsatVakti",
                 "robots": "noindex, follow"},
    })


@router.get("/submit")
async def submit_get(request: Request):
    user = current_user(request)
    db = get_db()
    ip = get_client_ip(request)
    if user and user["role"] == "admin":
        count, limit = 0, 999999
    elif user:
        count = get_today_submit_count(db, user_id=user["id"])
        limit = MEMBER_DAILY_LIMIT
    else:
        count = get_today_submit_count(db, ip=ip)
        limit = GUEST_DAILY_LIMIT
    return templates.TemplateResponse("submit.html", {
        "request": request, "user": user, "remaining": max(0, limit - count), "limit": limit,
    })


@router.post("/submit")
async def submit_post(request: Request, background_tasks: BackgroundTasks,
                      url: str = Form(...), cross_search_platforms: str = Form(""),
                      csrf_token: str = Form("")):
    from .api import _dispatch_scrape, _run_cross_search
    user = current_user(request)
    db = get_db()
    ip = get_client_ip(request)
    if _is_rate_limited(ip, "submit"):
        return templates.TemplateResponse("submit.html", {"request": request, "user": user,
                                                          "error": "Çok fazla istek gönderildi. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("submit.html", {"request": request, "user": user,
                                                          "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    if user and user["role"] == "admin":
        count, limit = 0, 999999
    elif user:
        count = get_today_submit_count(db, user_id=user["id"])
        limit = MEMBER_DAILY_LIMIT
    else:
        count = get_today_submit_count(db, ip=ip)
        limit = GUEST_DAILY_LIMIT
    remaining = max(0, limit - count)

    # Mobil uygulamaların "Paylaş" metni: "Bu ürünü Trendyol'da gördüm https://ty.gl/abc"
    raw_input = url
    url = extract_url_from_text(url)
    if not url:
        return templates.TemplateResponse("submit.html", {"request": request,
                                                          "error": "Geçerli bir ürün linki bulunamadı.", "user": user,
                                                          "remaining": remaining, "limit": limit})

    platform = detect_platform(url)
    # Hepsiburada kısa linki (app.hb.biz) — Akamai korumalı, ayrı modülde çözülüyor;
    # liste/kampanya/süresi dolmuş linkler burada anlamlı bir mesajla elenir
    if is_hb_short_url(url):
        hb = await resolve_hb_short_url(url)
        if not hb.ok:
            print(f"[submit] HB kısa link reddedildi: {url} → {hb.error}")
            return templates.TemplateResponse("submit.html", {"request": request, "error": hb.error,
                                                              "user": user, "remaining": remaining, "limit": limit})
        url, platform = hb.url, "hepsiburada"
    # Diğer kısa linkler (ty.gl, a.co, amzn.eu) ya da bilinmeyen bir yönlendirici
    # (bit.ly vb.) → gerçek ürün URL'sine çöz
    elif is_short_url(url) or not platform:
        url = await resolve_short_url(url)
        platform = detect_platform(url)
    if not platform:
        return templates.TemplateResponse("submit.html", {"request": request,
                                                          "error": "Desteklenmeyen site. Sadece Amazon, Trendyol, Hepsiburada ve N11 ürün linkleri eklenebilir.",
                                                          "user": user, "remaining": remaining, "limit": limit})

    url = normalize_product_url(url, platform)
    if platform == "hepsiburada":
        url = normalize_hb_url(url)
    if not is_valid_product_url(url, platform):
        print(f"[submit] geçersiz ürün URL'si: girdi={raw_input[:200]!r} çözülen={url[:200]!r}")
        return templates.TemplateResponse("submit.html", {"request": request,
                                                          "error": "Bu bir ürün sayfası linki değil (kategori/arama sayfası olabilir). Ürün sayfasını açıp linkini paylaş.",
                                                          "user": user, "remaining": remaining, "limit": limit})

    clean_url = clean_tracking_params(url, platform)
    existing = db.execute("SELECT id FROM products WHERE source_url=?", (clean_url,)).fetchone()
    if not existing:
        product_id_str = extract_product_id(url, platform)
        if product_id_str:
            existing = db.execute(
                "SELECT id FROM products WHERE platform=? AND asin_or_id=?",
                (platform, product_id_str)
            ).fetchone()
    if existing:
        product_id_existing = existing["id"]
        is_watching = False
        if user:
            is_watching = bool(db.execute(
                "SELECT 1 FROM product_watchlist WHERE user_id=? AND product_id=?",
                (user["id"], product_id_existing)
            ).fetchone())
        return templates.TemplateResponse("submit.html", {
            "request": request, "user": user,
            "remaining": remaining, "limit": limit,
            "already_exists": True,
            "already_exists_url": f"/product/{product_id_existing}",
            "already_exists_product_id": product_id_existing,
            "is_watching": is_watching,
        })

    if count >= limit:
        return templates.TemplateResponse("submit.html", {"request": request, "user": user,
                                                          "remaining": 0, "limit": limit, "limit_reached": True,
                                                          "error": f"Günlük limit ({limit}). Yarın tekrar dene!" if user else f"Misafir limiti ({limit}). Üye ol!"})

    if user and user["role"] == "admin":
        try:
            product_id = enqueue_url(db, url, platform)
        except ValueError as e:
            return templates.TemplateResponse("submit.html", {"request": request, "user": user,
                                                              "error": str(e), "remaining": remaining, "limit": limit})
        if cross_search_platforms:
            plats = [p.strip() for p in cross_search_platforms.split(",") if p.strip()]
        else:
            plats = [p for p in ["amazon", "trendyol", "hepsiburada", "n11"] if p != platform]
        await _dispatch_scrape(product_id, url, platform, background_tasks, cross_search_plats=plats)
        log_submit(db, user_id=user["id"], ip=ip, product_id=product_id)
    else:
        db.execute(
            "INSERT INTO link_queue(url, platform, user_id, ip, status, submitted_at) VALUES(?,?,?,?,'pending',?)",
            (clean_url, platform, user["id"] if user else None, ip, now_str())
        )
        db.commit()
        log_submit(db, user_id=user["id"] if user else None, ip=ip)

    if user and user["role"] == "admin":
        return templates.TemplateResponse("submit.html", {
            "request": request, "success": f"Link eklendi! {platform.title()} taranıyor...",
            "product_id": product_id, "product_url": f"/product/{product_id}",
            "user": user, "remaining": max(0, limit - count - 1), "limit": limit,
        })
    return templates.TemplateResponse("submit.html", {
        "request": request,
        "success": f"Linkin alındı! Admin onayından sonra {platform.title()} taranacak.",
        "user": user, "remaining": max(0, limit - count - 1), "limit": limit,
    })


@router.get("/product/{product_id}")
async def product_detail(request: Request, product_id: int):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        raise HTTPException(404, "Ürün bulunamadı")
    # Sadece /deal/{id} sayfasının gerçekten göstereceği (approved/expired)
    # deal'lar için yönlendir — aksi halde pending/rejected bir deal'ı olan
    # ürünler /deal/{id}'de 404 alıp ürün sayfasına hiç ulaşamıyordu.
    deal = db.execute(
        "SELECT id FROM deals WHERE product_id=? AND status IN ('approved','expired') "
        "ORDER BY id DESC LIMIT 1", (product_id,)
    ).fetchone()
    if deal:
        return RedirectResponse(f"/deal/{deal['id']}", status_code=302)
    history = _dedup_history(db, product_id)
    comparison = get_price_comparison(db, product_id)
    _refresh_stale_comparison(db, comparison.get("products", []))
    comments_data = get_product_comments(db, product_id)
    meta = build_meta_tags("product", dict(product))
    user = current_user(request)
    is_watching = bool(user and db.execute(
        "SELECT 1 FROM product_watchlist WHERE user_id=? AND product_id=?",
        (user["id"], product_id)
    ).fetchone())
    watcher_count = db.execute(
        "SELECT COUNT(*) FROM product_watchlist WHERE product_id=?", (product_id,)
    ).fetchone()[0]
    return templates.TemplateResponse("product.html", {
        "request": request, "product": product, "history": history,
        "user": user, "comparison": comparison,
        "comments_data": comments_data, "meta": meta, "pump_warning": _pump_warning(history),
        "is_watching": is_watching, "watcher_count": watcher_count,
    })


@router.post("/watch/{product_id}")
async def watch_product(request: Request, product_id: int):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yapmalısın")
    db = get_db()
    product = db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        raise HTTPException(404, "Ürün bulunamadı")
    try:
        db.execute(
            "INSERT OR IGNORE INTO product_watchlist(user_id, product_id, created_at) VALUES(?,?,?)",
            (user["id"], product_id, now_str())
        )
        db.commit()
    except Exception:
        pass
    return JSONResponse({"watching": True})


@router.post("/unwatch/{product_id}")
async def unwatch_product(request: Request, product_id: int):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yapmalısın")
    db = get_db()
    db.execute(
        "DELETE FROM product_watchlist WHERE user_id=? AND product_id=?",
        (user["id"], product_id)
    )
    db.commit()
    return JSONResponse({"watching": False})


@router.get("/watchlist")
async def watchlist_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth", status_code=302)
    db = get_db()
    items = db.execute("""
        SELECT p.*,
               d.id as deal_id, d.new_price, d.old_price, d.discount_pct, d.active as deal_active,
               sl.slug,
               ph.price_value as current_price,
               pw.created_at as watched_at
        FROM product_watchlist pw
        JOIN products p ON pw.product_id = p.id
        LEFT JOIN (
            SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id
        ) best ON best.product_id = p.id
        LEFT JOIN deals d ON d.id = best.max_id
        LEFT JOIN short_links sl ON sl.deal_id = d.id
        LEFT JOIN (
            SELECT product_id, price_value FROM price_history
            WHERE id IN (SELECT MAX(id) FROM price_history GROUP BY product_id)
        ) ph ON ph.product_id = p.id
        WHERE pw.user_id = ?
        ORDER BY pw.created_at DESC
    """, (user["id"],)).fetchall()
    return templates.TemplateResponse("watchlist.html", {
        "request": request, "user": user, "items": items,
    })


@router.get("/deal/{deal_id}")
async def deal_detail(request: Request, deal_id: int):
    db = get_db()
    deal = db.execute("""
        SELECT d.*, p.title, p.image_url, p.description, p.rating,
               p.review_count, p.platform, p.source_url, p.asin_or_id, p.stock
        FROM deals d JOIN products p ON d.product_id=p.id
        WHERE d.id=? AND d.status IN ('approved', 'expired')
    """, (deal_id,)).fetchone()
    if not deal:
        raise HTTPException(404, "Fırsat bulunamadı")
    history = _dedup_history(db, deal["product_id"])
    short = db.execute("SELECT slug FROM short_links WHERE deal_id=?", (deal_id,)).fetchone()
    short_url = f"https://firsatvakti.com/go/{short['slug']}" if short else None
    clicks = db.execute("SELECT COUNT(*) FROM clicks WHERE deal_id=?", (deal_id,)).fetchone()[0]
    comparison = get_price_comparison(db, deal["product_id"])
    _refresh_stale_comparison(db, comparison.get("products", []))
    comments_data = get_product_comments(db, deal["product_id"])
    meta = build_meta_tags("deal", dict(deal))
    # Aynı ürünün eski deal sayfaları kopya içerik — canonical en yeni deal'a
    latest = db.execute(
        "SELECT MAX(id) FROM deals WHERE product_id=? AND status IN ('approved','expired')",
        (deal["product_id"],)
    ).fetchone()
    meta["canonical"] = f"/deal/{(latest[0] if latest and latest[0] else deal_id)}"
    user = current_user(request)
    is_watching = bool(user and db.execute(
        "SELECT 1 FROM product_watchlist WHERE user_id=? AND product_id=?",
        (user["id"], deal["product_id"])
    ).fetchone())
    watcher_count = db.execute(
        "SELECT COUNT(*) FROM product_watchlist WHERE product_id=?", (deal["product_id"],)
    ).fetchone()[0]
    return templates.TemplateResponse("deal.html", {
        "request": request, "deal": deal, "history": history,
        "short_url": short_url, "clicks": clicks, "user": user,
        "comparison": comparison, "comments_data": comments_data, "meta": meta,
        "pump_warning": _pump_warning(history),
        "is_watching": is_watching, "watcher_count": watcher_count,
    })


@router.get("/compare/{group_id}")
async def compare_page(request: Request, group_id: int):
    db = get_db()
    data = generate_comparison_data(db, group_id)
    if not data:
        raise HTTPException(404, "Karşılaştırma bulunamadı")
    _refresh_stale_comparison(db, data.get("products", []))
    meta = build_meta_tags("comparison", {"name": data["group"]["name"],
                                          "platform_count": data["platform_count"],
                                          "image_url": data["group"].get("image_url")})
    return templates.TemplateResponse("compare.html", {
        "request": request, "user": current_user(request), "comparison": data, "meta": meta,
    })


@router.get("/go/{slug}")
async def short_redirect(slug: str, request: Request):
    db = get_db()
    row = db.execute("""
        SELECT sl.*, d.product_id, d.affiliate_url, p.source_url, p.platform, p.asin_or_id
        FROM short_links sl JOIN deals d ON sl.deal_id=d.id JOIN products p ON d.product_id=p.id
        WHERE sl.slug=? AND d.status IN ('approved', 'expired')
    """, (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "Link bulunamadı")
    db.execute(
        "INSERT INTO clicks(deal_id,slug,ip,ua,clicked_at) VALUES(?,?,?,?,?)",
        (row["deal_id"], slug, request.client.host if request.client else "",
         request.headers.get("user-agent", "")[:200], now_str())
    )
    db.commit()
    target = (row["affiliate_url"] if row["affiliate_url"]
              else build_affiliate_url(row["source_url"], row["platform"], row["asin_or_id"]))
    return RedirectResponse(target, status_code=302)
