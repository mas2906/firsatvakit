#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WSL Local Scraper — her platform kendi bağımsız worker pool'unda çalışır

import os, sys, tempfile as _tempfile

# Playwright/Chromium geçici profillerini yönlendir
# Linux/WSL'de Windows yolu geçersiz — TMPDIR bozarsa Chrome SIGTRAP ile çöküyor
if sys.platform == "win32":
    _TMP = r"D:\firsatvakti_temp"
else:
    _TMP = "/tmp/firsatvakti_temp"
os.makedirs(_TMP, exist_ok=True)
os.environ["TEMP"] = _TMP
os.environ["TMP"] = _TMP
os.environ["TMPDIR"] = _TMP
_tempfile.tempdir = _TMP

import asyncio
import logging
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("local_scraper")

BASE_URL      = os.getenv("BASE_URL", "https://firsatvakti.com")
API_KEY       = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY   = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECS", "1"))

# Her platform bağımsız — birbirini bloklamaz
PLATFORM_CONFIG = {
    "amazon":      {"concurrent": 20, "batch": 25},
    "trendyol":    {"concurrent": 15, "batch": 20},
    "n11":         {"concurrent": 25, "batch": 30},
    "hepsiburada": {"concurrent": 6,  "batch": 8},
}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.router import scrape_product
from scrapers.cdp_base import BrowserPool


async def process_job(job, client, sem, pool):
    async with sem:
        pid        = job["product_id"]
        url        = job["url"]
        platform   = job["platform"]
        price_only = job.get("price_only", False)

        log.info(f"[{platform}] Scraping #{pid}{' [fiyat]' if price_only else ''}")
        _timeout = 60 if platform == "hepsiburada" else 30
        try:
            data = await asyncio.wait_for(
                scrape_product(url, platform, pool=pool, price_only=price_only),
                timeout=_timeout,
            )
            if data and data.get("price"):
                title = (data.get("title") or "")[:50]
                log.info(f"[{platform}] ✔ #{pid} fiyat={data['price']} stok={data.get('stock','-')} title={title!r}")
            else:
                log.warning(f"[{platform}] ✘ #{pid} veri yok — url={url[:60]}")
            await send_webhook(client, pid, url, platform, data or {})
        except asyncio.TimeoutError:
            log.error(f"[{platform}] TIMEOUT #{pid} — url={url[:60]}")
            await send_webhook(client, pid, url, platform, {"error": "timeout"})
        except Exception as e:
            log.error(f"[{platform}] HATA #{pid}: {e}")
            await send_webhook(client, pid, url, platform, {"error": str(e)})


async def poll_platform(platform: str, cfg: dict, client: httpx.AsyncClient, pool):
    """Tek platform için bağımsız polling döngüsü."""
    concurrent = cfg["concurrent"]
    batch      = cfg["batch"]
    sem        = asyncio.Semaphore(concurrent)
    running: set[asyncio.Task] = set()

    log.info(f"[{platform}] Worker başladı — concurrent={concurrent} batch={batch}")
    _empty_count = 0

    while True:
        try:
            running.difference_update({t for t in running if t.done()})
            free = concurrent - len(running)

            if free == 0:
                await asyncio.sleep(1)
                continue

            limit = min(free, batch)
            r = await client.get(
                f"{BASE_URL}/api/pending-jobs?platform={platform}&limit={limit}",
                headers={"X-Api-Key": API_KEY},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"[{platform}] Poll failed: {r.status_code}")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            jobs = r.json()
            if not jobs:
                _empty_count += 1
                if _empty_count % 10 == 1:
                    log.info(f"[{platform}] Kuyruk boş (poll #{_empty_count})")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            _empty_count = 0

            log.info(f"[{platform}] {len(jobs)} iş alındı ({len(running)} çalışıyor)")
            for job in jobs:
                task = asyncio.create_task(process_job(job, client, sem, pool))
                running.add(task)
                task.add_done_callback(running.discard)

            await asyncio.sleep(0.1)

        except Exception as e:
            log.error(f"[{platform}] Poll hatası: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def send_webhook(client, product_id, url, platform, data):
    payload = {"product_id": product_id, "url": url, "platform": platform, "data": data}
    try:
        r = await client.post(
            f"{BASE_URL}/api/scraper-webhook",
            json=payload,
            headers={"X-Webhook-Key": WEBHOOK_KEY},
            timeout=10,
        )
        log.info(f"[{platform}] Webhook {r.status_code} #{product_id}")
    except Exception as e:
        log.error(f"[{platform}] Webhook failed #{product_id}: {e}")


async def reset_stale_jobs(client: httpx.AsyncClient):
    """Başlangıçta 'processing' kalan sıkışık işleri 'pending'e döndür."""
    try:
        r = await client.post(
            f"{BASE_URL}/api/reset-stale-jobs",
            headers={"X-Api-Key": API_KEY},
            params={"force": "false"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            log.info(f"[startup] Sıkışık işler sıfırlandı: {data.get('reset', 0)} iş pending'e döndü")
        else:
            log.warning(f"[startup] reset-stale-jobs: {r.status_code}")
    except Exception as e:
        log.warning(f"[startup] reset-stale-jobs başarısız: {e}")


async def main():
    total_pages = sum(c["concurrent"] for c in PLATFORM_CONFIG.values())
    log.info(f"WSL Local Scraper başladı — {len(PLATFORM_CONFIG)} platform, toplam {total_pages} slot")
    for p, cfg in PLATFORM_CONFIG.items():
        log.info(f"  {p}: concurrent={cfg['concurrent']} batch={cfg['batch']}")

    pool = BrowserPool(max_pages=total_pages + 10)  # buffer
    try:
        await pool.start()
        log.info("[BrowserPool] Hazır")
        async with httpx.AsyncClient() as client:
            await reset_stale_jobs(client)
            await asyncio.gather(*[
                poll_platform(p, cfg, client, pool)
                for p, cfg in PLATFORM_CONFIG.items()
            ])
    finally:
        await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
