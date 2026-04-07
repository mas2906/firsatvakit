#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
firsatvakti.com — Ana FastAPI uygulaması v2
Yenilikler: bcrypt güvenlik, çapraz platform arama, yorum sistemi, SEO blog, sitemap
"""

import os, json, secrets, string, time
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from email_utils import send_password_reset

SCRAPER_SERVICE_URL = os.getenv("SCRAPER_SERVICE_URL", "")
SCRAPER_SERVICE_KEY = os.getenv("SCRAPER_SERVICE_KEY", "")
WEBHOOK_KEY         = os.getenv("FRONTEND_WEBHOOK_KEY", "")
USE_LOCAL_SCRAPER   = os.getenv("USE_LOCAL_SCRAPER", "false").lower() == "true"
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
from telegram_pub import publish_deal
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

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), max_age=86400 * 7)

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
                        subject: str = Form(""), message: str = Form("")):
    user = current_user(request)
    if not name or not email or not message:
        return templates.TemplateResponse("contact.html", {
            "request": request, "user": user, "error": "Tüm alanları doldurun."})
    # E-posta gönder
    from email_utils import send_email
    admin_email = os.getenv("ADMIN_EMAIL", os.getenv("SMTP_USER", ""))
    html_body = f"<p><b>Ad:</b> {name}</p><p><b>E-posta:</b> {email}</p><p><b>Mesaj:</b><br>{message}</p>"
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
    else: order = {"newest":"d.created_at DESC","discount":"d.discount_pct DESC","price":"d.new_price ASC"}.get(sort,"d.created_at DESC")

    deals = db.execute(f"""
        SELECT d.*, p.title, p.image_url, p.rating, p.review_count, p.platform, p.stock, p.last_seen_at,
               (SELECT COUNT(*) FROM clicks c WHERE c.deal_id=d.id) as click_count
        FROM deals d LEFT JOIN products p ON d.product_id = p.id
        INNER JOIN (SELECT product_id, MAX(id) as max_id FROM deals WHERE active=1 GROUP BY product_id) best ON d.id = best.max_id
        {where.replace("WHERE","AND")} ORDER BY {order} LIMIT ? OFFSET ?
    """, (*params, per_page, offset)).fetchall()

    total = db.execute(f"SELECT COUNT(DISTINCT d.product_id) FROM deals d LEFT JOIN products p ON d.product_id=p.id {where}", params).fetchone()[0]
    stats = db.execute("SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals, (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops, (SELECT COUNT(*) FROM product_watchlist) as watchlist_count, (SELECT COUNT(*) FROM products) as tracked_products_count, (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count, (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count, (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count, (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count").fetchone()

    user = current_user(request)
    # Kullanıcının min indirim oranını al
    user_min_discount = 5
    if user:
        ensure_notify_table(db)
        ns = db.execute("SELECT min_discount FROM notify_settings WHERE user_id=?", (user["id"],)).fetchone()
        if ns: user_min_discount = ns["min_discount"]

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

    meta = build_meta_tags("index")
    return templates.TemplateResponse("index.html", {
        "request": request, "deals": deals, "platform": platform, "sort": sort,
        "tab": tab, "page": page, "total_pages": max(1,(total+per_page-1)//per_page),
        "stats": stats, "user": user, "q": "",
        "ticker_items": get_ticker_items(db), "meta": meta,
        "user_min_discount": user_min_discount,
        "new_deals": new_deals, "popular_deals": popular_deals,
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

    stats = db.execute("SELECT (SELECT COUNT(*) FROM deals WHERE active=1) as active_deals, (SELECT COUNT(*) FROM deals WHERE active=1 AND DATE(created_at)=DATE('now')) as today_drops, (SELECT COUNT(*) FROM product_watchlist) as watchlist_count, (SELECT COUNT(*) FROM products) as tracked_products_count, (SELECT COUNT(*) FROM products WHERE platform='amazon') as amazon_count, (SELECT COUNT(*) FROM products WHERE platform='trendyol') as trendyol_count, (SELECT COUNT(*) FROM products WHERE platform='n11') as n11_count, (SELECT COUNT(*) FROM products WHERE platform='hepsiburada') as hepsiburada_count").fetchone()
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
                      url: str = Form(...), cross_search_platforms: str = Form("")):
    user = current_user(request)
    db = get_db()
    ip = request.client.host if request.client else "unknown"
    if user and user["role"] == "admin": count, limit = 0, 999999
    elif user: count = get_today_submit_count(db, user_id=user["id"]); limit = MEMBER_DAILY_LIMIT
    else: count = get_today_submit_count(db, ip=ip); limit = GUEST_DAILY_LIMIT
    remaining = max(0, limit - count)

    url = url.strip()
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

    product_id = enqueue_url(db, url, platform)
    await _dispatch_scrape(product_id, url, platform, background_tasks)

    # v2: Çapraz platform arama
    if cross_search_platforms:
        plats = [p.strip() for p in cross_search_platforms.split(",") if p.strip()]
        background_tasks.add_task(_run_cross_search, product_id, plats)

    log_submit(db, user_id=user["id"] if user else None, ip=ip, product_id=product_id)
    return templates.TemplateResponse("submit.html", {
        "request": request, "success": f"Link eklendi! {platform.title()} taranıyor...",
        "product_id": product_id, "product_url": f"/product/{product_id}",
        "user": user, "remaining": max(0,limit-count-1), "limit": limit,
    })

async def _run_cross_search(product_id, platforms=None):
    import asyncio
    await asyncio.sleep(10)
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
async def register_post(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
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
async def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    db = get_db()
    ip = request.client.host if request.client else "unknown"

    allowed, wait = check_login_allowed(email)
    if not allowed:
        return templates.TemplateResponse("auth.html", {"request": request, "mode": "login",
            "error": f"Çok fazla hatalı deneme. {wait//60} dk sonra tekrar dene."})

    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
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
# ŞİFRE SIFIRLAMA
# ═══════════════════════════════════════════════════════════════

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(request: Request, email: str = Form(...)):
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
                               password: str = Form(...), password2: str = Form(...)):
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
                              tags: str = Form(""), status: str = Form("draft")):
    user = require_admin(request)
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

# ═══════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════

def ensure_notify_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notify_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE REFERENCES users(id),
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
        SELECT p.*, ph.price_value as current_price,
               d.discount_pct, d.new_price, d.old_price, d.active as deal_active,
               sl.submitted_at
        FROM submit_log sl
        JOIN products p ON sl.product_id = p.id
        LEFT JOIN (SELECT product_id, MAX(price_value) as price_value FROM price_history GROUP BY product_id) ph ON ph.product_id = p.id
        LEFT JOIN (SELECT product_id, discount_pct, new_price, old_price, active FROM deals WHERE active=1 ORDER BY id DESC LIMIT 1) d ON d.product_id = p.id
        WHERE sl.user_id=? AND sl.product_id IS NOT NULL
        GROUP BY p.id
        ORDER BY sl.submitted_at DESC LIMIT 30
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
        (SELECT COUNT(*) FROM deals WHERE status='pending') as pending_deals,
        (SELECT COUNT(*) FROM users) as users, (SELECT COUNT(*) FROM clicks) as clicks,
        (SELECT COUNT(*) FROM scan_queue WHERE status='pending') as queue_pending,
        (SELECT COUNT(*) FROM comments) as comments,
        (SELECT COUNT(*) FROM articles) as articles,
        (SELECT COUNT(*) FROM product_groups) as groups
    """).fetchone()
    # Onay bekleyen deal'lar
    pending_deals = db.execute("""
        SELECT d.*, p.title, p.platform, p.image_url, p.source_url, sl.slug
        FROM deals d JOIN products p ON d.product_id=p.id
        LEFT JOIN short_links sl ON sl.deal_id=d.id
        WHERE d.status='pending'
        ORDER BY d.created_at DESC LIMIT 50
    """).fetchall()
    recent_deals = db.execute("SELECT d.*,p.title,p.platform FROM deals d JOIN products p ON d.product_id=p.id WHERE d.status!='pending' ORDER BY d.created_at DESC LIMIT 30").fetchall()
    if q:
        products = db.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count
            FROM products p
            WHERE p.title LIKE ? OR p.source_url LIKE ?
            ORDER BY p.id DESC LIMIT 200
        """, (f"%{q}%", f"%{q}%")).fetchall()
    else:
        products = db.execute("SELECT p.*,(SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count FROM products p ORDER BY p.id DESC LIMIT 100").fetchall()
    queue = db.execute("SELECT * FROM scan_queue ORDER BY created_at DESC LIMIT 30").fetchall()
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

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "stats": stats,
        "pending_deals": pending_deals,
        "recent_deals": recent_deals, "products": products, "queue": queue,
        "wa_channel_url": os.getenv("WA_CHANNEL_URL", ""),
        "chart_labels": chart_labels, "chart_values": chart_values,
        "daily_labels": daily_labels, "daily_values": daily_values,
        "search_q": q,
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

@app.post("/admin/product/{product_id}/delete")
async def admin_delete_product(product_id: int, request: Request):
    require_admin(request); db = get_db()
    dids = [r["id"] for r in db.execute("SELECT id FROM deals WHERE product_id=?", (product_id,)).fetchall()]
    for did in dids:
        db.execute("DELETE FROM short_links WHERE deal_id=?", (did,))
        db.execute("DELETE FROM clicks WHERE deal_id=?", (did,))
    db.execute("DELETE FROM deals WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM price_history WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM comments WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM product_group_members WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM products WHERE id=?", (product_id,)); db.commit()
    return RedirectResponse("/admin", status_code=302)

# ── Scan state (admin progress tracking) ──────────────────────
_SCAN_STATE: dict = {"running": False, "current": 0, "total": 0, "title": "", "platform": "", "done": 0, "failed": 0, "errors": []}

@app.get("/api/scan-status")
async def scan_status(request: Request):
    require_admin(request)
    return JSONResponse(_SCAN_STATE)

async def _admin_scan_with_progress():
    """Admin paneli için ilerleme takipli tarama."""
    import asyncio
    from scrapers.router import scrape_product
    global _SCAN_STATE

    db = get_db()
    pending = db.execute("""
        SELECT sq.product_id, p.source_url, p.platform, p.title
        FROM scan_queue sq JOIN products p ON p.id = sq.product_id
        WHERE sq.status IN ('pending','failed')
        ORDER BY sq.priority ASC, sq.created_at ASC
        LIMIT 200
    """).fetchall()

    # Aktif ürünler de ekle
    active = db.execute("""
        SELECT id as product_id, source_url, platform, title FROM products
        ORDER BY last_seen_at ASC LIMIT 200
    """).fetchall()

    all_items = list(pending) + [r for r in active if r["product_id"] not in {p["product_id"] for p in pending}]
    total = len(all_items)

    _SCAN_STATE.update({"running": True, "current": 0, "total": total, "title": "", "platform": "", "done": 0, "failed": 0, "errors": []})

    for i, row in enumerate(all_items, 1):
        pid      = row["product_id"]
        url      = row["source_url"]
        platform = row["platform"]
        title    = row["title"] or url[:60]

        _SCAN_STATE.update({"current": i, "title": title, "platform": platform})

        try:
            data = await asyncio.wait_for(scrape_product(url, platform), timeout=75)
            if data:
                await _save_scraped_data(pid, data)
                _SCAN_STATE["done"] += 1
                db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now_str(), pid))
            else:
                _SCAN_STATE["failed"] += 1
                _SCAN_STATE["errors"].append({"pid": pid, "title": title, "platform": platform, "reason": "Veri alınamadı"})
                db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?", (now_str(), pid))
            db.commit()
        except asyncio.TimeoutError:
            _SCAN_STATE["failed"] += 1
            _SCAN_STATE["errors"].append({"pid": pid, "title": title, "platform": platform, "reason": "Zaman aşımı (75s)"})
            db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?", (now_str(), pid))
            db.commit()
            print(f"[admin-scan] Timeout #{pid}")
        except Exception as e:
            _SCAN_STATE["failed"] += 1
            _SCAN_STATE["errors"].append({"pid": pid, "title": title, "platform": platform, "reason": str(e)[:120]})
            print(f"[admin-scan] Hata #{pid}: {e}")

        await asyncio.sleep(1.5)

    _SCAN_STATE["running"] = False
    print(f"[admin-scan] Tamamlandı — {_SCAN_STATE['done']} başarılı / {_SCAN_STATE['failed']} başarısız")

@app.post("/admin/scan/run")
async def admin_run_scan(request: Request, background_tasks: BackgroundTasks):
    require_admin(request)
    if not _SCAN_STATE["running"]:
        background_tasks.add_task(_admin_scan_with_progress)
    return RedirectResponse("/admin?scan=started", status_code=302)

# ═══════════════════════════════════════════════════════════════
# ARKA PLAN TARAMA
# ═══════════════════════════════════════════════════════════════

async def _dispatch_scrape(product_id: int, url: str, platform: str, background_tasks):
    """Scraper servis varsa oraya gönder, USE_LOCAL_SCRAPER=true ise kuyruğa bırak, yoksa lokal çalıştır."""
    if SCRAPER_SERVICE_URL:
        background_tasks.add_task(_remote_scrape, product_id, url, platform)
    elif USE_LOCAL_SCRAPER:
        # local_scraper.py ayrı süreçte çalışıp /api/pending-jobs'tan alacak
        pass
    else:
        background_tasks.add_task(scrape_and_save, product_id, url, platform)


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
    if SCRAPER_SERVICE_KEY and key != SCRAPER_SERVICE_KEY:
        raise HTTPException(401, "Geçersiz API anahtarı")
    db = get_db()
    rows = db.execute("""
        SELECT sq.product_id, p.source_url, p.platform
        FROM scan_queue sq
        JOIN products p ON p.id = sq.product_id
        WHERE sq.status = 'pending'
        ORDER BY sq.created_at ASC
        LIMIT 10
    """).fetchall()
    # Alınan işleri "processing" olarak işaretle (tekrar alınmasın)
    ids = [r["product_id"] for r in rows]
    if ids:
        db.execute(
            f"UPDATE scan_queue SET status='processing', updated_at=? WHERE product_id IN ({','.join('?'*len(ids))})",
            [now_str()] + ids
        )
        db.commit()
    return JSONResponse([{"product_id": r["product_id"], "url": r["source_url"], "platform": r["platform"]} for r in rows])


@app.post("/api/scraper-webhook")
async def scraper_webhook(request: Request):
    """Scraper servisi (VPS) bu endpoint'e sonuçları gönderir."""
    key = request.headers.get("X-Webhook-Key", "")
    if WEBHOOK_KEY and key != WEBHOOK_KEY:
        raise HTTPException(401, "Geçersiz webhook anahtarı")

    body = await request.json()
    product_id = body.get("product_id")
    data       = body.get("data", {})
    url        = body.get("url", "")
    platform   = body.get("platform", "")

    if not product_id or not data:
        raise HTTPException(400, "product_id ve data gerekli")

    if data.get("error"):
        db = get_db()
        db.execute("UPDATE scan_queue SET status='failed',updated_at=? WHERE product_id=?",
                   (now_str(), product_id))
        db.commit()
        return {"ok": False, "error": data["error"]}

    # scrape_and_save'in veri kaydetme kısmını çalıştır
    await _save_scraped_data(product_id, data)
    return {"ok": True}


