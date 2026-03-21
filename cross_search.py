#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Çapraz Platform Ürün Arama & Fiyat Karşılaştırma — v2."""

import re, asyncio
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}


async def cross_search_product(db, product_id: int, search_platforms: list = None):
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product or not product["title"]:
        return

    src = product["platform"]
    all_p = ["amazon", "trendyol", "n11", "hepsiburada"]
    targets = [p for p in (search_platforms or all_p) if p != src]

    brand = product["brand"] if "brand" in product.keys() else None
    query = _build_query(product["title"], brand)
    group_id = _get_or_create_group(db, product_id, product)

    print(f"[cross] '{query}' → {targets}")
    for plat in targets:
        try:
            results = await _search(plat, query)
            if results:
                _save_match(db, group_id, results[0], plat)
                print(f"[cross] ✔ {plat}: {results[0].get('title','?')[:50]}")
            else:
                print(f"[cross] ✘ {plat}: yok")
        except Exception as e:
            print(f"[cross] ✘ {plat}: {e}")
        await asyncio.sleep(1.5)


def _build_query(title: str, brand: str = None) -> str:
    noise = ["fiyatı","fiyati","indirim","kampanya","ücretsiz kargo","orijinal","garantili"]
    q = title.lower()
    for w in noise: q = q.replace(w, "")
    words = [w for w in q.split() if len(w) > 1][:8]
    return " ".join(words).strip()


