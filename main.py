#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
firsatvakti.com — Ana FastAPI uygulaması v2
Yenilikler: bcrypt güvenlik, çapraz platform arama, yorum sistemi, SEO blog, sitemap
"""

import os, json, secrets, string, time
import logging
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from config import (
    INDEX_CACHE_TTL_SECS, RATE_LIMIT_WINDOW_SECS, RATE_LIMIT_MAX_REQUESTS,
)

log = logging.getLogger("main")

from email_utils import send_password_reset, send_price_alert

SCRAPER_SERVICE_URL = os.getenv("SCRAPER_SERVICE_URL", "")
SCRAPER_SERVICE_KEY = os.getenv("SCRAPER_SERVICE_KEY", "")
WEBHOOK_KEY         = os.getenv("FRONTEND_WEBHOOK_KEY", "")
USE_LOCAL_SCRAPER   = os.getenv("USE_LOCAL_SCRAPER", "false").lower() == "true"

if not SCRAPER_SERVICE_KEY:
    print("⚠️  SCRAPER_SERVICE_KEY tanımlı değil — /api/pending-jobs herkese açık!")
if not WEBHOOK_KEY:
    print("⚠️  FRONTEND_WEBHOOK_KEY tanımlı değil — /api/scraper-webhook herkese açık!")
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from db import get_db, init_db
from scraper_router import detect_platform, enqueue_url, clean_tracking_params, extract_product_id, is_short_url, resolve_short_url
from affiliate import build_affiliate_url, make_short_slug
from telegram_pub import publish_deal, notify_pending_approval
from security import (verify_password, hash_password, needs_rehash,  # noqa: F401
                      migrate_password_on_login, check_login_allowed,
                      record_login_attempt, record_login_attempt_db, get_secret_key)
from cross_search import cross_search_product, get_price_comparison
from comments import add_comment, get_product_comments, vote_comment, delete_comment
from seo_content import (create_article, update_article, get_article_by_slug,
                         list_articles, generate_comparison_data, build_meta_tags,
                         generate_sitemap_xml)

# ── Uygulama başlangıcı ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="FırsatVakti v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def _time_ago(value):
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        diff = int((datetime.utcnow() - dt).total_seconds())
        if diff < 60:     return "az önce kontrol edildi"
        if diff < 3600:   return f"{diff//60} dk önce kontrol edildi"
        if diff < 86400:  return f"{diff//3600} sa önce kontrol edildi"
        return f"{diff//86400} gün önce kontrol edildi"
    except:
        return ""
templates.env.filters["time_ago"] = _time_ago

def _hours_since(value):
    """Bir tarihten bu yana kaç saat geçti? Freshness kontrolü için."""
    if not value:
        return 9999
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return int((datetime.utcnow() - dt).total_seconds() / 3600)
    except:
        return 9999
templates.env.filters["hours_since"] = _hours_since

def _price_tr(value):
    """3798.9 → '3.798,90'  (Türk fiyat formatı)"""
    try:
        v = float(value)
        formatted = f"{v:,.2f}"          # '3,798.90'
        return formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return value
templates.env.filters["price_tr"] = _price_tr

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), max_age=86400 * 7)

# ── CSRF cookie middleware ─────────────────────────────────────
@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    response = await call_next(request)
    if "csrftoken" not in request.cookies:
        token = secrets.token_hex(32)
        response.set_cookie(
            "csrftoken", token,
            samesite="lax", httponly=False, max_age=86400 * 7,
        )
    return response

# ── Rate limiting (in-memory, IP bazlı) ──────────────────────
_rate_store: dict = {}
_RATE_CLEANUP_INTERVAL = 300  # 5 dakikada bir stale key temizle
_rate_last_cleanup = 0.0

def _is_rate_limited(ip: str, action: str) -> bool:
    """True döndürürse istek engellenir."""
    global _rate_last_cleanup
    limit  = RATE_LIMIT_MAX_REQUESTS.get(action, 10)
    window = RATE_LIMIT_WINDOW_SECS
    now    = time.time()
    key    = f"{ip}:{action}"
    _rate_store.setdefault(key, [])
    _rate_store[key] = [t for t in _rate_store[key] if now - t < window]
    if not _rate_store[key]:
        del _rate_store[key]
        return False
    if len(_rate_store[key]) >= limit:
        return True
    _rate_store[key].append(now)
    # Periyodik temizlik: window dışına çıkmış tüm boş key'leri sil
    if now - _rate_last_cleanup > _RATE_CLEANUP_INTERVAL:
        _rate_last_cleanup = now
        stale = [k for k, ts in _rate_store.items() if not any(now - t < window for t in ts)]
        for k in stale:
            del _rate_store[k]
    return False

# ── CSRF doğrulama ────────────────────────────────────────────
def _verify_csrf(request: Request, csrf_token: str) -> bool:
    cookie = request.cookies.get("csrftoken", "")
    if not cookie or not csrf_token:
        return False
    return secrets.compare_digest(cookie, csrf_token)

# ── Ana sayfa cache (TTL bazlı) ───────────────────────────────
_index_cache: dict = {}   # key → {"data": ..., "ts": float}

def _cache_get(key: str):
    entry = _index_cache.get(key)
    if entry and (time.time() - entry["ts"]) < INDEX_CACHE_TTL_SECS:
        return entry["data"]
    return None

def _cache_set(key: str, data):
    _index_cache[key] = {"data": data, "ts": time.time()}

# ═══════════════════════════════════════════════════════════════
# YARDIMCI
# ═══════════════════════════════════════════════════════════════

def get_ticker_items(db):
    return db.execute("""
        SELECT d.discount_pct, d.new_price, p.title, p.platform
        FROM deals d JOIN products p ON d.product_id = p.id
        INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id = best.max_id
        ORDER BY d.created_at DESC LIMIT 10
    """).fetchall()

def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def current_user(request: Request) -> Optional[dict]:
    uid = request.session.get("user_id") if hasattr(request, "session") else None
    if not uid: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def require_admin(request: Request):
    user = current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Erişim yok")
    return user

# ═══════════════════════════════════════════════════════════════
# ANA SAYFA
# ═══════════════════════════════════════════════════════════════

@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse("about.html", {"request": request, "user": current_user(request)})

@app.get("/faq", response_class=HTMLResponse)
async def faq(request: Request):
    return templates.TemplateResponse("faq.html", {"request": request, "user": current_user(request)})

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request, "user": current_user(request)})

@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request, "user": current_user(request)})

@app.get("/contact", response_class=HTMLResponse)
async def contact_get(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request, "user": current_user(request)})

@app.post("/contact", response_class=HTMLResponse)
async def contact_post(request: Request, name: str = Form(""), email: str = Form(""),
                        subject: str = Form(""), message: str = Form(""), csrf_token: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip, "contact"):
        return templates.TemplateResponse("contact.html", {"request": request, "user": current_user(request), "error": "Çok fazla istek. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("contact.html", {"request": request, "user": current_user(request), "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    user = current_user(request)
    if not name or not email or not message:
        return templates.TemplateResponse("contact.html", {
            "request": request, "user": user, "error": "Tüm alanları doldurun."})
    # E-posta gönder
    from email_utils import send_email
    admin_email = os.getenv("ADMIN_EMAIL", os.getenv("SMTP_USER", ""))
    from html import escape as _he
    html_body = f"<p><b>Ad:</b> {_he(name)}</p><p><b>E-posta:</b> {_he(email)}</p><p><b>Mesaj:</b><br>{_he(message)}</p>"
    send_email(admin_email, f"[FırsatVakti İletişim] {subject}", html_body)
    return templates.TemplateResponse("contact.html", {
        "request": request, "user": user, "sent": True})

@app.get("/deals")
async def deals_redirect(request: Request):
    qs = request.url.query
    return RedirectResponse(url=f"/?{qs}" if qs else "/", status_code=301)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, platform: str = "", sort: str = "newest", page: int = 1, tab: str = "all"):
    db = get_db()
    per_page = 24
    offset = (page - 1) * per_page
    where, params = "WHERE d.active=1", []
    if platform: where += " AND p.platform=?"; params.append(platform)

    if tab == "top_discount": order = "d.discount_pct DESC"
    elif tab == "most_clicked": order = "click_count DESC"
    elif tab == "cheapest": order = "d.new_price ASC"
    elif tab == "today": order = "d.created_at DESC"; where += " AND DATE(d.created_at)=DATE('now')"
    else: order = {"newest":"d.created_at DESC","discount":"d.discount_pct DESC","price":"d.new_price ASC"}.get(sort,"d.created_at DESC")

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

    total = db.execute(f"SELECT COUNT(DISTINCT d.product_id) FROM deals d LEFT JOIN products p ON d.product_id=p.id {where}", params).fetchone()[0]

    # Pahalı sorgular cache'lenir (60s TTL) — platform/sort/tab'a bağlı değil
    shared = _cache_get("index_shared")
    if shared is None:
        stats = db.execute("SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals, (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops, (SELECT COUNT(*) FROM product_watchlist) as watchlist_count, (SELECT COUNT(*) FROM product_groups) as tracked_products_count, (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count, (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count, (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count, (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count").fetchone()
        new_deals = db.execute("""
            SELECT d.*, p.title, p.image_url, p.rating, p.platform, p.stock, p.last_seen_at,
                   (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count
            FROM deals d LEFT JOIN products p ON d.product_id=p.id
            INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id
            WHERE d.active=1 ORDER BY d.created_at DESC LIMIT 8
        """).fetchall()
        popular_deals = db.execute("""
            SELECT d.*, p.title, p.image_url, p.rating, p.platform, p.stock, p.last_seen_at,
                   (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count
            FROM deals d LEFT JOIN products p ON d.product_id=p.id
            INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id
            WHERE d.active=1 ORDER BY click_count DESC LIMIT 8
        """).fetchall()
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
        ensure_notify_table(db)
        ns = db.execute("SELECT min_discount FROM notify_settings WHERE user_id=?", (user["id"],)).fetchone()
        if ns: user_min_discount = ns["min_discount"]

    meta = build_meta_tags("index")
    return templates.TemplateResponse("index.html", {
        "request": request, "deals": deals, "platform": platform, "sort": sort,
        "tab": tab, "page": page, "total_pages": max(1,(total+per_page-1)//per_page),
        "stats": shared["stats"], "user": user, "q": "",
        "ticker_items": shared["ticker_items"], "meta": meta,
        "user_min_discount": user_min_discount,
        "new_deals": shared["new_deals"], "popular_deals": shared["popular_deals"],
    })

# ═══════════════════════════════════════════════════════════════
# ARAMA
# ═══════════════════════════════════════════════════════════════

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", page: int = 1):
    db = get_db()
    deals, total_pages, per_page = [], 1, 24
    if q.strip():
        kw = f"%{q.strip()}%"
        offset = (page-1)*per_page
        deals = db.execute("""
            SELECT d.*, p.title, p.image_url, p.rating, p.review_count, p.platform, p.stock
            FROM deals d LEFT JOIN products p ON d.product_id=p.id
            INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id=best.max_id
            WHERE d.active=1 AND (p.title LIKE ? OR p.platform LIKE ?) ORDER BY d.discount_pct DESC LIMIT ? OFFSET ?
        """, (kw, kw, per_page, offset)).fetchall()
        total = db.execute("SELECT COUNT(DISTINCT d.product_id) FROM deals d LEFT JOIN products p ON d.product_id=p.id WHERE d.active=1 AND (p.title LIKE ? OR p.platform LIKE ?)", (kw,kw)).fetchone()[0]
        total_pages = max(1,(total+per_page-1)//per_page)

    stats = db.execute("SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals, (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops, (SELECT COUNT(*) FROM product_watchlist) as watchlist_count, (SELECT COUNT(*) FROM product_groups) as tracked_products_count, (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count, (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count, (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count, (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count").fetchone()
    return templates.TemplateResponse("index.html", {
        "request": request, "deals": deals, "platform": "", "sort": "discount",
        "page": page, "total_pages": total_pages, "stats": stats,
        "user": current_user(request), "q": q, "ticker_items": get_ticker_items(db),
    })

# ═══════════════════════════════════════════════════════════════
# LİNK GÖNDER (v2: çapraz arama seçeneği)
# ═══════════════════════════════════════════════════════════════

GUEST_DAILY_LIMIT = 3
MEMBER_DAILY_LIMIT = 20

def get_today_submit_count(db, user_id=None, ip=None):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if user_id:
        return db.execute("SELECT COUNT(*) FROM submit_log WHERE user_id=? AND DATE(submitted_at)=?", (user_id,today)).fetchone()[0]
    return db.execute("SELECT COUNT(*) FROM submit_log WHERE user_id IS NULL AND ip=? AND DATE(submitted_at)=?", (ip,today)).fetchone()[0]

def log_submit(db, user_id=None, ip=None, product_id=None):
    db.execute("INSERT INTO submit_log(user_id,ip,product_id,submitted_at) VALUES(?,?,?,?)",
               (user_id, ip, product_id, now_str()))
    db.commit()

@app.get("/submit", response_class=HTMLResponse)
async def submit_get(request: Request):
    user = current_user(request)
    db = get_db()
    ip = request.client.host if request.client else "unknown"
    if user and user["role"] == "admin": count, limit = 0, 999999
    elif user: count = get_today_submit_count(db, user_id=user["id"]); limit = MEMBER_DAILY_LIMIT
    else: count = get_today_submit_count(db, ip=ip); limit = GUEST_DAILY_LIMIT
    return templates.TemplateResponse("submit.html", {
        "request": request, "user": user, "remaining": max(0,limit-count), "limit": limit,
    })

@app.post("/submit", response_class=HTMLResponse)
async def submit_post(request: Request, background_tasks: BackgroundTasks,
                      url: str = Form(...), cross_search_platforms: str = Form(""), csrf_token: str = Form("")):
    user = current_user(request)
    db = get_db()
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip, "submit"):
        return templates.TemplateResponse("submit.html", {"request": request, "user": user, "error": "Çok fazla istek gönderildi. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("submit.html", {"request": request, "user": user, "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    if user and user["role"] == "admin": count, limit = 0, 999999
    elif user: count = get_today_submit_count(db, user_id=user["id"]); limit = MEMBER_DAILY_LIMIT
    else: count = get_today_submit_count(db, ip=ip); limit = GUEST_DAILY_LIMIT
    remaining = max(0, limit - count)

    url = url.strip()
    # "Ürün Adı https://..." formatında yapıştırıldıysa URL kısmını çıkar
    if not url.startswith("http"):
        import re as _re
        m = _re.search(r"(https?://\S+)", url)
        if m:
            url = m.group(1)
    if not url.startswith("http"):
        return templates.TemplateResponse("submit.html", {"request": request, "error": "Geçerli URL girin.", "user": user, "remaining": remaining, "limit": limit})

    # Kısa linkleri (ty.gl, app.hb.biz) gerçek URL'ye çevir
    if is_short_url(url):
        url = await resolve_short_url(url)

    platform = detect_platform(url)
    if not platform:
        return templates.TemplateResponse("submit.html", {"request": request, "error": "Desteklenmeyen site.", "user": user, "remaining": remaining, "limit": limit})

    # Limit kontrolünden ÖNCE: ürün zaten sistemde mi? (tüm kullanıcılar için göster)
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

    # Admin ise direkt tarama, diğerleri onay kuyruğuna gider
    if user and user["role"] == "admin":
        product_id = enqueue_url(db, url, platform)
        # cross_search_platforms formdan geldiyse onu kullan, yoksa tüm diğer platformlar
        if cross_search_platforms:
            plats = [p.strip() for p in cross_search_platforms.split(",") if p.strip()]
        else:
            plats = [p for p in ["amazon", "trendyol", "hepsiburada", "n11"] if p != platform]
        await _dispatch_scrape(product_id, url, platform, background_tasks, cross_search_plats=plats)
        log_submit(db, user_id=user["id"], ip=ip, product_id=product_id)
    else:
        # Onay kuyruğuna ekle
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
            "user": user, "remaining": max(0, limit-count-1), "limit": limit,
        })
    else:
        return templates.TemplateResponse("submit.html", {
            "request": request,
            "success": f"Linkin alındı! Admin onayından sonra {platform.title()} taranacak.",
            "user": user, "remaining": max(0, limit-count-1), "limit": limit,
        })

async def _run_cross_search(product_id, platforms=None):
    # Scrape tamamlanana kadar bekle (maks 90s)
    import asyncio
    for _ in range(18):
        await asyncio.sleep(5)
        row = get_db().execute("SELECT title FROM products WHERE id=?", (product_id,)).fetchone()
        if row and row["title"]:
            break
    await cross_search_product(get_db(), product_id, platforms)

# ═══════════════════════════════════════════════════════════════
# ÜRÜN & DEAL SAYFASI (v2: karşılaştırma + yorumlar)
# ═══════════════════════════════════════════════════════════════

@app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: int):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product: raise HTTPException(404, "Ürün bulunamadı")
    deal = db.execute("SELECT id FROM deals WHERE product_id=? ORDER BY id DESC LIMIT 1", (product_id,)).fetchone()
    if deal: return RedirectResponse(f"/deal/{deal['id']}", status_code=302)

    history = _dedup_history(db, product_id)
    comparison = get_price_comparison(db, product_id)
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
        "comments_data": comments_data, "meta": meta,
        "is_watching": is_watching, "watcher_count": watcher_count,
    })

@app.post("/watch/{product_id}")
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


@app.post("/unwatch/{product_id}")
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


@app.get("/watchlist", response_class=HTMLResponse)
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


@app.get("/deal/{deal_id}", response_class=HTMLResponse)
async def deal_detail(request: Request, deal_id: int):
    db = get_db()
    deal = db.execute("""
        SELECT d.*, p.title, p.image_url, p.description, p.rating,
               p.review_count, p.platform, p.source_url, p.asin_or_id, p.stock
        FROM deals d JOIN products p ON d.product_id=p.id WHERE d.id=?
    """, (deal_id,)).fetchone()
    if not deal: raise HTTPException(404, "Fırsat bulunamadı")

    history = _dedup_history(db, deal["product_id"])
    short = db.execute("SELECT slug FROM short_links WHERE deal_id=?", (deal_id,)).fetchone()
    short_url = f"https://firsatvakti.com/go/{short['slug']}" if short else None
    clicks = db.execute("SELECT COUNT(*) FROM clicks WHERE deal_id=?", (deal_id,)).fetchone()[0]

    # v2: karşılaştırma + yorumlar + SEO
    comparison = get_price_comparison(db, deal["product_id"])
    comments_data = get_product_comments(db, deal["product_id"])
    meta = build_meta_tags("deal", dict(deal))
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
        "is_watching": is_watching, "watcher_count": watcher_count,
    })

def _dedup_history(db, product_id):
    raw = db.execute("SELECT price_value,scraped_at FROM price_history WHERE product_id=? ORDER BY scraped_at ASC", (product_id,)).fetchall()
    out, last = [], None
    for r in raw:
        if r["price_value"] != last: out.append(r); last = r["price_value"]
    return out

# ═══════════════════════════════════════════════════════════════
# FİYAT KARŞILAŞTIRMA SAYFASI (YENİ)
# ═══════════════════════════════════════════════════════════════

@app.get("/compare/{group_id}", response_class=HTMLResponse)
async def compare_page(request: Request, group_id: int):
    db = get_db()
    data = generate_comparison_data(db, group_id)
    if not data: raise HTTPException(404, "Karşılaştırma bulunamadı")

    # Aktif deal olan ürünler → 5 dk, olmayanlar → 2 saat eşiği
    threshold_active = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    threshold_normal = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    for p in data.get("products", []):
        has_active_deal = db.execute(
            "SELECT 1 FROM deals WHERE product_id=? AND active=1 LIMIT 1", (p["id"],)
        ).fetchone()
        threshold = threshold_active if has_active_deal else threshold_normal
        last = p.get("last_seen_at") or ""
        if last < threshold:
            existing = db.execute(
                "SELECT id FROM scan_queue WHERE product_id=? AND status IN ('pending','processing')",
                (p["id"],)
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) VALUES(?,?,?,'pending',3,?)",
                    (p["id"], p["source_url"], p["platform"], now_str())
                )
    db.commit()

    meta = build_meta_tags("comparison", {"name": data["group"]["name"],
           "platform_count": data["platform_count"], "image_url": data["group"].get("image_url")})
    return templates.TemplateResponse("compare.html", {
        "request": request, "user": current_user(request), "comparison": data, "meta": meta,
    })

# ═══════════════════════════════════════════════════════════════
# KISA LİNK
# ═══════════════════════════════════════════════════════════════

@app.get("/go/{slug}")
async def short_redirect(slug: str, request: Request):
    db = get_db()
    row = db.execute("""
        SELECT sl.*, d.product_id, d.affiliate_url, p.source_url, p.platform, p.asin_or_id
        FROM short_links sl JOIN deals d ON sl.deal_id=d.id JOIN products p ON d.product_id=p.id
        WHERE sl.slug=?
    """, (slug,)).fetchone()
    if not row: raise HTTPException(404, "Link bulunamadı")
    db.execute("INSERT INTO clicks(deal_id,slug,ip,ua,clicked_at) VALUES(?,?,?,?,?)",
               (row["deal_id"], slug, request.client.host if request.client else "", request.headers.get("user-agent","")[:200], now_str()))
    db.commit()
    # Admin'in girdiği affiliate URL varsa onu kullan, yoksa otomatik oluştur
    target = row["affiliate_url"] if row["affiliate_url"] else build_affiliate_url(row["source_url"], row["platform"], row["asin_or_id"])
    return RedirectResponse(target, status_code=302)

# ═══════════════════════════════════════════════════════════════
# KAYIT / GİRİŞ (v2: bcrypt + rate limiting)
# ═══════════════════════════════════════════════════════════════

@app.get("/register", response_class=HTMLResponse)
async def register_get(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "register"})

@app.post("/register", response_class=HTMLResponse)
async def register_post(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip, "register"):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register", "error": "Çok fazla deneme. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register", "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register", "error": "Bu e-posta zaten kayıtlı."})
    if len(password) < 6:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "register", "error": "Şifre en az 6 karakter olmalı."})

    cur = db.execute("INSERT INTO users(username,email,password_hash,created_at,role) VALUES(?,?,?,?,?)",
                     (username, email, hash_password(password), now_str(), "user"))
    db.commit()
    request.session["user_id"] = cur.lastrowid
    return RedirectResponse("/", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "login"})

@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    db = get_db()
    ip = request.client.host if request.client else "unknown"

    if _is_rate_limited(ip, "login"):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login", "error": "Çok fazla deneme. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login", "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})

    allowed, wait = check_login_allowed(email)
    if not allowed:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
            "error": f"Çok fazla hatalı deneme. {wait//60} dk sonra tekrar dene."})

    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if user and not user["password_hash"]:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login", "error": "Bu hesap Google ile oluşturulmuş. Aşağıdaki Google butonu ile giriş yap."})
    if not user or not verify_password(password, user["password_hash"]):
        record_login_attempt(email, False)
        record_login_attempt_db(db, email, ip, False)
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login", "error": "E-posta veya şifre hatalı."})

    record_login_attempt(email, True)
    record_login_attempt_db(db, email, ip, True)
    if needs_rehash(user["password_hash"]):
        migrate_password_on_login(db, user["id"], password)

    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ═══════════════════════════════════════════════════════════════
# GOOGLE OAUTH
# ═══════════════════════════════════════════════════════════════

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _google_redirect_uri(request: Request) -> str:
    base = os.getenv("BASE_URL", "https://firsatvakti.com")
    return f"{base}/auth/google/callback"


@app.get("/auth/google")
async def google_login(request: Request):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return RedirectResponse("/login?error=google_not_configured", status_code=302)
    state = secrets.token_hex(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": client_id,
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    from urllib.parse import urlencode
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/login", status_code=302)

    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/login", status_code=302)

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_r = await client.post(_GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _google_redirect_uri(request),
                "grant_type": "authorization_code",
            })
            token_r.raise_for_status()
            access_token = token_r.json().get("access_token")

            info_r = await client.get(_GOOGLE_USERINFO_URL,
                                      headers={"Authorization": f"Bearer {access_token}"})
            info_r.raise_for_status()
            info = info_r.json()
    except Exception:
        return RedirectResponse("/login", status_code=302)

    google_id = info.get("id")
    email = info.get("email", "")
    name = info.get("name") or email.split("@")[0]

    if not google_id or not email:
        return RedirectResponse("/login", status_code=302)

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
    if not user:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            db.execute("UPDATE users SET google_id=? WHERE id=?", (google_id, user["id"]))
            db.commit()
        else:
            cur = db.execute(
                "INSERT INTO users(username,email,password_hash,google_id,role,created_at) VALUES(?,?,?,?,?,?)",
                (name, email, "", google_id, "user", now_str())
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=302)


# ═══════════════════════════════════════════════════════════════
# ŞİFRE SIFIRLAMA
# ═══════════════════════════════════════════════════════════════

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("forgot_password.html", {"request": request, "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    # Güvenlik: kullanıcı yoksa da başarı mesajı göster (enumeration'ı önlemek için)
    if user:
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO password_reset_tokens(user_id,token,expires_at,created_at) VALUES(?,?,?,?)",
                   (user["id"], token, expires, now_str()))
        db.commit()
        send_password_reset(user["email"], user["username"], token)
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "success": "Eğer bu e-posta kayıtlıysa sıfırlama linki gönderildi. Gelen kutunu kontrol et.",
    })

@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str):
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now_str())
    ).fetchone()
    if not row:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "invalid": True,
        })
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "token": token,
    })

@app.post("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_post(request: Request, token: str,
                               password: str = Form(...), password2: str = Form(...), csrf_token: str = Form("")):
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    db = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now_str())
    ).fetchone()
    if not row:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "invalid": True,
        })
    if len(password) < 6:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "error": "Şifre en az 6 karakter olmalı.",
        })
    if password != password2:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "error": "Şifreler eşleşmiyor.",
        })
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (hash_password(password), row["user_id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
    db.commit()
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "done": True,
    })

# ═══════════════════════════════════════════════════════════════
# YORUM API'LERİ (YENİ)
# ═══════════════════════════════════════════════════════════════

@app.post("/api/comment")
async def api_add_comment(request: Request, product_id: int = Form(...),
                           body: str = Form(...), rating: int = Form(None), parent_id: int = Form(None)):
    user = current_user(request)
    if not user: raise HTTPException(401, "Giriş yap")
    result = add_comment(get_db(), product_id, user["id"], body, rating, parent_id)
    if not result["ok"]: raise HTTPException(400, result["error"])
    return RedirectResponse(request.headers.get("referer", f"/product/{product_id}"), status_code=302)

@app.get("/api/comments/{product_id}")
async def api_get_comments(product_id: int, page: int = 1, sort: str = "newest"):
    return get_product_comments(get_db(), product_id, page=page, sort=sort)

@app.post("/api/comment/{comment_id}/vote")
async def api_vote(comment_id: int, request: Request, vote: int = Form(...)):
    user = current_user(request)
    if not user: raise HTTPException(401, "Giriş yap")
    result = vote_comment(get_db(), comment_id, user["id"], vote)
    if not result["ok"]: raise HTTPException(400, result["error"])
    return result

@app.post("/api/comment/{comment_id}/delete")
async def api_del_comment(comment_id: int, request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401, "Giriş yap")
    result = delete_comment(get_db(), comment_id, user["id"], user["role"]=="admin")
    if not result["ok"]: raise HTTPException(400, result["error"])
    return RedirectResponse(request.headers.get("referer", "/"), status_code=302)

# ═══════════════════════════════════════════════════════════════
# BLOG / SEO (YENİ)
# ═══════════════════════════════════════════════════════════════

@app.get("/blog", response_class=HTMLResponse)
async def blog_list_page(request: Request, category: str = "", page: int = 1):
    data = list_articles(get_db(), category=category or None, page=page)
    return templates.TemplateResponse("blog_list.html", {
        "request": request, "user": current_user(request),
        "articles": data["articles"], "total_pages": data["total_pages"],
        "page": page, "category": category,
    })

@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_detail_page(request: Request, slug: str):
    article = get_article_by_slug(get_db(), slug)
    if not article: raise HTTPException(404, "Yazı bulunamadı")
    meta = build_meta_tags("article", article)
    return templates.TemplateResponse("blog_detail.html", {
        "request": request, "user": current_user(request), "article": article, "meta": meta,
    })

@app.get("/admin/articles", response_class=HTMLResponse)
async def admin_articles(request: Request):
    user = require_admin(request)
    data = list_articles(get_db(), status=None, per_page=50)
    return templates.TemplateResponse("admin_articles.html", {
        "request": request, "user": user, "articles": data["articles"],
    })

@app.get("/admin/article/new", response_class=HTMLResponse)
async def admin_article_new(request: Request):
    require_admin(request)
    return templates.TemplateResponse("admin_article_edit.html", {"request": request, "user": current_user(request), "article": None})

@app.post("/admin/article/save")
async def admin_article_save(request: Request, article_id: int = Form(0),
                              title: str = Form(...), summary: str = Form(""),
                              body_html: str = Form(...), category: str = Form("rehber"),
                              tags: str = Form(""), status: str = Form("draft"),
                              csrf_token: str = Form("")):
    user = require_admin(request)
    if not _verify_csrf(request, csrf_token):
        raise HTTPException(403, "Geçersiz CSRF token")
    db = get_db()
    if article_id:
        update_article(db, article_id, title=title, summary=summary,
                       body_html=body_html, category=category, tags=tags, status=status)
    else:
        create_article(db, title, body_html, user["id"], category=category,
                       tags=tags, summary=summary, status=status)
    return RedirectResponse("/admin/articles", status_code=302)

# ═══════════════════════════════════════════════════════════════
# SITEMAP & ROBOTS (YENİ)
# ═══════════════════════════════════════════════════════════════

@app.get("/sitemap.xml")
async def sitemap():
    return Response(content=generate_sitemap_xml(get_db()), media_type="application/xml")

@app.get("/robots.txt")
async def robots():
    return Response(content="User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nSitemap: https://firsatvakti.com/sitemap.xml\n", media_type="text/plain")

# Google Search Console domain doğrulama
# Search Console'dan aldığın dosya adını GOOGLE_SITE_VERIFICATION env'e ekle
# Örnek: GOOGLE_SITE_VERIFICATION=googleabc123def456.html
_gsc_token = os.getenv("GOOGLE_SITE_VERIFICATION", "")
if _gsc_token:
    @app.get(f"/{_gsc_token}")
    async def google_site_verification():
        token = _gsc_token.replace(".html", "")
        return Response(content=f"google-site-verification: {token}", media_type="text/html")

# ═══════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════

def ensure_notify_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notify_settings (
        id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE REFERENCES users(id),
        tg_chat_id TEXT DEFAULT '', wa_phone TEXT DEFAULT '', min_discount INTEGER DEFAULT 10,
        platforms TEXT DEFAULT 'amazon,trendyol,n11', tg_active INTEGER DEFAULT 0,
        wa_active INTEGER DEFAULT 0, updated_at TEXT)""")
    db.commit()

@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    db = get_db(); ensure_notify_table(db)
    tg = db.execute("SELECT * FROM tg_subscriptions WHERE user_id=?", (user["id"],)).fetchone()
    notify = db.execute("SELECT * FROM notify_settings WHERE user_id=?", (user["id"],)).fetchone()

    # Kullanıcının takip ettiği ürünler (submit_log üzerinden)
    tracked = db.execute("""
        SELECT * FROM (
            SELECT DISTINCT ON (p.id)
                   p.*, ph.price_value as current_price,
                   d.discount_pct, d.new_price, d.old_price, d.active as deal_active,
                   sl.submitted_at
            FROM submit_log sl
            JOIN products p ON sl.product_id = p.id
            LEFT JOIN (SELECT product_id, MAX(price_value) as price_value FROM price_history GROUP BY product_id) ph ON ph.product_id = p.id
            LEFT JOIN (SELECT product_id, discount_pct, new_price, old_price, active FROM deals WHERE active=1 ORDER BY id DESC LIMIT 1) d ON d.product_id = p.id
            WHERE sl.user_id=? AND sl.product_id IS NOT NULL
            ORDER BY p.id, sl.submitted_at DESC
        ) sub
        ORDER BY submitted_at DESC LIMIT 30
    """, (user["id"],)).fetchall()

    watchlist_count = db.execute(
        "SELECT COUNT(*) FROM product_watchlist WHERE user_id=?", (user["id"],)
    ).fetchone()[0]

    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "tg": tg,
        "notify": notify, "tracked": tracked,
        "watchlist_count": watchlist_count,
    })

