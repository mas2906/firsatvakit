"""Ürün & deal sayfaları: ana sayfa, arama, submit, detay, watchlist, karşılaştırma, kısa link."""
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from affiliate import build_affiliate_url, make_short_slug
from comments import get_product_comments
from cross_search import get_price_comparison
from db import get_db
from price_rules import deal_score, find_pump_events
from hepsiburada_link import is_hb_short_url, normalize_hb_url, resolve_hb_short_url
from scraper_router import (clean_tracking_params, detect_platform, enqueue_url,
                             extract_product_id, extract_url_from_text, is_short_url,
                             is_valid_product_url, normalize_product_url, resolve_short_url)
from seo_content import build_meta_tags, generate_comparison_data

from .deps import (
    _GROUP_CHEAPEST_FILTER, _cache_get, _cache_set, _is_rate_limited, _verify_csrf,
    current_user, get_client_ip, now_str, templates,
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
async def dipte(request: Request, platform: str = ""):
    if platform and platform not in STORE_NAMES:
        platform = ""
    cards = await _feed(get_db(), "dipte", platform, None)   # yeni tema kartları, puana göre
    cards = sorted(cards, key=lambda c: (c["score"], c["pct"]), reverse=True)
    meta = {"title": f"Şu An En Düşük Fiyatında Olan {len(cards)} Ürün — Gerçek Fırsatlar | FırsatVakti",
            "description": ("Fiyat geçmişine göre şu anda tüm zamanların en düşük fiyatında olan ürünler: "
                            "Amazon, Trendyol, Hepsiburada, N11, Karaca ve Beymen'de olağan fiyatının en az %15 altındaki gerçek indirimler.")}
    if platform:
        meta["canonical"] = f"/dipte?platform={platform}"
    return templates.TemplateResponse("dipte.html", {
        "request": request, "user": current_user(request), "cards": cards, "meta": meta,
        "platform": platform, "stores": STORE_NAMES,
    })


@router.get("/deals")
async def deals_redirect(request: Request):
    qs = request.url.query
    return RedirectResponse(url=f"/?{qs}" if qs else "/", status_code=301)


# ── Yeni tema (2026-10-13): puanlı fırsat akışı ───────────────────────────────
STORE_NAMES = {"amazon": "Amazon", "trendyol": "Trendyol", "hepsiburada": "Hepsiburada",
               "n11": "N11", "karaca": "Karaca", "beymen": "Beymen"}
FEED_TABS = [("one-cikanlar", "Öne Çıkanlar"), ("populer", "Popüler"),
             ("sana-ozel", "Sana Özel"), ("dipte", "Şu An Dipte")]
_TAB_ALIASES = {"all": "one-cikanlar", "top_discount": "one-cikanlar", "cheapest": "one-cikanlar",
                "today": "one-cikanlar", "most_clicked": "populer"}
_FEED_PER_PAGE = 30  # 2026-10-31: 24→30 — 5/3/2 sütunlu ızgaraya tam bölünür
_HERO_COUNT = 5
_tracked_cache = {"at": 0.0, "n": 0}


def _tracked_count(db) -> int:
    """Ana sayfa hero'sundaki 'ürün takipte' sayısı — 10 dk önbellek."""
    import time
    if time.time() - _tracked_cache["at"] > 600:
        _tracked_cache["n"] = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        _tracked_cache["at"] = time.time()
    return _tracked_cache["n"]


def _load_points(db, product_ids) -> dict:
    """Ürün başına fiyat geçmişi [(zaman, fiyat)] — tek sorgu."""
    ids = sorted({int(i) for i in product_ids if i})
    out = {i: [] for i in ids}
    if not ids:
        return out
    for r in db.execute(
        f"SELECT product_id, scraped_at, price_value FROM price_history "
        f"WHERE price_value > 0 AND product_id IN ({','.join(map(str, ids))}) ORDER BY scraped_at, id"
    ).fetchall():
        out[r["product_id"]].append((r["scraped_at"], r["price_value"]))
    return out


def _card(points, *, url, title, image_url, platform, price, at, cheapest=None,
          deal_id=None, created_at=None, stock=None, click_count=0) -> dict:
    """Kart verisi + FırsatVakti Puanı ve sinyal çipleri."""
    sc = deal_score(points, price, at, cheapest)
    chips = []
    if sc.pumped:
        chips.append(("bad", "İndirimden önce zamlandı"))
    elif sc.repeated:
        chips.append(("warn", "Bu indirim sık tekrarlanıyor"))
    if sc.all_time_low and not sc.pumped:
        chips.append(("low", "Tüm zamanların en düşüğü"))
    elif sc.low30 and not sc.pumped:
        chips.append(("low", "30 günün en düşüğü"))
    if cheapest:
        chips.append(("cheap", "En ucuz burada"))
    return {
        "url": url, "title": title or "Ürün", "image_url": image_url, "platform": platform,
        "store": STORE_NAMES.get(platform, (platform or "").title()),
        "price": price, "ref": sc.ref_price if sc.ref_price and sc.ref_price > price else None,
        "pct": max(0, round(sc.pct)), "score": sc.score, "chips": chips[:2],
        "deal_id": deal_id, "created_at": created_at, "stock": stock, "click_count": click_count or 0,
    }


_ACTIVE_DEALS_SQL = """
    SELECT d.id, d.product_id, d.new_price, d.created_at, p.title, p.image_url, p.platform, p.stock,
           (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) AS click_count,
           (SELECT p2.platform FROM products p2
             JOIN product_group_members pgm2 ON p2.id=pgm2.product_id
             JOIN (SELECT product_id, price_value FROM price_history
                   WHERE id IN (SELECT MAX(id) FROM price_history GROUP BY product_id)) lp ON lp.product_id=p2.id
             WHERE pgm2.group_id=(SELECT pgm3.group_id FROM product_group_members pgm3 WHERE pgm3.product_id=d.product_id LIMIT 1)
               AND (p2.stock IS NULL OR p2.stock != 'Stok Yok')
             ORDER BY lp.price_value ASC LIMIT 1) AS best_compare_platform,
           (SELECT 1 FROM product_group_members pgm4 WHERE pgm4.product_id=d.product_id LIMIT 1) AS in_group
    FROM deals d LEFT JOIN products p ON d.product_id = p.id
    INNER JOIN (SELECT product_id, MAX(id) AS max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id = best.max_id
    WHERE d.active=1 {extra}
    LIMIT 500
"""


def _active_deal_cards(db, platform: str) -> list:
    extra, params = "", []
    if platform:
        extra, params = " AND p.platform=?", [platform]
    rows = [dict(r) for r in db.execute(
        _ACTIVE_DEALS_SQL.format(extra=extra + _GROUP_CHEAPEST_FILTER), params).fetchall()]
    pts = _load_points(db, [r["product_id"] for r in rows])
    now = datetime.utcnow()
    cards = []
    for r in rows:
        cheapest = (r["best_compare_platform"] == r["platform"]) if r["in_group"] else None
        c = _card(pts.get(r["product_id"], []), url=f"/deal/{r['id']}", title=r["title"],
                  image_url=r["image_url"], platform=r["platform"], price=r["new_price"], at=now,
                  cheapest=cheapest, deal_id=r["id"], created_at=r["created_at"],
                  stock=r["stock"], click_count=r["click_count"])
        c["product_id"] = r["product_id"]
        cards.append(c)
    return cards


async def _feed(db, tab: str, platform: str, user) -> list:
    """Sekmeye göre sıralı kart listesi. Kişisel olmayan veriler 60 sn önbellekli."""
    import asyncio
    if tab == "dipte":
        key = f"feed:dipte:{platform}"
        cached = _cache_get(key)
        if cached is not None:
            return cached
        items = [d for d in await get_dipte_items() if not platform or d["platform"] == platform]

        def build():
            pts = _load_points(get_db(), [d["id"] for d in items])
            now = datetime.utcnow()
            return [_card(pts.get(d["id"], []), url=f"/product/{d['id']}", title=d["title"],
                          image_url=d["image_url"], platform=d["platform"], price=d["cur"],
                          at=now, created_at=d.get("since")) for d in items]
        cards = sorted(await asyncio.to_thread(build), key=lambda c: (c["score"], c["pct"]), reverse=True)
        _cache_set(key, cards)
        return cards

    key = f"feed:deals:{platform}"
    cards = _cache_get(key)
    if cards is None:
        cards = await asyncio.to_thread(lambda: _active_deal_cards(get_db(), platform))
        _cache_set(key, cards)

    if tab == "populer":
        return sorted(cards, key=lambda c: (c["click_count"], c["score"]), reverse=True)
    if tab == "sana-ozel":
        if not user:
            return []
        from price_alerts import _norm
        watched = {r["product_id"] for r in db.execute(
            "SELECT product_id FROM product_watchlist WHERE user_id=?", (user["id"],)).fetchall()}
        kws = [_norm(r["keyword"]) for r in db.execute(
            "SELECT keyword FROM price_alerts WHERE user_id=?", (user["id"],)).fetchall()]
        kws = [k for k in kws if k]
        mine = [c for c in cards if c.get("product_id") in watched
                or any(k in _norm(c["title"]) for k in kws)]
        return sorted(mine, key=lambda c: c["score"], reverse=True)
    return sorted(cards, key=lambda c: (c["score"], c["pct"]), reverse=True)


@router.get("/")
async def index(request: Request, platform: str = "", page: int = 1, tab: str = "one-cikanlar",
                sort: str = ""):
    db = get_db()
    tab = _TAB_ALIASES.get(tab, tab)
    if tab not in dict(FEED_TABS):
        tab = "one-cikanlar"
    if platform and platform not in STORE_NAMES:
        platform = ""
    user = current_user(request)
    cards = await _feed(db, tab, platform, user)
    total_pages = max(1, (len(cards) + _FEED_PER_PAGE - 1) // _FEED_PER_PAGE)
    page = min(max(page, 1), total_pages)
    page_cards = cards[(page - 1) * _FEED_PER_PAGE: page * _FEED_PER_PAGE]

    alert_count = 0
    if user:
        alert_count = db.execute("SELECT COUNT(*) FROM price_alerts WHERE user_id=?",
                                 (user["id"],)).fetchone()[0]

    # Mağaza filtresindeki adetler her zaman filtresiz akıştan (akış 60sn önbellekli)
    all_cards = cards if not platform else await _feed(db, tab, "", user)
    store_counts: dict = {}
    for c in all_cards:
        store_counts[c["platform"]] = store_counts.get(c["platform"], 0) + 1
    show_hero = tab == "one-cikanlar" and not platform and page == 1
    latest_at = max((str(c.get("created_at") or "") for c in all_cards), default="")

    meta = build_meta_tags("index", {"platform": platform, "page": page})
    # Sekme varyantları aynı içeriğin farklı sıralaması → canonical platform + sayfa
    qs = "&".join(p for p in (f"platform={platform}" if platform else "",
                              f"page={page}" if page > 1 else "") if p)
    meta["canonical"] = "/" + (f"?{qs}" if qs else "")
    if tab != "one-cikanlar":
        meta["robots"] = "noindex, follow"
    return templates.TemplateResponse("index.html", {
        "request": request, "user": user, "q": "", "meta": meta,
        "cards": page_cards, "tab": tab, "tabs": FEED_TABS, "platform": platform,
        "stores": STORE_NAMES, "page": page, "total_pages": total_pages,
        "alert_count": alert_count, "total_cards": len(cards),
        "store_counts": store_counts, "total_all": len(all_cards),
        "hero_cards": cards[:_HERO_COUNT] if show_hero else [],
        "latest_at": latest_at, "tracked_count": _tracked_count(db) if show_hero else 0,
    })


@router.get("/search")
async def search(request: Request, q: str = "", page: int = 1, platform: str = ""):
    """Yeni tema: tüm takip edilen ürünlerde arar (eskiden sadece aktif fırsatlarda —
    aktif fırsat az olduğu için çoğu arama boş dönüyordu); sonuçlar puana göre sıralı."""
    import asyncio
    db = get_db()
    q = q.strip()
    if platform and platform not in STORE_NAMES:
        platform = ""
    cards = []
    if len(q) >= 2:
        def build():
            dbt = get_db()
            extra, params = "", [f"%{q}%"]
            if platform:
                extra, params = " AND p.platform=?", params + [platform]
            rows = [dict(r) for r in dbt.execute(f"""
                SELECT p.id, p.title, p.image_url, p.platform, p.stock, p.last_seen_at,
                       (SELECT price_value FROM price_history h WHERE h.product_id=p.id
                        ORDER BY scraped_at DESC, id DESC LIMIT 1) AS price,
                       (SELECT d.id FROM deals d WHERE d.product_id=p.id AND d.active=1
                        ORDER BY d.id DESC LIMIT 1) AS deal_id
                FROM products p
                WHERE p.title ILIKE ? {extra}
                ORDER BY p.last_seen_at DESC NULLS LAST
                LIMIT 120
            """, params).fetchall()]
            rows = [r for r in rows if r["price"]]
            pts = _load_points(dbt, [r["id"] for r in rows])
            now = datetime.utcnow()
            out = [_card(pts.get(r["id"], []),
                         url=f"/deal/{r['deal_id']}" if r["deal_id"] else f"/product/{r['id']}",
                         title=r["title"], image_url=r["image_url"], platform=r["platform"],
                         price=r["price"], at=now, deal_id=r["deal_id"],
                         created_at=r["last_seen_at"], stock=r["stock"]) for r in rows]
            return sorted(out, key=lambda c: (c["score"], c["pct"]), reverse=True)
        cards = await asyncio.to_thread(build)
    total_pages = max(1, (len(cards) + _FEED_PER_PAGE - 1) // _FEED_PER_PAGE)
    page = min(max(page, 1), total_pages)
    return templates.TemplateResponse("index.html", {
        "request": request, "user": current_user(request), "q": q,
        "cards": cards[(page - 1) * _FEED_PER_PAGE: page * _FEED_PER_PAGE],
        "tab": "", "tabs": FEED_TABS, "platform": platform, "stores": STORE_NAMES,
        "page": page, "total_pages": total_pages, "alert_count": 0, "total_cards": len(cards),
        # Arama sonuç sayfaları ince/tekrarlı içerik — indekslenmesin, linkler takip edilsin
        "meta": {"title": f"“{q[:60]}” İndirimleri ve Fiyatları | FırsatVakti" if q else "Ara | FırsatVakti",
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
