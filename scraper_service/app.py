#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FırsatVakti — Scraper Servisi
VPS'de bağımsız çalışır. Scraperları çalıştırır ve sonuçları
ana uygulamaya (frontend) webhook ile gönderir.

Kurulum:
  cd scraper_service
  pip install -r requirements.txt
  uvicorn app:app --host 0.0.0.0 --port 8001

.env dosyasına ekle:
  SCRAPER_API_KEY=gizli-scraper-api-anahtari
  FRONTEND_WEBHOOK_URL=https://firsatvakti.com/api/scraper-webhook
  FRONTEND_WEBHOOK_KEY=gizli-webhook-anahtari
"""

import os, sys, asyncio
from datetime import datetime
from typing import Optional

# Üst dizindeki modülleri import etmek için
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import httpx
import uvicorn

# Scraper modülleri (üst dizinden)
from scrapers.router import scrape_product
from scraper_router import detect_platform

SCRAPER_API_KEY       = os.getenv("SCRAPER_API_KEY", "")
FRONTEND_WEBHOOK_URL  = os.getenv("FRONTEND_WEBHOOK_URL", "")
FRONTEND_WEBHOOK_KEY  = os.getenv("FRONTEND_WEBHOOK_KEY", "")


app = FastAPI(title="FırsatVakti Scraper Service", docs_url=None, redoc_url=None)


def _check_key(x_api_key: Optional[str]):
    if not SCRAPER_API_KEY:
        return  # geliştirme modunda key yok
    if x_api_key != SCRAPER_API_KEY:
        raise HTTPException(401, "Geçersiz API anahtarı")


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ── Webhook gönderici ──────────────────────────────────────────

async def push_to_frontend(product_id: int, url: str, platform: str, data: dict):
    """Scraping sonucunu ana uygulamaya gönder."""
    if not FRONTEND_WEBHOOK_URL:
        print(f"[webhook] FRONTEND_WEBHOOK_URL ayarlanmamış, sonuç gönderilemiyor.")
        return
    payload = {
        "product_id": product_id,
        "url": url,
        "platform": platform,
        "data": data,
        "scraped_at": _now(),
    }
    headers = {"X-Webhook-Key": FRONTEND_WEBHOOK_KEY, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(FRONTEND_WEBHOOK_URL, json=payload, headers=headers)
            print(f"[webhook] → {r.status_code} product_id={product_id}")
    except Exception as e:
        print(f"[webhook] ✘ Hata: {e}")


# ── Arka plan scrape görevleri ─────────────────────────────────

async def _scrape_task(product_id: int, url: str, platform: str):
    print(f"[scraper] Başlıyor: #{product_id} {platform} {url[:60]}")
    try:
        data = await scrape_product(url, platform)
        if data:
            print(f"[scraper] ✔ #{product_id} — fiyat: {data.get('price')}")
            await push_to_frontend(product_id, url, platform, data)
        else:
            print(f"[scraper] ✘ #{product_id} — scraping başarısız")
            await push_to_frontend(product_id, url, platform, {"error": "scraping_failed"})
    except Exception as e:
        print(f"[scraper] ✘ #{product_id} exception: {e}")
        await push_to_frontend(product_id, url, platform, {"error": str(e)})


# ══════════════════════════════════════════════════════════════
# API ENDPOINT'LERİ
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "service": "scraper", "time": _now()}


@app.post("/scrape")
async def scrape_url(request: Request, x_api_key: Optional[str] = Header(None)):
    """
    Bir URL'yi scrape kuyruğuna ekle ve arka planda taramayı başlat.
    Body: { "product_id": 123, "url": "https://...", "platform": "trendyol" }
    """
    _check_key(x_api_key)
    body = await request.json()
    url        = body.get("url", "").strip()
    product_id = body.get("product_id", 0)
    platform   = body.get("platform") or detect_platform(url)

    if not url or not platform:
        raise HTTPException(400, "url ve platform gerekli")

    asyncio.create_task(_scrape_task(product_id, url, platform))
    return {"ok": True, "product_id": product_id, "platform": platform, "queued_at": _now()}


@app.post("/scrape/sync")
async def scrape_url_sync(request: Request, x_api_key: Optional[str] = Header(None)):
    """
    Senkron scrape — sonucu doğrudan döndür (webhook göndermez).
    Küçük testler için kullanışlı.
    """
    _check_key(x_api_key)
    body = await request.json()
    url      = body.get("url", "").strip()
    platform = body.get("platform") or detect_platform(url)

    if not url or not platform:
        raise HTTPException(400, "url ve platform gerekli")

    data = await scrape_product(url, platform)
    if not data:
        raise HTTPException(502, "Scraping başarısız")
    return {"ok": True, "platform": platform, "data": data}


@app.get("/status")
async def status(x_api_key: Optional[str] = Header(None)):
    """Servis durumu."""
    _check_key(x_api_key)
    return {
        "service": "scraper",
        "webhook_configured": bool(FRONTEND_WEBHOOK_URL),
        "api_key_set": bool(SCRAPER_API_KEY),
        "time": _now(),
    }


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)