@app.post("/settings/telegram")
async def settings_tg(request: Request, tg_chat_id: str = Form(""), wa_phone: str = Form(""),
                       min_discount: int = Form(10), tg_active: str = Form(""),
                       wa_active: str = Form(""), platforms: list = Form([])):
    user = current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    db = get_db(); ensure_notify_table(db)
    plat_str = ",".join(platforms) if platforms else "amazon,trendyol,n11"
    tg_on, wa_on = 1 if tg_active else 0, 1 if wa_active else 0

    db.execute("""INSERT INTO notify_settings(user_id,tg_chat_id,wa_phone,min_discount,platforms,tg_active,wa_active,updated_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET tg_chat_id=excluded.tg_chat_id,wa_phone=excluded.wa_phone,
        min_discount=excluded.min_discount,platforms=excluded.platforms,tg_active=excluded.tg_active,wa_active=excluded.wa_active,updated_at=excluded.updated_at""",
               (user["id"], tg_chat_id.strip(), wa_phone.strip(), min_discount, plat_str, tg_on, wa_on, now_str()))

    if tg_on and tg_chat_id.strip():
        db.execute("""INSERT INTO tg_subscriptions(user_id,chat_id,min_discount_pct,platforms,active,updated_at)
            VALUES(?,?,?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,min_discount_pct=excluded.min_discount_pct,
            platforms=excluded.platforms,active=1,updated_at=excluded.updated_at""",
                   (user["id"], tg_chat_id.strip(), min_discount, plat_str, now_str()))
    else:
        db.execute("UPDATE tg_subscriptions SET active=0 WHERE user_id=?", (user["id"],))
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)

