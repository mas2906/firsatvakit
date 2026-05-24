#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WSL Local Scraper — her platform kendi bağımsız worker pool'unda çalışır

import os, sys, tempfile as _tempfile

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
import random
import sys
import time
import httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("local_scraper")

BASE_URL      = os.getenv("BASE_URL", "https://firsatvakti.com")
API_KEY       = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY   = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECS", "1"))

# Toplam slot havuzu — aktif platform sayısına göre orantılı dağıtılır
TOTAL_SLOTS = 24

# Her platformun ağırlığı — eşit dağılım
BASE_WEIGHTS = {
    "amazon":      6,
    "trendyol":    6,
    "n11":         6,
    "hepsiburada": 6,
}

# Saate göre aktif platformlar
#   00-07 → sadece Amazon  (gece algoritma güncellemeleri)
#   07-09 → Amazon + Trendyol
#   09-21 → tüm platformlar
#   21-00 → Amazon + Trendyol
SCHEDULE = {
    "amazon":      list(range(0, 24)),
    "trendyol":    list(range(7, 24)),
    "n11":         list(range(9, 21)),
    "hepsiburada": list(range(9, 21)),
}

def _is_active(platform: str) -> bool:
    return datetime.now().hour in SCHEDULE[platform]

_AMAZON_SLOTS_BY_HOUR = {
    # gece (00-07): sadece Amazon → 24
    **{h: 24 for h in range(0, 7)},
    # sabah / akşam (07-09, 21-00): Amazon + Trendyol → 12+12
    **{h: 12 for h in range(7, 9)},
    **{h: 12 for h in range(21, 24)},
    # gündüz (09-21): 4 platform → 6+6+6+6
    **{h: 6 for h in range(9, 21)},
}

def _calc_concurrent(platform: str) -> int:
    """Aktif platformlara TOTAL_SLOTS'u ağırlıklı olarak dağıt."""
    if platform == "amazon":
        return _AMAZON_SLOTS_BY_HOUR.get(datetime.now().hour, 24)
    active = [p for p in SCHEDULE if _is_active(p)]
    total_w = sum(BASE_WEIGHTS[p] for p in active)
    slots = max(6, round(TOTAL_SLOTS * BASE_WEIGHTS[platform] / total_w))
    return slots

def _calc_batch(concurrent: int) -> int:
    return max(concurrent, round(concurrent * 1.25))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.router import scrape_product
from scrapers.cdp_base import BrowserPool

# Platform başına son başarılı tarama zamanı — hata izleme için
_last_success: dict[str, float] = {}
_error_streak_logged: dict[str, bool] = {}
ERROR_RESET_SECS = 5 * 60  # 5 dakika


async def process_job(job, client, sem, pool):
    async with sem:
        pid        = job["product_id"]
        url        = job["url"]
        platform   = job["platform"]
        price_only = job.get("price_only", False)

        log.info(f"[{platform}] Scraping #{pid}{' [fiyat]' if price_only else ''}")
        _timeout = 60 if platform in ("hepsiburada", "amazon") else 30
        try:
            data = await asyncio.wait_for(
                scrape_product(url, platform, pool=pool, price_only=price_only),
                timeout=_timeout,
            )
            if data and data.get("price"):
                title = (data.get("title") or "")[:50]
                log.info(f"[{platform}] ✔ #{pid} fiyat={data['price']} stok={data.get('stock','-')} title={title!r}")
                _last_success[platform] = time.time()
                _error_streak_logged[platform] = False
            else:
                log.warning(f"[{platform}] ✘ #{pid} veri yok — url={url[:60]}")
            await send_webhook(client, pid, url, platform, data or {})
        except asyncio.TimeoutError:
            log.error(f"[{platform}] TIMEOUT #{pid} — url={url[:60]}")
            await send_webhook(client, pid, url, platform, {"error": "timeout"})
        except Exception as e:
            log.error(f"[{platform}] HATA #{pid}: {e}")
            await send_webhook(client, pid, url, platform, {"error": str(e)})


async def _reset_pool(platform: str, pool, max_pages: int):
    """BrowserPool'u durdur ve yeniden başlat."""
    log.warning(f"[{platform}] 5 dakika hata — pool sıfırlanıyor...")
    try:
        await pool.stop()
    except Exception as e:
        log.error(f"[{platform}] Pool stop hatası: {e}")
    await asyncio.sleep(3)
    try:
        await pool.start()
        _last_success[platform] = time.time()
        _error_streak_logged[platform] = False
        log.info(f"[{platform}] Pool başarıyla sıfırlandı")
    except Exception as e:
        log.error(f"[{platform}] Pool start hatası: {e}")