def _get_or_create_group(db, pid, product) -> int:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute("SELECT group_id FROM product_group_members WHERE product_id=?", (pid,)).fetchone()
    if row: return row["group_id"]

    cur = db.execute("INSERT INTO product_groups(name,image_url,category,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (product["title"], product.get("image_url"), product.get("category"), now, now))
    gid = cur.lastrowid
    db.execute("INSERT OR IGNORE INTO product_group_members(group_id,product_id,match_type,confidence,added_at) VALUES(?,?,'source',1.0,?)",
               (gid, pid, now))
    db.commit()
    return gid


def _save_match(db, group_id, result, platform):
    from scraper_router import clean_tracking_params
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    url = result.get("url","")
    if not url: return
    clean = clean_tracking_params(url, platform)

    cur = db.execute("INSERT OR IGNORE INTO products(platform,asin_or_id,source_url,title,image_url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                     (platform, result.get("product_id"), clean, result.get("title"), result.get("image_url"), now, now))
    db.commit()
    pid = cur.lastrowid or (db.execute("SELECT id FROM products WHERE source_url=?", (clean,)).fetchone() or {}).get("id")
    if not pid: return

    price = result.get("price")
    if price and price > 0:
        db.execute("INSERT INTO price_history(product_id,price_value,currency,scraped_at) VALUES(?,?,'TRY',?)", (pid, price, now))

    db.execute("INSERT OR IGNORE INTO product_group_members(group_id,product_id,match_type,confidence,added_at) VALUES(?,?,'auto_title',0.7,?)",
               (group_id, pid, now))
    db.execute("INSERT INTO scan_queue(product_id,url,platform,status,priority,created_at) VALUES(?,?,?,'pending',2,?)",
               (pid, clean, platform, now))
    db.commit()


# ══ Platform Arama ════════════════════════════════════════════

async def _search(platform, query) -> list:
    if platform == "trendyol":   return await _search_trendyol(query)
    if platform == "amazon":     return await _search_amazon(query)
    if platform == "n11":        return await _search_n11(query)
    if platform == "hepsiburada":return await _search_hb(query)
    return []


async def _search_trendyol(q) -> list:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://www.trendyol.com/sr?q={quote_plus(q)}", headers=HEADERS)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select(".p-card-wrppr")[:5]:
        try:
            a = item.select_one("a")
            if not a: continue
            href = a.get("href","")
            if not href.startswith("http"): href = f"https://www.trendyol.com{href}"
            t = item.select_one(".prdct-desc-cntnr-name, span[class*='prdct-desc']")
            p = item.select_one(".prc-box-dscntd, .prc-box-sllng")
            m = re.search(r"-p-(\d+)", href)
            out.append({"url": href.split("?")[0], "title": t.get_text(strip=True) if t else None,
                         "price": _price(p.get_text() if p else None), "product_id": m.group(1) if m else None})
        except: pass
    return out


async def _search_amazon(q) -> list:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://www.amazon.com.tr/s?k={quote_plus(q)}", headers=HEADERS)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select("[data-asin]")[:5]:
        asin = item.get("data-asin")
        if not asin or len(asin) != 10: continue
        t = item.select_one("h2 a span, .a-text-normal")
        pw = item.select_one(".a-price-whole")
        pf = item.select_one(".a-price-fraction")
        img = item.select_one("img.s-image")
        price = None
        if pw:
            ps = pw.get_text(strip=True).replace(".","").replace(",",".")
            if pf: ps += pf.get_text(strip=True)
            try: price = float(ps)
            except: pass
        out.append({"url": f"https://www.amazon.com.tr/dp/{asin}",
                     "title": t.get_text(strip=True) if t else None,
                     "price": price, "product_id": asin,
                     "image_url": img.get("src") if img else None})
    return out


async def _search_n11(q) -> list:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://www.n11.com/arama?q={quote_plus(q)}", headers=HEADERS)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select(".columnContent .plink, li.column")[:5]:
        try:
            a = item if item.name == "a" else item.select_one("a")
            if not a: continue
            t = item.select_one(".productName, h3.productName")
            p = item.select_one(".newPrice ins, .price ins, .newPrice")
            out.append({"url": a.get("href",""), "title": t.get_text(strip=True) if t else None,
                         "price": _price(p.get_text() if p else None)})
        except: pass
    return out


async def _search_hb(q) -> list:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://www.hepsiburada.com/ara?q={quote_plus(q)}", headers=HEADERS)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select("[data-test-id='product-card-item'], li[class*='productListContent']")[:5]:
        try:
            a = item.select_one("a[href*='/']")
            if not a: continue
            href = a.get("href","")
            if not href.startswith("http"): href = f"https://www.hepsiburada.com{href}"
            t = item.select_one("h3, span[data-test-id='product-card-name']")
            p = item.select_one("[data-test-id='price-current-price'], [class*='price-value']")
            out.append({"url": href.split("?")[0], "title": t.get_text(strip=True) if t else None,
                         "price": _price(p.get_text() if p else None)})
        except: pass
    return out


# ══ Fiyat Karşılaştırma ══════════════════════════════════════

def get_price_comparison(db, product_id: int) -> dict:
    member = db.execute("SELECT group_id FROM product_group_members WHERE product_id=?", (product_id,)).fetchone()
    if not member:
        return {"group": None, "products": [], "cheapest": None, "savings": None}

    group = db.execute("SELECT * FROM product_groups WHERE id=?", (member["group_id"],)).fetchone()
    members = db.execute("""
        SELECT p.*, pgm.confidence,
               (SELECT ph.price_value FROM price_history ph WHERE ph.product_id=p.id ORDER BY ph.id DESC LIMIT 1) as current_price
        FROM product_group_members pgm
        JOIN products p ON pgm.product_id = p.id
        WHERE pgm.group_id=?
        ORDER BY current_price ASC NULLS LAST
    """, (member["group_id"],)).fetchall()

    products = [dict(m) for m in members]
    priced = [p for p in products if p.get("current_price")]
    cheapest = min(priced, key=lambda x: x["current_price"]) if priced else None
    expensive = max(priced, key=lambda x: x["current_price"]) if priced else None
    savings = None
    if cheapest and expensive and cheapest["id"] != expensive["id"]:
        diff = expensive["current_price"] - cheapest["current_price"]
        savings = {"amount": round(diff,2),
                   "percent": round(diff/expensive["current_price"]*100, 1),
                   "cheapest_platform": cheapest["platform"]}

    return {"group": dict(group) if group else None, "products": products,
            "cheapest": cheapest, "savings": savings}


def _price(text) -> Optional[float]:
    if not text: return None
    text = re.sub(r'[^\d.,]', '', text.strip())
    if not text: return None
    if "," in text and "." in text: text = text.replace(".","").replace(",",".")
    elif "," in text: text = text.replace(",",".")
    try: return float(text)
    except: return None
