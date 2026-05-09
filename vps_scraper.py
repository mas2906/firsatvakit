#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# VPS Scraper - deal_loop + full_loop (8000+ ürün için optimize)

import asyncio
import logging
import os
import sys
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] scraper: %(message)s")
log = logging.getLogger("vps_scraper")

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
WEBHOOK_URL = f"{BASE_URL}/api/scraper-webhook"
PENDING_URL = f"{BASE_URL}/api/pending-jobs"
API_KEY  = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
BATCH_SIZE = 50

PLATFORM_DELAY = {"amazon": 2.0, "trendyol": 1.5, "hepsiburada": 4.0, "n11": 2.5}
MAX_CONCURRENT = 4

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.utils_v2 import RateLimiter, parse_price_tr_clean, normalize_image_url, detect_cart_discount

def now():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

async def get_deals(client):
    """Aktif deal ürünleri (priority 0)."""
    try:
        r = await client.get(PENDING_URL, params={"priority": 0, "limit": 20},
                             headers={"X-Api-Key": API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log.error(f"[get_deals] {e}")
        return []

async def get_full(client):
    """Full scan işleri."""
    try:
        r = await client.get(PENDING_URL, params={"limit": 100},
                             headers={"X-Api-Key": API_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log.error(f"[get_full] {e}")
        return []

async def webhook(client, job_id, pid, url, platform, data):
    payload = {"product_id": pid, "url": url, "platform": platform, "data": data}
    try:
        r = await client.post(WEBHOOK_URL, json=payload, headers={"X-Webhook-Key": WEBHOOK_KEY}, timeout=8)
        log.info(f"[{pid}] webhook {r.status_code}")
    except Exception as e:
        log.error(f"[{pid}] webhook ERROR: {e}")

async def scrape_one(client, job):
    pid, url, platform, job_id = job["product_id"], job["source_url"], job["platform"], job["id"]
    
    log.info(f"[{pid}] {platform}")
    try:
        data = await asyncio.wait_for(scrape_product(url, platform), timeout=60)
        await webhook(client, job_id, pid, url, platform, data or {"error": "no_data"})
    except Exception as e:
        log.error(f"[{pid}] scrape ERROR: {e}")
        await webhook(client, job_id, pid, url, platform, {"error": str(e)})

async def deal_loop(client):
    """Priority 0 jobs (aktif deal'lar)."""
    while True:
        jobs = await get_deals(client)
        if jobs:
            log.info(f"DEAL LOOP: {len(jobs)} jobs")
            await asyncio.gather(*(scrape_one(client, j) for j in jobs), return_exceptions=True)
        await asyncio.sleep(3)

async def full_loop(client):
    """Priority >0 jobs (tam tarama)."""
    while True:
        jobs = await get_full(client)
        if jobs:
            log.info(f"FULL LOOP: {len(jobs)} jobs (batch {BATCH_SIZE})")
            sem = asyncio.Semaphore(MAX_CONCURRENT)
            tasks = []
            for j in jobs[:BATCH_SIZE]:
                async def wrapped(j=j):
                    async with sem:
                        await scrape_one(client, j)
                tasks.append(wrapped())
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(10)

async def main():
    log.info(f"VPS Scraper v3 - batch:{BATCH_SIZE}")
    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(deal_loop(client), full_loop(client))

if __name__ == "__main__":
    asyncio.run(main())