# ═══════════════════════════════════════════════════════════════
# ADMİN PANELİ
# ═══════════════════════════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, q: str = ""):
    user = require_admin(request)
    db = get_db()
    stats = db.execute("""SELECT (SELECT COUNT(*) FROM products) as products,
        (SELECT COUNT(*) FROM deals) as deals, (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals,
        (SELECT COUNT(*) FROM deals d JOIN products p ON d.product_id=p.id WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok')) as pending_deals,
        (SELECT COUNT(*) FROM users) as users, (SELECT COUNT(*) FROM clicks) as clicks,
        (SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending','processing')) as queue_pending,
        (SELECT COUNT(*) FROM comments) as comments,
        (SELECT COUNT(*) FROM articles) as articles,
        (SELECT COUNT(*) FROM product_groups) as groups,
        (SELECT COUNT(*) FROM link_queue WHERE status='pending') as pending_links,
        (SELECT COUNT(*) FROM scan_queue WHERE status='dead') as dead_queue,
        (SELECT COUNT(*) FROM scraper_errors WHERE occurred_at > datetime('now','-24 hours')) as errors_24h
    """).fetchone()
    pending_links = db.execute("""
        SELECT lq.*, u.username FROM link_queue lq
        LEFT JOIN users u ON lq.user_id = u.id
        WHERE lq.status = 'pending'
        ORDER BY lq.submitted_at DESC LIMIT 100
    """).fetchall()
    scraper_errors = db.execute("""
        SELECT se.*, p.title, p.platform FROM scraper_errors se
        LEFT JOIN products p ON se.product_id = p.id
        ORDER BY se.occurred_at DESC LIMIT 50
    """).fetchall()
    # Onay bekleyen deal'lar (aynı gruptaki diğer platformda aktif deal varsa işaretle)
    pending_deals = db.execute("""
        SELECT d.*, p.title, p.platform, p.image_url, p.source_url, sl.slug,
               (SELECT GROUP_CONCAT(p2.platform || '|' || d2.id || '|' || CAST(d2.new_price AS INTEGER))
                FROM product_group_members pgm1
                JOIN product_group_members pgm2 ON pgm1.group_id = pgm2.group_id AND pgm2.product_id != pgm1.product_id
                JOIN products p2 ON pgm2.product_id = p2.id
                JOIN deals d2 ON d2.product_id = p2.id AND d2.active = 1
                WHERE pgm1.product_id = d.product_id AND p2.platform != p.platform
               ) as other_active_deals
        FROM deals d JOIN products p ON d.product_id=p.id
        LEFT JOIN short_links sl ON sl.deal_id=d.id
        WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok')
        ORDER BY d.created_at DESC LIMIT 50
    """).fetchall()
    recent_deals = db.execute("SELECT d.*,p.title,p.platform FROM deals d JOIN products p ON d.product_id=p.id WHERE d.status!='pending' ORDER BY d.created_at DESC LIMIT 10").fetchall()
    if q:
        products = db.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count
            FROM products p
            WHERE p.title LIKE ? OR p.source_url LIKE ?
            ORDER BY p.id DESC LIMIT 200
        """, (f"%{q}%", f"%{q}%")).fetchall()
    else:
        products = db.execute("SELECT p.*,(SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count FROM products p ORDER BY p.id DESC LIMIT 10").fetchall()
    queue = db.execute("SELECT * FROM scan_queue ORDER BY created_at DESC LIMIT 10").fetchall()
    # Chart verisi — platform bazlı aktif deal sayısı (her zaman 4 platform)
    chart_raw = {r["platform"]: r["cnt"] for r in db.execute("""
        SELECT p.platform, COUNT(*) as cnt
        FROM deals d JOIN products p ON d.product_id=p.id
        WHERE d.active=1 GROUP BY p.platform
    """).fetchall()}
    _platforms = ["amazon", "trendyol", "n11", "hepsiburada"]
    chart_labels = [p.capitalize() for p in _platforms]
    chart_values = [chart_raw.get(p, 0) for p in _platforms]

    # Son 7 günlük deal akışı — boş günler 0 olarak gösterilsin
    from datetime import date, timedelta
    daily_raw = {r["day"]: r["cnt"] for r in db.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM deals WHERE created_at >= datetime('now','-7 days')
        GROUP BY day ORDER BY day
    """).fetchall()}
    today = date.today()
    daily_labels = [(today - timedelta(days=6-i)).isoformat() for i in range(7)]
    daily_values = [daily_raw.get(d, 0) for d in daily_labels]

    bulk_result = request.session.pop("bulk_result", None)
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "stats": stats,
        "pending_deals": pending_deals,
        "recent_deals": recent_deals, "products": products, "queue": queue,
        "wa_channel_url": os.getenv("WA_CHANNEL_URL", ""),
        "chart_labels": chart_labels, "chart_values": chart_values,
        "daily_labels": daily_labels, "daily_values": daily_values,
        "search_q": q,
        "pending_links": pending_links,
        "scraper_errors": scraper_errors,
        "bulk_result": bulk_result,
    })

