#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEO İçerik — makale, karşılaştırma, sitemap, meta tag — v2."""

import re
from datetime import datetime
from typing import Optional


def create_article(db, title, body_html, author_id, category="rehber",
                   tags="", summary="", cover_image=None, product_ids=None,
                   group_ids=None, status="draft"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    slug = _slug(title)
    if db.execute("SELECT id FROM articles WHERE slug=?", (slug,)).fetchone():
        slug = f"{slug}-{int(datetime.utcnow().timestamp())%10000}"
    pub = now if status == "published" else None

    cur = db.execute("""INSERT INTO articles(slug,title,summary,body_html,cover_image,category,tags,
                        author_id,status,published_at,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (slug,title,summary,body_html,cover_image,category,tags,author_id,status,pub,now,now))
    aid = cur.lastrowid
    for i, pid in enumerate(product_ids or []):
        db.execute("INSERT OR IGNORE INTO article_products(article_id,product_id,sort_order) VALUES(?,?,?)", (aid,pid,i))
    for i, gid in enumerate(group_ids or []):
        db.execute("INSERT OR IGNORE INTO article_products(article_id,group_id,sort_order) VALUES(?,?,?)",
                   (aid,gid,i+len(product_ids or [])))
    db.commit()
    return {"ok": True, "article_id": aid, "slug": slug}


def update_article(db, article_id, **kw):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    allowed = ["title","summary","body_html","cover_image","category","tags","status"]
    sets, params = [], []
    for k, v in kw.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); params.append(v)
    if not sets: return {"ok": False}
    if kw.get("status") == "published":
        a = db.execute("SELECT published_at FROM articles WHERE id=?", (article_id,)).fetchone()
        if a and not a["published_at"]:
            sets.append("published_at=?"); params.append(now)
    sets.append("updated_at=?"); params.append(now); params.append(article_id)
    db.execute(f"UPDATE articles SET {','.join(sets)} WHERE id=?", params)
    db.commit()
    return {"ok": True}


def get_article_by_slug(db, slug):
    a = db.execute("SELECT a.*,u.username as author_name FROM articles a LEFT JOIN users u ON a.author_id=u.id WHERE a.slug=?", (slug,)).fetchone()
    if not a: return None
    r = dict(a)
    r["products"] = [dict(p) for p in db.execute("""
        SELECT ap.*,p.title,p.image_url,p.platform,p.source_url,
               (SELECT ph.price_value FROM price_history ph WHERE ph.product_id=p.id ORDER BY ph.id DESC LIMIT 1) as current_price
        FROM article_products ap LEFT JOIN products p ON ap.product_id=p.id WHERE ap.article_id=? ORDER BY ap.sort_order
    """, (a["id"],)).fetchall()]
    db.execute("UPDATE articles SET views=views+1 WHERE id=?", (a["id"],)); db.commit()
    return r


def list_articles(db, category=None, status="published", page=1, per_page=12):
    offset = (page-1)*per_page
    w, p = "WHERE 1=1", []
    if status: w += " AND a.status=?"; p.append(status)
    if category: w += " AND a.category=?"; p.append(category)
    rows = db.execute(f"SELECT a.*,u.username as author_name FROM articles a LEFT JOIN users u ON a.author_id=u.id {w} ORDER BY a.published_at DESC LIMIT ? OFFSET ?",
                      (*p, per_page, offset)).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM articles a {w}", p).fetchone()[0]
    return {"articles": [dict(r) for r in rows], "total": total, "page": page,
            "total_pages": max(1,(total+per_page-1)//per_page)}


def generate_comparison_data(db, group_id):
    g = db.execute("SELECT * FROM product_groups WHERE id=?", (group_id,)).fetchone()
    if not g: return None
    members = db.execute("""
        SELECT p.*,pgm.confidence,
               (SELECT ph.price_value FROM price_history ph WHERE ph.product_id=p.id ORDER BY ph.id DESC LIMIT 1) as current_price,
               (SELECT MIN(ph.price_value) FROM price_history ph WHERE ph.product_id=p.id) as min_price,
               (SELECT MAX(ph.price_value) FROM price_history ph WHERE ph.product_id=p.id) as max_price,
               (SELECT d.cart_discount FROM deals d WHERE d.product_id=p.id ORDER BY d.id DESC LIMIT 1) as cart_discount
        FROM product_group_members pgm JOIN products p ON pgm.product_id=p.id
        WHERE pgm.group_id=?
          AND (p.stock IS NULL OR p.stock != 'Stok Yok')
        ORDER BY current_price ASC NULLS LAST
    """, (group_id,)).fetchall()
    products = [dict(m) for m in members]
    priced = [p for p in products if p.get("current_price")]
    return {"group": dict(g), "products": products,
            "cheapest": min(priced, key=lambda x: x["current_price"]) if priced else None,
            "expensive": max(priced, key=lambda x: x["current_price"]) if priced else None,
            "platform_count": len(set(p["platform"] for p in products))}


def build_meta_tags(page_type, data=None):
    d = data or {}
    if page_type == "deal":
        t = d.get("title","Fırsat"); pct = d.get("discount_pct",0); pl = d.get("platform","").title()
        new_p = d.get("new_price"); old_p = d.get("old_price")
        price_str = f"{new_p:.0f} TL (Normal: {old_p:.0f} TL). " if new_p and old_p else ""
        desc = f"{t[:80]} — {pl}'da %{pct:.0f} indirim! {price_str}Fiyat geçmişi ve takibi FırsatVakti'de."
        return {"title": f"%{pct:.0f} İndirim — {t[:55]} | FırsatVakti",
                "description": desc[:160],
                "og_image": d.get("image_url",""),
                "og_type": "product"}
    if page_type == "product":
        t = d.get("title","Ürün"); pl = d.get("platform","").title()
        return {"title": f"{t[:60]} Fiyat Takibi | FırsatVakti",
                "description": f"{t[:100]} için {pl} fiyat geçmişi, indirim takibi ve fırsat bildirimi.",
                "og_image": d.get("image_url","")}
    if page_type == "comparison":
        n = d.get("name","Karşılaştırma"); pc = d.get("platform_count",0)
        return {"title": f"{n[:60]} — {pc} Platformda Karşılaştırma | FırsatVakti",
                "description": f"{n[:80]}: Amazon, Trendyol, N11, Hepsiburada fiyatlarını karşılaştırın ve en ucuzunu bulun.",
                "og_image": d.get("image_url","")}
    if page_type == "article":
        return {"title": f"{d.get('title','')[:60]} | FırsatVakti Blog",
                "description": d.get("summary","")[:160], "og_image": d.get("cover_image","")}
    if page_type == "index":
        return {"title": "FırsatVakti — Amazon, Trendyol, N11 Fırsat ve İndirim Takibi",
                "description": "Amazon, Trendyol, Hepsiburada ve N11'den gerçek indirimler. Günlük kampanyalar, fiyat düşüş bildirimi ve ücretsiz fırsat takibi.",
                "og_image": ""}
    return {"title": "FırsatVakti — En Ucuz Fiyatlar, Gerçek İndirimler",
            "description": "Amazon, Trendyol, Hepsiburada, N11 fiyat takibi ve karşılaştırma. Ücretsiz fırsat bildirimi.",
            "og_image": ""}


def generate_sitemap_xml(db, domain="https://firsatvakti.com"):
    urls = [
        {"loc": domain, "priority": "1.0", "changefreq": "hourly"},
        {"loc": f"{domain}/blog", "priority": "0.7", "changefreq": "daily"},
    ]
    # Aktif deal'lar — en yüksek öncelik
    for d in db.execute("SELECT id,created_at FROM deals WHERE active=1 AND status='approved' ORDER BY created_at DESC LIMIT 500").fetchall():
        urls.append({"loc": f"{domain}/deal/{d['id']}", "lastmod": d["created_at"][:10], "priority": "0.9", "changefreq": "daily"})
    # Sona ermiş ama onaylı deal'lar — fiyat geçmişi sayfası olarak değerli
    for d in db.execute("SELECT id,created_at FROM deals WHERE active=0 AND status='approved' ORDER BY created_at DESC LIMIT 300").fetchall():
        urls.append({"loc": f"{domain}/deal/{d['id']}", "lastmod": d["created_at"][:10], "priority": "0.5", "changefreq": "monthly"})
    # Karşılaştırma sayfaları
    for g in db.execute("SELECT pg.id,pg.updated_at FROM product_groups pg WHERE (SELECT COUNT(*) FROM product_group_members WHERE group_id=pg.id)>=2 LIMIT 200").fetchall():
        urls.append({"loc": f"{domain}/compare/{g['id']}", "lastmod": (g["updated_at"] or "")[:10], "priority": "0.7", "changefreq": "weekly"})
    # Ürün sayfaları
    for p in db.execute("SELECT id,last_seen_at FROM products ORDER BY id DESC LIMIT 500").fetchall():
        urls.append({"loc": f"{domain}/product/{p['id']}", "lastmod": (p["last_seen_at"] or "")[:10], "priority": "0.6", "changefreq": "weekly"})
    # Blog makaleleri
    for a in db.execute("SELECT slug,published_at FROM articles WHERE status='published' LIMIT 200").fetchall():
        urls.append({"loc": f"{domain}/blog/{a['slug']}", "lastmod": (a["published_at"] or "")[:10], "priority": "0.6", "changefreq": "monthly"})

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.sitemaps.org/schemas/sitemap/0.9 http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd">\n'
    for u in urls:
        xml += f'  <url><loc>{u["loc"]}</loc>'
        if u.get("lastmod"): xml += f'<lastmod>{u["lastmod"]}</lastmod>'
        if u.get("changefreq"): xml += f'<changefreq>{u["changefreq"]}</changefreq>'
        xml += f'<priority>{u.get("priority","0.5")}</priority></url>\n'
    return xml + '</urlset>'


def _slug(title):
    tr = str.maketrans({"ç":"c","ğ":"g","ı":"i","ö":"o","ş":"s","ü":"u","Ç":"c","Ğ":"g","İ":"i","Ö":"o","Ş":"s","Ü":"u"})
    s = title.lower().translate(tr)
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    return re.sub(r'[\s-]+', '-', s).strip('-')[:80]