async def _save_scraped_data(product_id: int, data: dict):
    """Scraping sonucunu veritabanına kaydet (hem lokal hem webhook için ortak)."""
    db = get_db()
    db.execute("""UPDATE products SET title=COALESCE(?,title), image_url=COALESCE(?,image_url),
        description=COALESCE(?,description), rating=COALESCE(?,rating),
        review_count=COALESCE(?,review_count), brand=COALESCE(?,brand),
        barcode=COALESCE(?,barcode), stock=COALESCE(?,stock), last_seen_at=? WHERE id=?""",
               (data.get("title"), data.get("image_url"), data.get("description"),
                data.get("rating"), data.get("review_count"), data.get("brand"),
                data.get("barcode"), data.get("stock"), now_str(), product_id))

    price = data.get("price")
    if price:
        prev = db.execute("SELECT price_value FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 1",
                          (product_id,)).fetchone()
        if prev and prev["price_value"] == price:
            db.commit()
            return
        db.execute("INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,?,?)",
                   (product_id, price, "TRY", now_str()))
        if prev and prev["price_value"] and price < prev["price_value"]:
            pct = (prev["price_value"] - price) / prev["price_value"] * 100
            if pct >= 5:
                existing = db.execute("SELECT id FROM deals WHERE product_id=? AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1",
                                      (product_id,)).fetchone()
                if existing:
                    db.execute("UPDATE deals SET old_price=?,new_price=?,discount_pct=?,created_at=? WHERE id=?",
                               (prev["price_value"], price, round(pct, 1), now_str(), existing["id"]))
                else:
                    slug = make_short_slug()
                    cur = db.execute("INSERT INTO deals(product_id,old_price,new_price,discount_pct,active,status,created_at) VALUES(?,?,?,?,0,'pending',?)",
                                     (product_id, prev["price_value"], price, round(pct, 1), now_str()))
                    db.execute("INSERT INTO short_links(deal_id,slug,created_at) VALUES(?,?,?)",
                               (cur.lastrowid, slug, now_str()))
                db.commit()

        for deal in db.execute("SELECT id, new_price FROM deals WHERE product_id=? AND active=1",
                                (product_id,)).fetchall():
            if price > deal["new_price"] * 1.02:
                db.execute("UPDATE deals SET active=0, status='expired', expires_at=? WHERE id=?",
                           (now_str(), deal["id"]))

    db.execute("UPDATE scan_queue SET status='done',updated_at=? WHERE product_id=?", (now_str(), product_id))
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
