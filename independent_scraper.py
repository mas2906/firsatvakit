#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# VPS FULL Scraper - HİÇBİR EXTERNAL DEPENDENCY YOK

import asyncio
import logging
import os
import sys
import random
import time
import re
import json
import httpx
import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] VPS-SCRAPER: %(message)s")
log = logging.getLogger("vps_scraper")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://firsatvakti:firsatvakti@localhost/firsatvakti")
WEBHOOK_URL = "http://127.0.0.1:8000/api/scraper-webhook"
WEBHOOK_KEY = "firsatvakti-webhook-key"
BATCH_SIZE = 30

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

def now():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# Minimal price parser
def parse_price(text: str) -> Optional[float]:
    t = re.sub(r"[^\d,.]", "", str(text or ""))
    if not t: return None
    t = t.replace(',', '.')
    try:
        v = float(t)
        return v if 10 < v < 1000000 else None
    except:
        return None

class SimpleRateLimiter:
    def __init__(self, delay: float):
        self.last = 0
        self.delay = delay

    async def wait(self):
        await asyncio.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()

limiter = SimpleRateLimiter(2.0)

def get_jobs():
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT sq.id, sq.product_id, p.source_url AS url, p.platform, sq.priority
        FROM scan_queue sq JOIN products p ON sq.product_id=p.id
        WHERE sq.status='pending'
        ORDER BY sq.priority ASC, sq.created_at ASC LIMIT 50
    """)
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

async def scrape_basic(url: str, platform: str) -> Dict[str, Any]:
    """Minimal scraper - sadece title + price + stock."""
    headers = {"User-Agent": random.choice(UA_POOL)}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}"}
            
            html = resp.text
            soup = BeautifulSoup(html, 'html.parser')
            
            # Title
            title = soup.title.string.strip()[:200] if soup.title else ""
            
            # Price (generic selectors)
            price = None
            for sel in ['ins', '.price', '[class*="price"]', '.new-price']:
                for el in soup.select(sel):
                    p = parse_price(el.get_text())
                    if p:
                        price = p
                        break
                if price: break
            
            # Stock
            stock_text = soup.get_text().lower()
            stock = "Stokta Var"
            if any(kw in stock_text for kw in ['stok yok', 'tükendi', 'out of stock']):
                stock = "Stok Yok"
            
            log.info(f"[{platform}] {title[:50]} price={price}")
            return {"title": title, "price": price, "stock": stock}
        except Exception as e:
            return {"error": str(e)}

async def worker():
    jobs = get_jobs()
    if not jobs:
        return
    
    log.info(f"TARAYICI BAŞLADI: {len(jobs)} iş")
    
    async with httpx.AsyncClient(timeout=20) as client:
        tasks = []
        for job in jobs:
            pid = job["product_id"]
            url = job["url"]
            plat = job["platform"]
            job_id = job["id"]
            
            async def process_one(pid=pid, url=url, plat=plat, job_id=job_id):
                await limiter.wait()
                data = await scrape_basic(url, plat)
                # Webhook
                payload = {"product_id": pid, "url": url, "platform": plat, "data": data}
                try:
                    r = await client.post(WEBHOOK_URL, json=payload, headers={"X-Webhook-Key": WEBHOOK_KEY})
                    log.info(f"[{pid}] {r.status_code}")
                    con = psycopg2.connect(DATABASE_URL)
                    cur = con.cursor()
                    cur.execute("UPDATE scan_queue SET status='done' WHERE id=%s", (job_id,))
                    con.commit()
                    con.close()
                except Exception as e:
                    log.error(f"[{pid}] webhook fail: {e}")
            
            tasks.append(process_one())
        
        await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    log.info("VPS INDEPENDENT FULL SCAPER - 8000 ÜRÜN!")
    while True:
        await worker()
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