async def poll_platform(platform: str, client: httpx.AsyncClient, pool):
    """Tek platform için bağımsız polling döngüsü.
    Aktif pencereye girildiğinde concurrent slotları yeniden hesaplanır.
    """
    sem: asyncio.Semaphore | None = None
    concurrent = 0
    batch = 0
    was_active = False
    running: set[asyncio.Task] = set()
    _empty_count = 0
    _last_success[platform] = time.time()
    _error_streak_logged[platform] = False

    while True:
        try:
            # 5 dakika başarılı tarama yoksa pool'u sıfırla
            elapsed = time.time() - _last_success.get(platform, time.time())
            if elapsed >= ERROR_RESET_SECS:
                if not _error_streak_logged.get(platform):
                    log.warning(f"[{platform}] {int(elapsed)}s süredir başarılı tarama yok!")
                    _error_streak_logged[platform] = True
                max_p = PLATFORM_MAX.get(platform, 6)
                await _reset_pool(platform, pool, max_p)
                await asyncio.sleep(5)
                continue
            active = _is_active(platform)

            # Aktif değilse — sadece priority=-1 (yeni eklenen) işleri kontrol et
            if not active:
                if was_active:
                    log.info(f"[{platform}] Devre dışı — uyku moduna geçildi")
                    was_active = False
                    sem = None
                # Uyku modunda da yeni eklenen ürünleri anında tara
                try:
                    r = await client.get(
                        f"{BASE_URL}/api/pending-jobs?platform={platform}&limit=4&priority=-1",
                        headers={"X-Api-Key": API_KEY},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        urgent = r.json()
                        if urgent:
                            if sem is None:
                                sem = asyncio.Semaphore(4)
                            log.info(f"[{platform}] Uyku modunda {len(urgent)} öncelikli iş alındı")
                            for job in urgent:
                                task = asyncio.create_task(process_job(job, client, sem, pool))
                                running.add(task)
                                task.add_done_callback(running.discard)
                            await asyncio.sleep(2)
                            continue
                except Exception:
                    pass
                await asyncio.sleep(60)
                continue

            # Aktif pencere yeni başladıysa slotları (yeniden) hesapla
            if not was_active:
                concurrent = _calc_concurrent(platform)
                batch = _calc_batch(concurrent)
                sem = asyncio.Semaphore(concurrent)
                running = set()
                was_active = True
                log.info(f"[{platform}] Aktif — concurrent={concurrent} batch={batch}")

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


async def express_lane(platform: str, client: httpx.AsyncClient, pool):
    """priority=-1 işleri için ayrı hızlı şerit — normal kuyruğu atlar."""
    sem = asyncio.Semaphore(2)
    running: set[asyncio.Task] = set()
    log.info(f"[{platform}/express] Hızlı şerit başladı")

    while True:
        try:
            running.difference_update({t for t in running if t.done()})
            free = 2 - len(running)
            if free == 0:
                await asyncio.sleep(0.5)
                continue

            r = await client.get(
                f"{BASE_URL}/api/pending-jobs?platform={platform}&limit={free}&priority=-1",
                headers={"X-Api-Key": API_KEY},
                timeout=10,
            )
            if r.status_code == 200:
                jobs = r.json()
                if jobs:
                    log.info(f"[{platform}/express] {len(jobs)} öncelikli iş alındı")
                    for job in jobs:
                        task = asyncio.create_task(process_job(job, client, sem, pool))
                        running.add(task)
                        task.add_done_callback(running.discard)
                    await asyncio.sleep(0.5)
                    continue
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"[{platform}/express] Hata: {e}")
            await asyncio.sleep(2)


PLATFORM_MAX = {
    "amazon":      24,
    "trendyol":    12,
    "n11":         6,
    "hepsiburada": 6,
}


async def main():
    log.info(f"WSL Local Scraper başladı — TOTAL_SLOTS={TOTAL_SLOTS}")
    log.info("Slot dağılımı:")
    log.info(f"  Gece  (00-07): amazon=24")
    log.info(f"  Sabah (07-09): amazon=12  trendyol=12")
    log.info(f"  Gündüz(09-21): amazon=6   trendyol=6   n11=6   hepsiburada=6")
    log.info(f"  Aksam (21-00): amazon=12  trendyol=12")

    pools = {
        p: BrowserPool(max_pages=PLATFORM_MAX[p], name=p)
        for p in BASE_WEIGHTS
    }
    try:
        for p, pool in pools.items():
            await pool.start()
            log.info(f"[BrowserPool/{p}] Hazır — max_pages={PLATFORM_MAX[p]}")
        async with httpx.AsyncClient() as client:
            await reset_stale_jobs(client)
            await asyncio.gather(*[
                poll_platform(p, client, pools[p])
                for p in BASE_WEIGHTS
            ], *[
                express_lane(p, client, pools[p])
                for p in BASE_WEIGHTS
            ])
    finally:
        for pool in pools.values():
            await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
