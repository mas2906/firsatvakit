"""Statik sayfalar, blog, sitemap, ayarlar, yorum API'leri."""
import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from comments import add_comment, delete_comment, get_product_comments, vote_comment
from db import get_db
from seo_content import (build_meta_tags, generate_sitemap_xml, get_article_by_slug,
                          list_articles)

from .deps import _is_rate_limited, _verify_csrf, current_user, now_str, require_admin, templates

router = APIRouter()


# ── Statik sayfalar ───────────────────────────────────────────

@router.get("/about")
async def about(request: Request):
    return templates.TemplateResponse("about.html", {"request": request, "user": current_user(request)})


@router.get("/faq")
async def faq(request: Request):
    return templates.TemplateResponse("faq.html", {"request": request, "user": current_user(request)})


@router.get("/privacy")
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request, "user": current_user(request)})


@router.get("/terms")
async def terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request, "user": current_user(request)})


@router.get("/contact")
async def contact_get(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request, "user": current_user(request)})


@router.post("/contact")
async def contact_post(request: Request, name: str = Form(""), email: str = Form(""),
                       subject: str = Form(""), message: str = Form(""), csrf_token: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip, "contact"):
        return templates.TemplateResponse("contact.html", {"request": request,
                                                           "user": current_user(request),
                                                           "error": "Çok fazla istek. Lütfen bekleyin."})
    if not _verify_csrf(request, csrf_token):
        return templates.TemplateResponse("contact.html", {"request": request,
                                                           "user": current_user(request),
                                                           "error": "Geçersiz istek. Sayfayı yenileyip tekrar dene."})
    user = current_user(request)
    if not name or not email or not message:
        return templates.TemplateResponse("contact.html", {
            "request": request, "user": user, "error": "Tüm alanları doldurun."})
    from email_utils import send_email
    from html import escape as _he
    admin_email = os.getenv("ADMIN_EMAIL", os.getenv("SMTP_USER", ""))
    html_body = (f"<p><b>Ad:</b> {_he(name)}</p>"
                 f"<p><b>E-posta:</b> {_he(email)}</p>"
                 f"<p><b>Mesaj:</b><br>{_he(message)}</p>")
    send_email(admin_email, f"[FırsatVakti İletişim] {subject}", html_body)
    return templates.TemplateResponse("contact.html", {"request": request, "user": user, "sent": True})


# ── Yorum API'leri ────────────────────────────────────────────

@router.post("/api/comment")
async def api_add_comment(request: Request, product_id: int = Form(...),
                           body: str = Form(...), rating: int = Form(None),
                           parent_id: int = Form(None)):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yap")
    result = add_comment(get_db(), product_id, user["id"], body, rating, parent_id)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return RedirectResponse(request.headers.get("referer", f"/product/{product_id}"), status_code=302)


@router.get("/api/comments/{product_id}")
async def api_get_comments(product_id: int, page: int = 1, sort: str = "newest"):
    return get_product_comments(get_db(), product_id, page=page, sort=sort)


@router.post("/api/comment/{comment_id}/vote")
async def api_vote(comment_id: int, request: Request, vote: int = Form(...)):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yap")
    result = vote_comment(get_db(), comment_id, user["id"], vote)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/comment/{comment_id}/delete")
async def api_del_comment(comment_id: int, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yap")
    result = delete_comment(get_db(), comment_id, user["id"], user["role"] == "admin")
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return RedirectResponse(request.headers.get("referer", "/"), status_code=302)


# ── Blog ──────────────────────────────────────────────────────

@router.get("/blog")
async def blog_list_page(request: Request, category: str = "", page: int = 1):
    data = list_articles(get_db(), category=category or None, page=page)
    return templates.TemplateResponse("blog_list.html", {
        "request": request, "user": current_user(request),
        "articles": data["articles"], "total_pages": data["total_pages"],
        "page": page, "category": category,
    })


@router.get("/blog/{slug}")
async def blog_detail_page(request: Request, slug: str):
    article = get_article_by_slug(get_db(), slug)
    if not article:
        raise HTTPException(404, "Yazı bulunamadı")
    meta = build_meta_tags("article", article)
    return templates.TemplateResponse("blog_detail.html", {
        "request": request, "user": current_user(request), "article": article, "meta": meta,
    })


# ── Sitemap & Robots ──────────────────────────────────────────

@router.get("/sitemap.xml")
async def sitemap():
    return Response(content=generate_sitemap_xml(get_db()), media_type="application/xml")


@router.get("/robots.txt")
async def robots():
    return Response(
        content="User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nSitemap: https://firsatvakti.com/sitemap.xml\n",
        media_type="text/plain"
    )


# ── Ayarlar ───────────────────────────────────────────────────

def ensure_notify_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notify_settings (
        id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE REFERENCES users(id),
        tg_chat_id TEXT DEFAULT '', wa_phone TEXT DEFAULT '', min_discount INTEGER DEFAULT 10,
        platforms TEXT DEFAULT 'amazon,trendyol,n11', tg_active INTEGER DEFAULT 0,
        wa_active INTEGER DEFAULT 0, updated_at TEXT)""")
    db.commit()


@router.get("/settings")
async def settings_get(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    db = get_db()
    ensure_notify_table(db)
    tg = db.execute("SELECT * FROM tg_subscriptions WHERE user_id=?", (user["id"],)).fetchone()
    notify = db.execute("SELECT * FROM notify_settings WHERE user_id=?", (user["id"],)).fetchone()
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
        "notify": notify, "tracked": tracked, "watchlist_count": watchlist_count,
    })


@router.post("/settings/telegram")
async def settings_tg(request: Request, tg_chat_id: str = Form(""), wa_phone: str = Form(""),
                       min_discount: int = Form(10), tg_active: str = Form(""),
                       wa_active: str = Form(""), platforms: list = Form([])):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    db = get_db()
    ensure_notify_table(db)
    plat_str = ",".join(platforms) if platforms else "amazon,trendyol,n11"
    tg_on, wa_on = 1 if tg_active else 0, 1 if wa_active else 0
    db.execute(
        "INSERT INTO notify_settings(user_id,tg_chat_id,wa_phone,min_discount,platforms,tg_active,wa_active,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET tg_chat_id=excluded.tg_chat_id,"
        "wa_phone=excluded.wa_phone,min_discount=excluded.min_discount,platforms=excluded.platforms,"
        "tg_active=excluded.tg_active,wa_active=excluded.wa_active,updated_at=excluded.updated_at",
        (user["id"], tg_chat_id.strip(), wa_phone.strip(), min_discount, plat_str, tg_on, wa_on, now_str())
    )
    if tg_on and tg_chat_id.strip():
        db.execute(
            "INSERT INTO tg_subscriptions(user_id,chat_id,min_discount_pct,platforms,active,updated_at)"
            " VALUES(?,?,?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,"
            "min_discount_pct=excluded.min_discount_pct,platforms=excluded.platforms,active=1,updated_at=excluded.updated_at",
            (user["id"], tg_chat_id.strip(), min_discount, plat_str, now_str())
        )
    else:
        db.execute("UPDATE tg_subscriptions SET active=0 WHERE user_id=?", (user["id"],))
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=302)