# ── Deal Onaylama (affiliate link ile) ────────────────────────
@app.post("/admin/deal/{deal_id}/approve")
async def admin_approve_deal(deal_id: int, request: Request,
                              affiliate_url: str = Form("")):
    require_admin(request)
    db = get_db()
    deal = db.execute("SELECT d.*, p.title, p.image_url, p.platform FROM deals d JOIN products p ON d.product_id=p.id WHERE d.id=?", (deal_id,)).fetchone()
    if not deal:
        return RedirectResponse("/admin", status_code=302)

    # Zaten onaylanmışsa tekrar yayın yapma
    if deal["status"] == "approved":
        return RedirectResponse("/admin", status_code=302)

    aff = affiliate_url.strip() if affiliate_url.strip() else None

    db.execute("""UPDATE deals SET active=1, status='approved', affiliate_url=? WHERE id=?""",
               (aff, deal_id))
    db.commit()

    # Telegram yayını — onay sonrası
    slug_row = db.execute("SELECT slug FROM short_links WHERE deal_id=?", (deal_id,)).fetchone()
    slug = slug_row["slug"] if slug_row else make_short_slug()
    product = db.execute("SELECT * FROM products WHERE id=?", (deal["product_id"],)).fetchone()
    prod_dict = dict(product)
    prod_dict["deal_id"] = deal_id
    await publish_deal(db, deal_id, prod_dict, deal["new_price"], deal["old_price"], deal["discount_pct"], slug)

    # WhatsApp paylaşım metni oluştur
    title   = (prod_dict.get("title") or "Ürün").strip()[:80]
    pct     = deal["discount_pct"]
    old_p   = int(deal["old_price"])
    new_p   = int(deal["new_price"])
    link    = f"https://firsatvakti.com/go/{slug}"
    wa_text = f"🔥 %{int(pct)} İNDİRİM!\n\n🛍 {title}\n\n💰 {old_p:,} TL → {new_p:,} TL\n\n👉 {link}\n\n🌐 firsatvakti.com".replace(",", ".")

    from urllib.parse import quote
    print(f"[admin] ✔ Deal #{deal_id} onaylandı ve yayınlandı")
    return RedirectResponse(f"/admin?wa_text={quote(wa_text)}", status_code=302)

