"""Admin paneli ve yönetim işlemleri."""
import os
import re as _re
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse

from affiliate import make_short_slug
from db import get_db
from scraper_router import (clean_tracking_params, detect_platform, enqueue_url,
                             is_short_url, resolve_short_url)
from telegram_pub import publish_deal

from .api import _dispatch_scrape, _run_cross_search
from .deps import _verify_csrf, cache_invalidate_index, current_user, now_str, require_admin, templates

router = APIRouter()

BULK_ADD_LIMIT = 200


def _delete_product_cascade(db, product_id: int) -> None:
    db.execute("DELETE FROM short_links WHERE deal_id IN (SELECT id FROM deals WHERE product_id=?)", (product_id,))
    db.execute("DELETE FROM clicks WHERE deal_id IN (SELECT id FROM deals WHERE product_id=?)", (product_id,))
    db.execute("DELETE FROM deal_publish_log WHERE deal_id IN (SELECT id FROM deals WHERE product_id=?)", (product_id,))
    db.execute("DELETE FROM deal_publish_log WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM deals WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM price_history WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM scan_queue WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM comment_votes WHERE comment_id IN (SELECT id FROM comments WHERE product_id=?)",
               (product_id,))
    db.execute("DELETE FROM comments WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM product_group_members WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM product_watchlist WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM article_products WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM scraper_errors WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM submit_log WHERE product_id=?", (product_id,))
    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    db.commit()


@router.get("/admin")
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
        (SELECT COUNT(*) FROM scraper_errors WHERE occurred_at::timestamp > NOW() - INTERVAL '24 hours') as errors_24h
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
    # CTE ile N+1 correlated subquery yerine tek geçişte cross-deal bilgisi
    pending_deals = db.execute("""
        WITH pd AS (
            SELECT d.id, d.product_id, p.platform
            FROM deals d JOIN products p ON d.product_id=p.id
            WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok')
            ORDER BY d.created_at DESC LIMIT 50
        ),
        cd AS (
            SELECT pgm1.product_id,
                   STRING_AGG(p2.platform || '|' || d2.id::text || '|' || d2.new_price::integer::text, ',') AS other_active_deals
            FROM product_group_members pgm1
            JOIN product_group_members pgm2 ON pgm1.group_id = pgm2.group_id AND pgm2.product_id != pgm1.product_id
            JOIN products p2 ON pgm2.product_id = p2.id
            JOIN deals d2 ON d2.product_id = p2.id AND d2.active = 1
            JOIN pd ON pd.product_id = pgm1.product_id AND p2.platform != pd.platform
            GROUP BY pgm1.product_id
        )
        SELECT d.*, p.title, p.platform, p.image_url, p.source_url, sl.slug, cd.other_active_deals
        FROM deals d
        JOIN products p ON d.product_id=p.id
        LEFT JOIN short_links sl ON sl.deal_id=d.id
        LEFT JOIN cd ON cd.product_id = d.product_id
        WHERE d.status='pending' AND (p.platform != 'amazon' OR p.stock != 'Stok Yok')
        ORDER BY d.created_at DESC LIMIT 50
    """).fetchall()
    recent_deals = db.execute(
        "SELECT d.*,p.title,p.platform FROM deals d JOIN products p ON d.product_id=p.id"
        " WHERE d.status!='pending' ORDER BY d.created_at DESC LIMIT 10"
    ).fetchall()
    if q:
        products = db.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count
            FROM products p
            WHERE p.title LIKE ? OR p.source_url LIKE ?
            ORDER BY p.id DESC LIMIT 200
        """, (f"%{q}%", f"%{q}%")).fetchall()
    else:
        products = db.execute(
            "SELECT p.*,(SELECT COUNT(*) FROM price_history ph WHERE ph.product_id=p.id) as price_count"
            " FROM products p ORDER BY p.id DESC LIMIT 10"
        ).fetchall()
    queue = db.execute("SELECT * FROM scan_queue ORDER BY created_at DESC LIMIT 10").fetchall()
    chart_raw = {r["platform"]: r["cnt"] for r in db.execute("""
        SELECT p.platform, COUNT(*) as cnt
        FROM deals d JOIN products p ON d.product_id=p.id
        WHERE d.active=1 GROUP BY p.platform
    """).fetchall()}
    _platforms = ["amazon", "trendyol", "n11", "hepsiburada"]
    chart_labels = [p.capitalize() for p in _platforms]
    chart_values = [chart_raw.get(p, 0) for p in _platforms]
    daily_raw = {r["day"]: r["cnt"] for r in db.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM deals WHERE created_at::timestamp >= NOW() - INTERVAL '7 days'
        GROUP BY 1 ORDER BY 1
    """).fetchall()}
    today = date.today()
    daily_labels = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    daily_values = [daily_raw.get(d, 0) for d in daily_labels]
    bulk_result = request.session.pop("bulk_result", None)
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "stats": stats,
        "pending_deals": pending_deals, "recent_deals": recent_deals,
        "products": products, "queue": queue,
        "wa_channel_url": os.getenv("WA_CHANNEL_URL", ""),
        "chart_labels": chart_labels, "chart_values": chart_values,
        "daily_labels": daily_labels, "daily_values": daily_values,
        "search_q": q, "pending_links": pending_links,
        "scraper_errors": scraper_errors, "bulk_result": bulk_result,
    })


