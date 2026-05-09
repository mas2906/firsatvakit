#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[KULLANILMIYOR] FırsatVakti — VPS Scraper Worker v2 (eski)
Bu dosya local_scraper.py (WSL) + scan_queue mimarisi ile değiştirilmiştir.
Silinebilir.
"""

import asyncio
import logging
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scraper_worker")

# ── Konfigürasyon ────────────────────────────────────────────────
BASE_URL           = os.getenv("BASE_URL", "http://127.0.0.1:8000")
WEBHOOK_URL        = f"{BASE_URL}/api/scraper-webhook"
PENDING_URL        = f"{BASE_URL}/api/pending-jobs"
API_KEY            = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY        = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
FULL_LOOP_SKIP_MIN = int(os.getenv("FULL_LOOP_SKIP_MINUTES", "10"))
BATCH_SIZE         = 100

PLATFORM_CONCURRENCY = {"amazon": 2, "trendyol": 3, "hepsiburada": 1, "n11": 1}
PLATFORM_MIN_DELAY   = {"amazon": 1.5, "trendyol": 1.0, "hepsiburada": 4.0, "n11": 2.0}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.router import scrape_product
from scrapers.cdp_base import BrowserPool


def _now():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def get_deal_products(client: httpx.AsyncClient) -> list:
    """Aktif deal işleri (priority 0)."""
    try:
        r = await client.get(PENDING_URL, params={"priority": 0, "limit": 20},
                             headers={"X-Api-Key": API_KEY}, timeout=10)
        jobs = r.json() if r.status_code == 200 else []
        return [{"id": j["product_id"], "url": j["url"], "platform": j["platform"]} for j in jobs]
    except Exception as e:
        log.error(f"[api] get_deal_products hata: {e}")
        return []


async def get_full_products(client: httpx.AsyncClient) -> list:
    """Tüm bekleyen işler."""
    try:
        r = await client.get(PENDING_URL, params={"limit": BATCH_SIZE},
                             headers={"X-Api-Key": API_KEY}, timeout=10)
        jobs = r.json() if r.status_code == 200 else []
        return [{"id": j["product_id"], "url": j["url"], "platform": j["platform"]} for j in jobs]
    except Exception as e:
        log.error(f"[api] get_full_products hata: {e}")
        return []


async def send_webhook(client: httpx.AsyncClient, product_id: int,
                       url: str, platform: str, data: dict):
    payload = {
        "product_id": product_id,
        "url":        url,
        "platform":   platform,
        "data":       data,
        "scraped_at": _now(),
    }
    try:
        r = await client.post(
            WEBHOOK_URL,
            json=payload,
            headers={"X-Webhook-Key": WEBHOOK_KEY},
            timeout=15,
        )
        if r.status_code == 200:
            log.info(f"[webhook] ✔ pid={product_id} ({platform})")
        else:
            log.warning(f"[webhook] HTTP {r.status_code} pid={product_id}: {r.text[:100]}")
    except Exception as e:
        log.error(f"[webhook] ✘ pid={product_id}: {e}")


async def scrape_one(client: httpx.AsyncClient, product: dict,
                     global_sem: asyncio.Semaphore,
                     platform_sems: dict,
                     platform_last_ts: dict,
                     pool: BrowserPool = None):
    pid      = product["id"]
    url      = product["url"]
    platform = product["platform"]

    p_sem     = platform_sems.get(platform, asyncio.Semaphore(1))
    min_delay = PLATFORM_MIN_DELAY.get(platform, 2.0)

    async with global_sem:
        async with p_sem:
            last = platform_last_ts.get(platform, 0.0)
            wait = min_delay - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            platform_last_ts[platform] = time.monotonic()

            log.info(f"[scraper] #{pid} {platform} {url[:70]}")
            try:
                data = await asyncio.wait_for(
                    scrape_product(url, platform, pool=pool), timeout=75
                )
                if data:
                    log.info(f"[scraper] ✔ #{pid} fiyat={data.get('price')}")
                    await send_webhook(client, pid, url, platform, data)
                else:
                    log.warning(f"[scraper] ✘ #{pid} veri alınamadı")
                    await send_webhook(client, pid, url, platform, {"error": "scraping_failed"})
            except asyncio.TimeoutError:
                log.warning(f"[scraper] ⏱ #{pid} timeout 75s")
                await send_webhook(client, pid, url, platform, {"error": "timeout"})
            except Exception as e:
                log.error(f"[scraper] ✘ #{pid} hata: {e}")
                await send_webhook(client, pid, url, platform, {"error": str(e)})


async def deal_loop(client: httpx.AsyncClient,
                    global_sem: asyncio.Semaphore,
                    platform_sems: dict,
                    platform_last_ts: dict,
                    pool: BrowserPool = None):
    """Aktif deal ürünlerini sürekli tara — bitince hemen yeniden başla."""
    log.info("[deal_loop] başladı")
    while True:
        products = get_deal_products()
        if not products:
            log.debug("[deal_loop] aktif deal yok, 30s bekleniyor")
            await asyncio.sleep(30)
            continue
        log.info(f"[deal_loop] {len(products)} aktif deal ürünü tarıyor")
        await asyncio.gather(*[
            scrape_one(client, p, global_sem, platform_sems, platform_last_ts, pool)
            for p in products
        ])


async def full_loop(client: httpx.AsyncClient,
                    global_sem: asyncio.Semaphore,
                    platform_sems: dict,
                    platform_last_ts: dict,
                    pool: BrowserPool = None):
    """Tüm DB ürünlerini sürekli tara — bitince hemen yeniden başla."""
    log.info("[full_loop] başladı")
    while True:
        products = get_full_products()
        if not products:
            log.debug("[full_loop] tüm ürünler tarandı, 60s bekleniyor")
            await asyncio.sleep(60)
            continue
        log.info(f"[full_loop] {len(products)} ürün tarıyor (batch={BATCH_SIZE})")
        for i in range(0, len(products), BATCH_SIZE):
            batch = products[i:i + BATCH_SIZE]
            await asyncio.gather(*[
                scrape_one(client, p, global_sem, platform_sems, platform_last_ts, pool)
                for p in batch
            ])


async def main():
    log.info("VPS Scraper Worker v2 başladı (CDP mimarisi)")
    log.info(f"DB: {DB_PATH} | full_loop_skip: {FULL_LOOP_SKIP_MIN}dk | "
             f"concurrency: {PLATFORM_CONCURRENCY}")

    global_sem       = asyncio.Semaphore(sum(PLATFORM_CONCURRENCY.values()))
    platform_sems    = {p: asyncio.Semaphore(c) for p, c in PLATFORM_CONCURRENCY.items()}
    platform_last_ts: dict = {}

    pool = BrowserPool(max_pages=6)
    await pool.start()

    try:
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                deal_loop(client, global_sem, platform_sems, platform_last_ts, pool),
                full_loop(client, global_sem, platform_sems, platform_last_ts, pool),
            )
    finally:
        await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
