#!/usr/bin/env python3
import logging
import random
import httpx
import re
import os
from datetime import datetime
import asyncio
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("PERFECT-SCRAPER")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://firsatvakti:firsatvakti@localhost/firsatvakti")
WEBHOOK_URL = "http://127.0.0.1:8000/api/scraper-webhook"

UA_POOL = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def parse_price(text):
    t = re.sub(r"[^\d.,]", "", str(text))
    t = t.replace(',', '.')
    try:
        v = float(t)
        return v if 10 < v < 1000000 else None
    except:
        return None

def get_pending():
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, product_id, source_url, platform FROM scan_queue WHERE status='pending' ORDER BY priority ASC LIMIT 20")
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
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}"}
            title = r.text.split('<title>')[1].split('</title>')[0] if '<title>' in r.text else "No title"
            price_match = re.search(r'price["\']?\s*[:=]\s*["\']?([\d.,]+)', r.text)
            price = parse_price(price_match.group(1)) if price_match else None
            log.info(f"✓ {title[:50]} price={price}")
            return {"title": title[:200], "price": price, "stock": "Stokta Var"}
    except Exception as e:
        log.error(f"✘ {e}")
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
    log.info("PERFECT VPS SCAPER - BAŞLADI!")
    while True:
        rows = get_pending()
        if not rows:
            log.info("Kuyruk boş - 5s bekle")
            await asyncio.sleep(5)
            continue
        log.info(f"TARANIYOR: {len(rows)}")
        tasks = []
        for row in rows:
            tasks.append(webhook(row[1], row[2], row[3], await scrape(row[2]), row[0]))
        await asyncio.gather(*tasks)
        log.info("BATCH TAMAM")

if __name__ == "__main__":
    asyncio.run(main())