@router.post("/admin/deal/{deal_id}/approve")
async def admin_approve_deal(deal_id: int, request: Request, affiliate_url: str = Form("")):
    require_admin(request)
    db = get_db()
    deal = db.execute(
        "SELECT d.*, p.title, p.image_url, p.platform FROM deals d JOIN products p ON d.product_id=p.id WHERE d.id=?",
        (deal_id,)
    ).fetchone()
    if not deal:
        return RedirectResponse("/admin", status_code=302)
    if deal["status"] == "approved":
        return RedirectResponse("/admin", status_code=302)
    aff = affiliate_url.strip() if affiliate_url.strip() else None
    db.execute("UPDATE deals SET active=1, status='approved', affiliate_url=? WHERE id=?", (aff, deal_id))
    db.commit()
    slug_row = db.execute("SELECT slug FROM short_links WHERE deal_id=?", (deal_id,)).fetchone()
    slug = slug_row["slug"] if slug_row else make_short_slug()
    product = db.execute("SELECT * FROM products WHERE id=?", (deal["product_id"],)).fetchone()
    prod_dict = dict(product)
    prod_dict["deal_id"] = deal_id
    await publish_deal(db, deal_id, prod_dict, deal["new_price"], deal["old_price"], deal["discount_pct"], slug)
    title = (prod_dict.get("title") or "Ürün").strip()[:80]
    pct = deal["discount_pct"]
    old_p = int(deal["old_price"])
    new_p = int(deal["new_price"])
    link = f"https://firsatvakti.com/go/{slug}"
    wa_text = f"🔥 %{int(pct)} İNDİRİM!\n\n🛍 {title}\n\n💰 {old_p:,} TL → {new_p:,} TL\n\n👉 {link}\n\n🌐 firsatvakti.com".replace(",", ".")
    cache_invalidate_index()
    from urllib.parse import quote
    print(f"[admin] ✔ Deal #{deal_id} onaylandı ve yayınlandı")
    return RedirectResponse(f"/admin?wa_text={quote(wa_text)}", status_code=302)


