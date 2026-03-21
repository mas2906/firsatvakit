#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FırsatVakti — Local Scraper
Kendi bilgisayarında çalışır, VPS'deki ana uygulamadan iş alır ve sonuçları gönderir.

Kurulum:
  pip install -r requirements.txt
  playwright install chromium

Kullanım:
  python3 local_scraper.py

.env dosyasında şunları ayarla:
  SITE_URL=https://firsatvakti.com        # VPS'deki sitenin adresi
  SCRAPER_SERVICE_KEY=gizli-anahtar      # API anahtarı (her iki tarafta aynı olmalı)
  FRONTEND_WEBHOOK_KEY=gizli-anahtar    # Webhook anahtarı (her iki tarafta aynı olmalı)
"""

import asyncio
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

SITE_URL          = os.getenv("SITE_URL", "http://localhost:8000").rstrip("/")
API_KEY           = os.getenv("SCRAPER_SERVICE_KEY", "")
WEBHOOK_KEY       = os.getenv("FRONTEND_WEBHOOK_KEY", "")
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "30"))   # saniye
MAX_CONCURRENT    = int(os.getenv("MAX_CONCURRENT", "2"))    # aynı anda max iş

sys.path.insert(0, os.path.dirname(__file__))


async def fetch_pending_jobs() -> list:
    """Ana uygulamadan bekleyen scrape işlerini al."""
    url = f"{SITE_URL}/api/pending-jobs"
    headers = {"X-Api-Key": API_KEY}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.json()
        print(f"[poll] ✘ HTTP {r.status_code}: {r.text[:100]}")
        return []


async def send_result(product_id: int, url: str, platform: str, data: dict):
    """Scrape sonucunu ana uygulamaya webhook ile gönder."""
    webhook_url = f"{SITE_URL}/api/scraper-webhook"
    headers = {"X-Webhook-Key": WEBHOOK_KEY, "Content-Type": "application/json"}
    payload = {"product_id": product_id, "url": url, "platform": platform, "data": data}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(webhook_url, json=payload, headers=headers)
            if r.status_code == 200:
                print(f"[webhook] ✔ #{product_id} gönderildi")
            else:
                print(f"[webhook] ✘ #{product_id} HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[webhook] ✘ #{product_id} bağlantı hatası: {e}")


async def scrape_job(job: dict):
    """Tek bir ürünü tara."""
    product_id = job["product_id"]
    url        = job["url"]
    platform   = job["platform"]
    print(f"[scraper] #{product_id} tarıyor: {platform} — {url[:60]}")

    try:
        from scrapers.router import scrape_product
        data = await scrape_product(url, platform)
        if data:
            print(f"[scraper] ✔ #{product_id} title={data.get('title','')[:40]!r} price={data.get('price')}")
            await send_result(product_id, url, platform, data)
        else:
            print(f"[scraper] ✘ #{product_id} veri alınamadı")
            await send_result(product_id, url, platform, {})
    except Exception as e:
        print(f"[scraper] ✘ #{product_id} hata: {e}")
        await send_result(product_id, url, platform, {})


async def run_loop():
    """Ana polling döngüsü."""
    print(f"[local_scraper] Başlatıldı")
    print(f"  Site URL   : {SITE_URL}")
    print(f"  Polling    : her {POLL_INTERVAL} saniyede bir")
    print(f"  Max eş zamanlı: {MAX_CONCURRENT}")
    print()

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded_scrape(job):
        async with sem:
            await scrape_job(job)

    while True:
        try:
            jobs = await fetch_pending_jobs()
            if jobs:
                print(f"[poll] {len(jobs)} iş alındı")
                await asyncio.gather(*[bounded_scrape(j) for j in jobs])
            else:
                print(f"[poll] Bekleyen iş yok — {POLL_INTERVAL}sn bekleniyor...")
        except KeyboardInterrupt:
            print("\n[local_scraper] Durduruluyor...")
            break
        except Exception as e:
            print(f"[poll] Hata: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_loop())