# ── Deal Reddetme ─────────────────────────────────────────────
@app.post("/admin/deal/{deal_id}/reject")
async def admin_reject_deal(deal_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE deals SET active=0, status='rejected' WHERE id=?", (deal_id,))
    db.commit()
    print(f"[admin] ✘ Deal #{deal_id} reddedildi")
    return RedirectResponse("/admin", status_code=302)

# ── Scraper Hataları Temizle ──────────────────────────────────
@app.post("/admin/clear-errors")
async def admin_clear_errors(request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM scraper_errors")
    db.commit()
    return RedirectResponse("/admin", status_code=302)

# ── Link Kuyruğu Onay / Red ───────────────────────────────────
@app.post("/admin/link/{link_id}/approve")
async def admin_approve_link(link_id: int, request: Request, background_tasks: BackgroundTasks):
    require_admin(request)
    db = get_db()
    lq = db.execute("SELECT * FROM link_queue WHERE id=?", (link_id,)).fetchone()
    if not lq or lq["status"] != "pending":
        return RedirectResponse("/admin", status_code=302)
    db.execute("UPDATE link_queue SET status='approved', reviewed_at=? WHERE id=?", (now_str(), link_id))
    db.commit()
    product_id = enqueue_url(db, lq["url"], lq["platform"])
    await _dispatch_scrape(product_id, lq["url"], lq["platform"], background_tasks)
    background_tasks.add_task(_run_cross_search, product_id)
    print(f"[admin] ✔ Link #{link_id} onaylandı, ürün #{product_id} taranıyor")
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/link/{link_id}/reject")
async def admin_reject_link(link_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE link_queue SET status='rejected', reviewed_at=? WHERE id=?", (now_str(), link_id))
    db.commit()
    print(f"[admin] ✘ Link #{link_id} reddedildi")
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/deal/{deal_id}/toggle")
async def admin_toggle_deal(deal_id: int, request: Request):
    require_admin(request); db = get_db()
    db.execute("UPDATE deals SET active=1-active WHERE id=?", (deal_id,)); db.commit()
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/deal/{deal_id}/delete")
async def admin_delete_deal(deal_id: int, request: Request):
    require_admin(request); db = get_db()
    db.execute("DELETE FROM short_links WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM clicks WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM deals WHERE id=?", (deal_id,)); db.commit()
    return RedirectResponse("/admin", status_code=302)

@app.post("/admin/group/{group_id}/remove/{product_id}")
async def admin_remove_from_group(group_id: int, product_id: int, request: Request):
    """Ürünü gruptan çıkar (ürünü silmez). Yanlış çapraz arama eşleşmelerini düzeltmek için."""
    require_admin(request)
    db = get_db()
    # Sil değil, 'removed' olarak işaretle — cross_search tekrar ekleyemez (UNIQUE constraint)
    db.execute(
        "UPDATE product_group_members SET match_type='removed', added_at=? WHERE group_id=? AND product_id=?",
        (now_str(), group_id, product_id)
    )
    # Aktif (removed olmayan) üye sayısını kontrol et
    remaining = db.execute(
        "SELECT COUNT(*) FROM product_group_members WHERE group_id=? AND match_type != 'removed'",
        (group_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM product_group_members WHERE group_id=?", (group_id,))
        db.execute("DELETE FROM product_groups WHERE id=?", (group_id,))
        db.commit()
        log.info(f"[admin] Ürün #{product_id} grup #{group_id}'den kaldırıldı, grup silindi")
        return RedirectResponse("/", status_code=302)
    db.commit()
    log.info(f"[admin] Ürün #{product_id} grup #{group_id}'den kaldırıldı (blocklist)")
    return RedirectResponse(f"/compare/{group_id}", status_code=302)


def _delete_product_cascade(db, product_id: int) -> None:
    dids = [r["id"] for r in db.execute("SELECT id FROM deals WHERE product_id=?", (product_id,)).fetchall()]
    for did in dids:
        db.execute("DELETE FROM short_links WHERE deal_id=?", (did,))
        db.execute("DELETE FROM clicks WHERE deal_id=?", (did,))
    db.execute("DELETE FROM deals WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM price_history WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM comment_votes WHERE comment_id IN (SELECT id FROM comments WHERE product_id=?)", (product_id,))
    db.execute("DELETE FROM comments WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM product_group_members WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM product_watchlist WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM article_products WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM scraper_errors WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM submit_log WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    db.commit()


@app.post("/admin/product/{product_id}/delete")
async def admin_delete_product(product_id: int, request: Request):
    require_admin(request); db = get_db()
    _delete_product_cascade(db, product_id)
    return RedirectResponse("/admin", status_code=302)

# ── Scan state (admin progress tracking) ──────────────────────
_SCAN_STATE: dict = {"running": False, "current": 0, "total": 0, "title": "", "platform": "", "done": 0, "failed": 0, "errors": []}

@app.get("/api/scan-status")
async def scan_status(request: Request):
    require_admin(request)
    db = get_db()
    queue_pending = db.execute("SELECT COUNT(*) FROM scan_queue WHERE status IN ('pending','processing')").fetchone()[0]
    return JSONResponse({**_SCAN_STATE, "queue_pending": queue_pending})

@app.post("/admin/scan/run")
async def admin_run_scan(request: Request):
    """Stale/failed işleri pending'e çekip WSL scraper'ın almasını sağla."""
    require_admin(request)
    db = get_db()
    # processing ama 5 dakikadır güncellenmemiş → pending'e al
    stale = db.execute("""
        UPDATE scan_queue SET status='pending', updated_at=?
        WHERE status='processing' AND updated_at < datetime('now', '-5 minutes')
    """, (now_str(),)).rowcount
    # failed → pending'e al (WSL yeniden denesin)
    failed = db.execute("""
        UPDATE scan_queue SET status='pending', updated_at=?
        WHERE status='failed'
    """, (now_str(),)).rowcount
    db.commit()
    print(f"[admin-reset] stale={stale} failed={failed} → pending'e alındı")
    return RedirectResponse("/admin", status_code=302)

BULK_ADD_LIMIT = 200

@app.post("/admin/bulk-add")
async def admin_bulk_add(request: Request, background_tasks: BackgroundTasks,
                         urls: str = Form("")):
    require_admin(request)
    db = get_db()
    lines = [u.strip() for u in urls.splitlines() if u.strip()]
    if len(lines) > BULK_ADD_LIMIT:
        lines = lines[:BULK_ADD_LIMIT]

    added, skipped, errors = 0, 0, []
    import re as _re
    for raw_url in lines:
        # "Ürün Adı https://..." formatında gelirse URL kısmını çıkar
        if not raw_url.startswith("http"):
            m = _re.search(r"(https?://\S+)", raw_url)
            raw_url = m.group(1) if m else raw_url

        if is_short_url(raw_url):
            raw_url = await resolve_short_url(raw_url)

        platform = detect_platform(raw_url)
        if not platform:
            errors.append(f"Desteklenmeyen site: {raw_url[:80]}")
            continue

        clean = clean_tracking_params(raw_url, platform)
        is_new = db.execute("SELECT id FROM products WHERE source_url=?", (clean,)).fetchone() is None
        try:
            product_id = enqueue_url(db, clean, platform)
            cross_plats = [p for p in ["amazon", "trendyol", "hepsiburada", "n11"] if p != platform]
            await _dispatch_scrape(product_id, clean, platform, background_tasks, cross_search_plats=cross_plats)
            if is_new:
                added += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{raw_url[:60]}: {exc}")

    request.session["bulk_result"] = {
        "added": added,
        "skipped": skipped,
        "errors": errors,
        "total": len(lines),
    }
    return RedirectResponse("/admin#bulk-add", status_code=302)

# ═══════════════════════════════════════════════════════════════
# ARKA PLAN TARAMA
# ═══════════════════════════════════════════════════════════════

async def _dispatch_scrape(product_id: int, url: str, platform: str, background_tasks,
                           cross_search_plats: list = None):
    """Scraper servis varsa oraya gönder, USE_LOCAL_SCRAPER=true ise kuyruğa bırak, yoksa lokal çalıştır."""
    if SCRAPER_SERVICE_URL:
        background_tasks.add_task(_remote_scrape, product_id, url, platform)
    elif USE_LOCAL_SCRAPER:
        # İş zaten scan_queue'ya eklendi (enqueue_url tarafından).
        # WSL'deki local_scraper.py /api/pending-jobs'ı poll ederek çekecek.
        log.debug(f"[dispatch] #{product_id} scan_queue'da bekliyor (USE_LOCAL_SCRAPER=true)")
    else:
        background_tasks.add_task(scrape_and_save, product_id, url, platform)
    if cross_search_plats:
        background_tasks.add_task(_run_cross_search, product_id, cross_search_plats)


async def _remote_scrape(product_id: int, url: str, platform: str):
    """Scraper servisine HTTP isteği gönder."""
    import httpx
    headers = {"X-Api-Key": SCRAPER_SERVICE_KEY}
    payload = {"product_id": product_id, "url": url, "platform": platform}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{SCRAPER_SERVICE_URL}/scrape", json=payload, headers=headers)
            print(f"[scraper-svc] → {r.status_code} product_id={product_id}")
    except Exception as e:
        print(f"[scraper-svc] ✘ Bağlantı hatası, lokal çalışıyor: {e}")
        await scrape_and_save(product_id, url, platform)


async def scrape_and_save(product_id: int, url: str, platform: str):
    import asyncio
    from scrapers.router import scrape_product
    db = get_db()
    try:
        data = await asyncio.wait_for(scrape_product(url, platform), timeout=75)
        if not data:
            db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?",
                       (now_str(), product_id))
            db.commit()
            return
        await _save_scraped_data(product_id, data)
    except asyncio.TimeoutError:
        print(f"[scrape] Timeout #{product_id} ({platform}) — 75s aşıldı")
        db = get_db()
        db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?",
                   (now_str(), product_id))
        db.commit()
    except Exception as e:
        print(f"[scrape] Hata #{product_id}: {e}")
        db = get_db()
        db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?",
                   (now_str(), product_id))
        db.commit()

# ═══════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/pending-jobs")
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
        f"""SELECT sq.id AS sq_id, sq.product_id, sq.priority, p.source_url, p.platform, p.last_seen_at
            FROM scan_queue sq
            JOIN products p ON p.id = sq.product_id
            WHERE sq.status = 'pending'{extra_sql}
            ORDER BY sq.priority ASC, sq.created_at ASC
            LIMIT ?""",
        extra_vals + [limit]
    ).fetchall()
    # Alınan işleri "processing" olarak işaretle — sq.id ile, product_id ile DEĞİL
    sq_ids = [r["sq_id"] for r in rows]
    if sq_ids:
        db.execute(
            f"UPDATE scan_queue SET status='processing', updated_at=? WHERE id IN ({','.join('?'*len(sq_ids))})",
            [now_str()] + sq_ids
        )
        db.commit()
    return JSONResponse([{"product_id": r["product_id"], "url": r["source_url"], "platform": r["platform"], "priority": r["priority"], "last_seen_at": r["last_seen_at"]} for r in rows])


@app.post("/api/reset-stale-jobs")
async def reset_stale_jobs(request: Request):
    """Processing kalan işleri pending'e döndür. force=true → süre sınırı olmadan tümünü sıfırla."""
    key = request.headers.get("X-Api-Key", "")
    if SCRAPER_SERVICE_KEY:
        if key != SCRAPER_SERVICE_KEY:
            raise HTTPException(401, "Geçersiz API anahtarı")
    elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(401, "SCRAPER_SERVICE_KEY tanımlı değil")
    force = (request.query_params.get("force", "false").lower() == "true")
    db = get_db()
    if force:
        cur = db.execute(
            "UPDATE scan_queue SET status='pending', updated_at=? WHERE status IN ('processing','done','failed')",
            (now_str(),)
        )
    else:
        cur = db.execute("""
            UPDATE scan_queue SET status='pending', updated_at=?
            WHERE status='processing' AND updated_at < datetime('now', '-10 minutes')
        """, (now_str(),))
    db.commit()
    return JSONResponse({"reset": cur.rowcount, "force": force})


@app.post("/api/scraper-webhook")
async def scraper_webhook(request: Request, background_tasks: BackgroundTasks):
    """Scraper servisi webhook (timeout 5s, log metrics)."""
    import time
    start = time.time()

    key = request.headers.get("X-Webhook-Key", "")
    if WEBHOOK_KEY and key != WEBHOOK_KEY:
        raise HTTPException(401, "Invalid webhook key")

    body = await request.json()
    product_id = body.get("product_id")
    data = body.get("data", {})
    url = body.get("url", "")
    platform = body.get("platform", "")

    if not product_id:
        raise HTTPException(400, "product_id required")

    log.info(f"[webhook] {product_id} {platform} started ({time.time()-start:.1f}s)")

    # Background DB save (hızlı response)
    background_tasks.add_task(_save_scraped_data_async, product_id, data, url, platform)

    log.info(f"[webhook] {product_id} done ({time.time()-start:.1f}s)")
    return {"ok": True, "processed": True}

async def _save_scraped_data_async(product_id, data, url, platform):
    """Background scraped data save."""
    try:
        await _save_scraped_data(product_id, data)
    except Exception as e:
        log.error(f"[webhook-bg] {product_id} error: {e}")


def _smart_price_analysis(db, product_id: int, price: float, prev_price: float):
    """
    İnsan gibi düşünen fiyat analizi.
    Returns: (is_genuine_deal: bool, ref_price: float, reason: str)
    """
    rows = db.execute(
        "SELECT price_value, scraped_at FROM price_history "
        "WHERE product_id=? AND scraped_at >= datetime('now','-90 days') "
        "ORDER BY scraped_at ASC", (product_id,)
    ).fetchall()
    prices = [r[0] for r in rows]

    if len(prices) < 3:
        return price < prev_price, prev_price, "veri az"

    sorted_p = sorted(prices)
    median = sorted_p[len(sorted_p) // 2]

    # 1. Outlier tespiti: medyandan %45+ sapan ve ≤2 kez görülen fiyatları çıkar
    def _seen_count(p, ps):
        return sum(1 for x in ps if abs(x - p) / max(p, 1) < 0.03)

    clean = [p for p in prices if
             abs(p - median) / max(median, 1) <= 0.45 or _seen_count(p, prices) > 2]
    if not clean:
        clean = prices
    clean_sorted = sorted(clean)

    # 2. "Normal fiyat" = en çok tekrar eden fiyat aralığı (±%5 tolerans)
    best_p, best_cnt = clean[0], 0
    for p in clean:
        cnt = sum(1 for x in clean if abs(x - p) / max(p, 1) < 0.05)
        if cnt > best_cnt:
            best_cnt, best_p = cnt, p
    modal_price = best_p

    # 3. Yapay enflasyon tespiti: son 14 günde fiyat suni yükseldiyse
    #    spike öncesi kararlı fiyatı referans al
    cutoff_14 = db.execute("SELECT datetime('now','-14 days')").fetchone()[0]
    recent_14 = [r[0] for r in rows if r[1] >= cutoff_14]
    pre_spike_price = None
    if len(recent_14) >= 2 and recent_14[-1] > prev_price:
        # Mevcut prev_price (son kayıt) spike ise, ondan önceki kararlı fiyatı bul
        older = [p for p in clean if p < prev_price * 0.90]
        if older:
            pre_spike_price = max(older)

    # Referans fiyat: spike tespiti > modal > max(clean)
    ref_price = pre_spike_price or modal_price or max(clean_sorted)
    if ref_price <= price:
        ref_price = prev_price

    # 4. Gerçek indirim: şu anki fiyat temiz geçmişin alt %30'unda mı?
    p30 = clean_sorted[max(0, int(len(clean_sorted) * 0.30) - 1)]
    if price >= p30:
        return False, ref_price, f"tarihsel dusuk degil (esik={p30:.0f})"

    return True, ref_price, "gercek indirim"


async def _save_scraped_data(product_id: int, data: dict):
    """Scraping sonucunu işle: fiyat geçmişi, deal yönetimi, Telegram + email bildirimleri."""
    db = get_db()
    now = now_str()
    stock = data.get("stock")

    db.execute("""UPDATE products SET title=COALESCE(?,title), image_url=COALESCE(?,image_url),
        description=COALESCE(?,description), rating=COALESCE(?,rating),
        review_count=COALESCE(?,review_count), brand=COALESCE(?,brand),
        barcode=COALESCE(?,barcode), stock=COALESCE(?,stock), last_seen_at=? WHERE id=?""",
               (data.get("title"), data.get("image_url"), data.get("description"),
                data.get("rating"), data.get("review_count"), data.get("brand"),
                data.get("barcode"), stock, now, product_id))

    price = data.get("price")
    if not price or price <= 0:
        db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now, product_id))
        db.commit()
        return

    # Stok yoksa fiyatı kaydet ama deal oluşturma
    stock_lower = (stock or "").lower()
    if stock == "Stok Yok" or any(kw in stock_lower for kw in [
        "mevcut değil", "tükendi", "stokta yok", "out of stock", "sold out", "unavailable"
    ]):
        prev_s = db.execute("SELECT price_value FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 1",
                            (product_id,)).fetchone()
        if not prev_s or prev_s["price_value"] != price:
            db.execute("INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
                       (product_id, price, "TRY", now))
        for d in db.execute("SELECT id FROM deals WHERE product_id=? AND active=1", (product_id,)).fetchall():
            db.execute("UPDATE deals SET active=0, expires_at=? WHERE id=?", (now, d["id"]))
            log.info(f"[webhook] ⏹ Deal #{d['id']} kapatıldı — stok yok")
        db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now, product_id))
        db.commit()
        return

    prev = db.execute("SELECT price_value FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 1",
                      (product_id,)).fetchone()
    if prev and prev["price_value"] == price:
        db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now, product_id))
        db.commit()
        return

    db.execute("INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
               (product_id, price, "TRY", now))

    if prev and prev["price_value"] and price < prev["price_value"]:
        is_deal, old_price, reason = _smart_price_analysis(db, product_id, price, prev["price_value"])
        if not is_deal:
            db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now, product_id))
            db.commit()
            log.info(f"[webhook] ⏭ #{product_id} deal degil: {reason}")
            return

        pct = (old_price - price) / old_price * 100

        if pct > 90:
            log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim şüpheli (>90), deal oluşturulmadı")
        elif stock != "Stokta Var" and pct > 75:
            log.warning(f"[webhook] ⚠ #{product_id} %{pct:.0f} indirim + stok={stock!r} şüpheli, atlandı")
        elif pct >= 5:
            existing_deal = db.execute("""
                SELECT d.id, sl.slug FROM deals d
                LEFT JOIN short_links sl ON sl.deal_id = d.id
                WHERE d.product_id=? AND d.status IN ('pending','approved')
                ORDER BY d.id DESC LIMIT 1
            """, (product_id,)).fetchone()

            if existing_deal:
                deal_id = existing_deal["id"]
                db.execute("UPDATE deals SET old_price=?,new_price=?,discount_pct=?,created_at=? WHERE id=?",
                           (old_price, price, round(pct, 1), now, deal_id))
                if not existing_deal["slug"]:
                    db.execute("INSERT INTO short_links(deal_id,slug,created_at) VALUES(?,?,?)",
                               (deal_id, make_short_slug(), now))
            else:
                slug = make_short_slug()
                cart_disc = 1 if data.get("cart_discount") else 0
                cur = db.execute(
                    "INSERT INTO deals(product_id,old_price,new_price,discount_pct,active,status,cart_discount,created_at) "
                    "VALUES(?,?,?,?,0,'pending',?,?)",
                    (product_id, old_price, price, round(pct, 1), cart_disc, now)
                )
                deal_id = cur.lastrowid
                db.execute("INSERT INTO short_links(deal_id,slug,created_at) VALUES(?,?,?)", (deal_id, slug, now))

            db.commit()
            log.info(f"[webhook] 📋 Deal #{deal_id} onay bekliyor: #{product_id} %{pct:.1f}")

            if not existing_deal:
                try:
                    pending_count = db.execute("SELECT COUNT(*) FROM deals WHERE status='pending'").fetchone()[0]
                    prod_row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
                    if prod_row:
                        prod_dict = dict(prod_row)
                        prod_dict["deal_id"] = deal_id
                        await notify_pending_approval(deal_id, prod_dict, price, old_price, round(pct, 1), pending_count)
                except Exception as e:
                    log.warning(f"[webhook] Telegram bildirimi hatası: {e}")

                try:
                    import os as _os
                    site_url = _os.getenv("SITE_URL", "https://firsatvakti.com")
                    prod_t = db.execute("SELECT title FROM products WHERE id=?", (product_id,)).fetchone()
                    title_str = (prod_t["title"] or f"Ürün #{product_id}") if prod_t else f"Ürün #{product_id}"
                    group_row = db.execute("SELECT group_id FROM product_group_members WHERE product_id=?",
                                          (product_id,)).fetchone()
                    gids = [r["product_id"] for r in db.execute(
                        "SELECT product_id FROM product_group_members WHERE group_id=?",
                        (group_row["group_id"],)).fetchall()] if group_row else [product_id]
                    ph = ",".join("?" * len(gids))
                    watchers = db.execute(
                        f"SELECT DISTINCT u.email, u.username FROM product_watchlist pw "
                        f"JOIN users u ON pw.user_id=u.id WHERE pw.product_id IN ({ph})", gids
                    ).fetchall()
                    for w in watchers:
                        try:
                            send_price_alert(to=w["email"], username=w["username"], product_title=title_str,
                                            old_price=old_price, new_price=price, pct=pct,
                                            deal_url=f"{site_url}/deal/{deal_id}")
                        except Exception as e:
                            log.warning(f"[webhook] Email hatası ({w['email']}): {e}")
                except Exception as e:
                    log.warning(f"[webhook] Watchlist bildirimi hatası: {e}")

    for deal in db.execute("SELECT id, new_price FROM deals WHERE product_id=? AND active=1",
                           (product_id,)).fetchall():
        if price > deal["new_price"] * 1.02:
            db.execute("UPDATE deals SET active=0, status='expired', expires_at=? WHERE id=?", (now, deal["id"]))
            log.info(f"[webhook] ⏹ Deal #{deal['id']} kapandı — fiyat yükseldi")

    db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now, product_id))
    db.commit()


@app.get("/api/deals")
async def api_deals(platform: str = "", limit: int = 20):
    db = get_db(); w, p = "WHERE d.active=1", []
    if platform: w += " AND p.platform=?"; p.append(platform)
    return [dict(r) for r in db.execute(f"""
        SELECT d.id,d.old_price,d.new_price,d.discount_pct,d.created_at,p.title,p.image_url,p.platform,sl.slug
        FROM deals d JOIN products p ON d.product_id=p.id LEFT JOIN short_links sl ON sl.deal_id=d.id {w} ORDER BY d.created_at DESC LIMIT ?
    """, (*p, limit)).fetchall()]

@app.get("/api/stats")
async def api_stats():
    return dict(get_db().execute("""SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals,
        (SELECT COUNT(*) FROM products) as products, (SELECT COUNT(*) FROM clicks) as total_clicks,
        (SELECT COUNT(*) FROM product_groups) as comparisons""").fetchone())

@app.get("/health")
async def health():
    """Uptime monitörü / load balancer için sağlık kontrolü."""
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    return JSONResponse({"status": "ok" if db_status == "ok" else "degraded", "db": db_status})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