@router.post("/admin/deal/{deal_id}/reject")
async def admin_reject_deal(deal_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE deals SET active=0, status='rejected' WHERE id=?", (deal_id,))
    db.commit()
    cache_invalidate_index()
    print(f"[admin] ✘ Deal #{deal_id} reddedildi")
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/deals/reject-all-pending")
async def admin_reject_all_pending_deals(request: Request):
    """Onay bekleyen tüm fırsatları tek seferde reddet."""
    from fastapi.responses import JSONResponse
    require_admin(request)
    db = get_db()
    count = db.execute(
        "UPDATE deals SET active=0, status='rejected' WHERE status='pending'"
    ).rowcount
    db.commit()
    cache_invalidate_index()
    return JSONResponse({"rejected": count})


@router.post("/admin/clear-errors")
async def admin_clear_errors(request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM scraper_errors")
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/product/{product_id}/clear-errors")
async def admin_clear_product_errors(product_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM scraper_errors WHERE product_id=?", (product_id,))
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/link/{link_id}/approve")
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


@router.post("/admin/link/{link_id}/reject")
async def admin_reject_link(link_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE link_queue SET status='rejected', reviewed_at=? WHERE id=?", (now_str(), link_id))
    db.commit()
    print(f"[admin] ✘ Link #{link_id} reddedildi")
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/deal/{deal_id}/toggle")
async def admin_toggle_deal(deal_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE deals SET active=1-active WHERE id=?", (deal_id,))
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/deal/{deal_id}/delete")
async def admin_delete_deal(deal_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM short_links WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM clicks WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/group/{group_id}/remove/{product_id}")
async def admin_remove_from_group(group_id: int, product_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute(
        "UPDATE product_group_members SET match_type='removed', added_at=? WHERE group_id=? AND product_id=?",
        (now_str(), group_id, product_id)
    )
    remaining = db.execute(
        "SELECT COUNT(*) FROM product_group_members WHERE group_id=? AND match_type != 'removed'",
        (group_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM product_group_members WHERE group_id=?", (group_id,))
        db.execute("DELETE FROM product_groups WHERE id=?", (group_id,))
        db.commit()
        return RedirectResponse("/", status_code=302)
    db.commit()
    return RedirectResponse(f"/compare/{group_id}", status_code=302)


@router.post("/admin/product/{product_id}/delete")
async def admin_delete_product(product_id: int, request: Request):
    require_admin(request)
    db = get_db()
    _delete_product_cascade(db, product_id)
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/scan/run")
async def admin_run_scan(request: Request):
    require_admin(request)
    db = get_db()
    stale = db.execute("""
        UPDATE scan_queue SET status='pending', updated_at=?
        WHERE status='processing' AND updated_at::timestamp < NOW() - INTERVAL '5 minutes'
    """, (now_str(),)).rowcount
    failed = db.execute("""
        UPDATE scan_queue SET status='pending', updated_at=?
        WHERE status='failed'
    """, (now_str(),)).rowcount
    db.commit()
    print(f"[admin-reset] stale={stale} failed={failed} → pending'e alındı")
    return RedirectResponse("/admin", status_code=302)


@router.post("/admin/products/delete-errored")
async def admin_delete_errored_products(request: Request):
    """Hatalı linkleri sil: dead URL olarak işaretlenenler + 80+ scraper hatası birikmiş ürünler."""
    from fastapi.responses import JSONResponse
    require_admin(request)
    db = get_db()

    # scan_queue'da dead olarak işaretlenen ürünlerin ID'leri
    dead_ids = [r["product_id"] for r in db.execute(
        "SELECT DISTINCT product_id FROM scan_queue WHERE status='dead'"
    ).fetchall()]

    # 80+ scraper hatası birikmiş ürünlerin ID'leri
    error_ids = [r["product_id"] for r in db.execute(
        "SELECT product_id FROM scraper_errors GROUP BY product_id HAVING COUNT(*) >= 80"
    ).fetchall()]

    all_ids = list(set(dead_ids + error_ids))
    for pid in all_ids:
        _delete_product_cascade(db, pid)

    return JSONResponse({"deleted": len(all_ids)})


@router.post("/admin/bulk-add")
async def admin_bulk_add(request: Request, background_tasks: BackgroundTasks,
                         urls: str = Form("")):
    require_admin(request)
    db = get_db()
    lines = [u.strip() for u in urls.splitlines() if u.strip()]
    if len(lines) > BULK_ADD_LIMIT:
        lines = lines[:BULK_ADD_LIMIT]
    added, skipped, errors = 0, 0, []
    for raw_url in lines:
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
        "added": added, "skipped": skipped, "errors": errors, "total": len(lines),
    }
    return RedirectResponse("/admin#bulk-add", status_code=302)


@router.get("/admin/articles")
async def admin_articles(request: Request):
    from seo_content import list_articles
    user = require_admin(request)
    data = list_articles(get_db(), status=None, per_page=50)
    return templates.TemplateResponse("admin_articles.html", {
        "request": request, "user": user, "articles": data["articles"],
    })


@router.get("/admin/article/new")
async def admin_article_new(request: Request):
    require_admin(request)
    return templates.TemplateResponse("admin_article_edit.html", {
        "request": request, "user": current_user(request), "article": None,
    })


@router.post("/admin/article/save")
async def admin_article_save(request: Request, article_id: int = Form(0),
                              title: str = Form(...), summary: str = Form(""),
                              body_html: str = Form(...), category: str = Form("rehber"),
                              tags: str = Form(""), status: str = Form("draft"),
                              csrf_token: str = Form("")):
    from fastapi import HTTPException
    from seo_content import create_article, update_article
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
