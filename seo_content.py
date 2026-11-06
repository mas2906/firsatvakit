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
          AND p.platform NOT IN ('beymen','karaca')
        ORDER BY current_price ASC NULLS LAST
    """, (group_id,)).fetchall()
    products = [dict(m) for m in members]
    # Yalnızca Beymen/Karaca üyesi olan grup yok sayılır; görseli de onlardan gelmesin
    if not products and db.execute(
        "SELECT 1 FROM product_group_members m JOIN products p ON p.id=m.product_id "
        "WHERE m.group_id=? AND p.platform IN ('beymen','karaca') LIMIT 1", (group_id,)
    ).fetchone():
        return None
    priced = [p for p in products if p.get("current_price")]
    group = dict(g)
    if any(h in (group.get("image_url") or "") for h in ("karaca.com", "beymen.com")):
        group["image_url"] = next((p["image_url"] for p in products if p.get("image_url")), None)
    # ~%24 grubun adı boş — şablondaki |truncate None'da patlıyor ve sayfa 500
    # dönüyordu. İlk ürünün başlığını ad olarak kullan.
    if not group.get("name"):
        group["name"] = next((p["title"] for p in products if p.get("title")), None) or "Ürün"
    return {"group": group, "products": products,
            "cheapest": min(priced, key=lambda x: x["current_price"]) if priced else None,
            "expensive": max(priced, key=lambda x: x["current_price"]) if priced else None,
            "platform_count": len(set(p["platform"] for p in products))}


def _cut(text: str, n: int) -> str:
    """Başlığı kelime ortasından değil, son boşluktan keser ("Kırmız" yerine "Kırmızı"
    kelimesini tamamen atar)."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" -–—,;:/(&") or text[:n]


def _loc(platform_name: str) -> str:
    """Bulunma eki: Amazon'da, Trendyol'da, Hepsiburada'da ama N11'de."""
    return "'de" if platform_name.lower() in ("n11", "beymen") else "'da"


def build_meta_tags(page_type, data=None):
    d = data or {}
    if page_type == "deal":
        t = d.get("title") or "Fırsat"; pct = d.get("discount_pct") or 0; pl = (d.get("platform") or "").title()
        new_p = d.get("new_price"); old_p = d.get("old_price")
        price_str = f"{new_p:.0f} TL (Normal: {old_p:.0f} TL). " if new_p and old_p else ""
        desc = (f"{_cut(t, 70)} {pl}{_loc(pl)} %{pct:.0f} indirimde! {price_str}"
                f"En ucuz fiyatı, fiyat geçmişi ve anlık indirim bildirimi FırsatVakti'de.")
        return {"title": f"%{pct:.0f} İndirim: {_cut(t, 55)} En Ucuz Fiyatı | FırsatVakti",
                "description": desc[:160],
                "og_image": d.get("image_url") or "",
                "og_type": "product"}
    if page_type == "product":
        t = d.get("title") or "Ürün"; pl = (d.get("platform") or "").title()
        return {"title": f"{_cut(t, 60)} En Ucuz Fiyatı ve Fiyat Geçmişi | FırsatVakti",
                "description": (f"{_cut(t, 80)} en ucuz fiyatı, {pl} fiyat geçmişi ve özellikleri. "
                                f"Fiyat düşünce anlık indirim bildirimi al.")[:160],
                "og_image": d.get("image_url") or ""}
    if page_type == "comparison":
        n = d.get("name") or "Karşılaştırma"; pc = d.get("platform_count") or 0
        return {"title": f"{_cut(n, 55)} En Ucuz Fiyat Karşılaştırma ({pc} Mağaza) | FırsatVakti",
                "description": (f"{_cut(n, 70)} en ucuz nerede? Amazon, Trendyol, Hepsiburada ve N11 fiyatlarını "
                                f"karşılaştır, fiyat geçmişini gör, en uygun fiyata al.")[:160],
                "og_image": d.get("image_url") or ""}
    if page_type == "article":
        return {"title": f"{(d.get('title') or '')[:60]} | FırsatVakti Blog",
                "description": (d.get("summary") or "")[:160], "og_image": d.get("cover_image") or "",
                "og_type": "article"}
    if page_type == "index":
        platform_names = {"amazon": "Amazon", "trendyol": "Trendyol", "hepsiburada": "Hepsiburada", "n11": "N11"}
        pl = platform_names.get(d.get("platform") or "")
        page = d.get("page") or 1
        suffix = f" — Sayfa {page}" if page > 1 else ""
        if pl:
            return {"title": f"{pl} İndirimleri, Kampanyaları ve Günün Fırsatları{suffix} | FırsatVakti",
                    "description": (f"{pl}{_loc(pl)}ki anlık indirimler, sıcak fırsatlar ve kampanyalar. "
                                    f"Fiyat geçmişiyle doğrulanmış gerçek indirimler, en ucuz fiyatlar ve ücretsiz fiyat takibi.")[:160],
                    "og_image": ""}
        return {"title": f"Anlık İndirimler ve Günün Fırsatları{suffix} | FırsatVakti",
                "description": ("Amazon, Trendyol, Hepsiburada ve N11'deki anlık indirimler, sıcak fırsatlar ve kampanyalar. "
                                "Fiyat geçmişi, en ucuz fiyat ve ücretsiz Telegram/WhatsApp indirim bildirimi."),
                "og_image": ""}
    return {"title": "FırsatVakti — En Ucuz Fiyatlar, Gerçek İndirimler",
            "description": "Amazon, Trendyol, Hepsiburada, N11 fiyat takibi ve karşılaştırma. Ücretsiz fırsat bildirimi.",
            "og_image": ""}


def generate_sitemap_xml(db, domain="https://firsatvakti.com"):
    from xml.sax.saxutils import escape
    urls = [
        {"loc": f"{domain}/", "priority": "1.0", "changefreq": "hourly"},
        *({"loc": f"{domain}/?platform={pl}", "priority": "0.9", "changefreq": "hourly"}
          for pl in ("amazon", "trendyol", "hepsiburada", "n11")),
        {"loc": f"{domain}/dipte", "priority": "0.9", "changefreq": "hourly"},
        {"loc": f"{domain}/blog", "priority": "0.7", "changefreq": "daily"},
        *({"loc": f"{domain}/{pg}", "priority": "0.3", "changefreq": "yearly"}
          for pg in ("about", "faq", "contact", "privacy", "terms")),
    ]
    # Deal sayfaları — ürün başına SADECE en yeni deal (deal.html canonical'ı da
    # oraya işaret ediyor; eski deal id'leri aynı ürünün kopya sayfaları).
    # 'expired' da dahil: süresi dolmuş fırsat, fiyat geçmişi sayfası olarak değerli.
    for d in db.execute("""
        SELECT d.id, d.created_at, d.active FROM deals d
        JOIN (SELECT product_id, MAX(id) AS max_id FROM deals
              WHERE status IN ('approved','expired') GROUP BY product_id) l ON l.max_id = d.id
        ORDER BY d.id DESC LIMIT 5000
    """).fetchall():
        urls.append({"loc": f"{domain}/deal/{d['id']}", "lastmod": (d["created_at"] or "")[:10],
                     "priority": "0.9" if d["active"] else "0.5",
                     "changefreq": "daily" if d["active"] else "weekly"})
    # Karşılaştırma sayfaları
    for g in db.execute("SELECT pg.id,pg.updated_at FROM product_groups pg WHERE (SELECT COUNT(*) FROM product_group_members m JOIN products mp ON mp.id=m.product_id WHERE m.group_id=pg.id AND mp.platform NOT IN ('beymen','karaca'))>=2 ORDER BY pg.id DESC LIMIT 5000").fetchall():
        urls.append({"loc": f"{domain}/compare/{g['id']}", "lastmod": (g["updated_at"] or "")[:10], "priority": "0.7", "changefreq": "weekly"})
    # Ürün sayfaları — deal'ı olan ürünler /deal/{id}'ye redirect ediyor, sitemap'e
    # redirect eden URL koymak Search Console'da "Yönlendirmeli sayfa" hatası üretir
    for p in db.execute("""
        SELECT p.id, p.last_seen_at FROM products p
        WHERE p.title IS NOT NULL AND p.title <> ''
          AND p.platform NOT IN ('beymen','karaca')
          AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.product_id = p.id
                          AND d.status IN ('approved','expired'))
        ORDER BY p.id DESC LIMIT 30000
    """).fetchall():
        urls.append({"loc": f"{domain}/product/{p['id']}", "lastmod": (p["last_seen_at"] or "")[:10], "priority": "0.6", "changefreq": "daily"})
    # Blog makaleleri
    for a in db.execute("SELECT slug,published_at FROM articles WHERE status='published' LIMIT 2000").fetchall():
        urls.append({"loc": f"{domain}/blog/{a['slug']}", "lastmod": (a["published_at"] or "")[:10], "priority": "0.6", "changefreq": "monthly"})

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u in urls:
        xml += f'  <url><loc>{escape(u["loc"])}</loc>'
        if u.get("lastmod"): xml += f'<lastmod>{u["lastmod"]}</lastmod>'
        if u.get("changefreq"): xml += f'<changefreq>{u["changefreq"]}</changefreq>'
        xml += f'<priority>{u.get("priority","0.5")}</priority></url>\n'
    return xml + '</urlset>'


def _slug(title):
    tr = str.maketrans({"ç":"c","ğ":"g","ı":"i","ö":"o","ş":"s","ü":"u","Ç":"c","Ğ":"g","İ":"i","Ö":"o","Ş":"s","Ü":"u"})
    s = title.lower().translate(tr)
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    return re.sub(r'[\s-]+', '-', s).strip('-')[:80]
