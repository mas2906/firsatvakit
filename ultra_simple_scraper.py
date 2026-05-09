#!/usr/bin/env python3
# ULTRA SIMPLE - SADECE httpx + psycopg2 + logging

import logging
import random
import httpx
import re
import os
from datetime import datetime
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ultra_scraper")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://firsatvakti:firsatvakti@localhost/firsatvakti")
WEBHOOK_URL = "http://127.0.0.1:8000/api/scraper-webhook"

UA_POOL = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def parse_price(text):
    t = re.sub(r"[^\d.,]", "", str(text))
    if ',' in t: t = t.replace(',', '.')
    try:
        return float(t) if 10 < float(t) < 1000000 else None
    except:
        return None

def get_pending():
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, product_id, source_url, platform FROM scan_queue
        WHERE status='pending' ORDER BY priority ASC, created_at ASC LIMIT 20
    """)
    rows = cur.fetchall()
    con.close()
    return rows

def mark_done(job_id):
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    cur.execute("UPDATE scan_queue SET status='done' WHERE id=%s", (job_id,))
    con.commit()
    con.close()

async def scrape(url):
    headers = {"User-Agent": random.choice(UA_POOL)}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                title = r.text[:200].strip()
                price = parse_price(r.text)
                log.info(f"✓ {title[:50]} price={price}")
                return {"title": title, "price": price, "stock": "Stokta Var"}
            else:
                return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        log.error(f"✘ scrape fail: {e}")
        return {"error": str(e)}

async def webhook(pid, url, platform, data, job_id):
    payload = {"product_id": pid, "url": url, "platform": platform, "data": data}
    headers = {"X-Webhook-Key": "firsatvakti-webhook-key"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(WEBHOOK_URL, json=payload, headers=headers)
            log.info(f"[{pid}] webhook {r.status_code}")
            mark_done(job_id)
    except Exception as e:
        log.error(f"[{pid}] webhook fail: {e}")

async def main():
    log.info("ULTRA SIMPLE SCAPER - 8000 ÜRÜN BAŞLIYOR!")
    while True:
        rows = get_pending()
        if not rows:
            log.info("Kuyruk boş - 10s bekle")
            await asyncio.sleep(10)
            continue

        log.info(f"TARANIYOR: {len(rows)} iş")
        tasks = []
        for row in rows:
            job_id, pid, url, platform = row
            tasks.append(webhook(pid, url, platform, await scrape(url), job_id))
        
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("BATCH TAMAM")

if __name__ == "__main__":
    asyncio.run(main())

