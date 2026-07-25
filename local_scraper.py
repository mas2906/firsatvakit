#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WSL Local Scraper — platform başına bağımsız polling döngüsü

import os
import sys
import tempfile as _tempfile

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
import time
import httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("local_scraper")

BASE_URL      = os.getenv("BASE_URL", "https://firsatvakti.com")
API_KEY       = os.getenv("SCRAPER_SERVICE_KEY", "firsatvakti-scraper-key")
WEBHOOK_KEY   = os.getenv("FRONTEND_WEBHOOK_KEY", "firsatvakti-webhook-key")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECS", "1"))

# Platform başına eş zamanlı iş sayısı
# Trendyol/N11: API tabanlı → yüksek concurrent mümkün
# Amazon/HB: Playwright fallback var → düşük concurrent (RAM tasarrufu)
PLATFORM_CONCURRENT = {
    "amazon":      2,
    "trendyol":    6,
    "hepsiburada": 2,
    "n11":         2,   # camoufox RAM ağırlıklı — max 2 browser instance
}

SCHEDULE = {
    "trendyol":    list(range(0, 24)),
    "n11":         list(range(0, 24)),
    "amazon":      list(range(0, 24)),
    "hepsiburada": list(range(0, 24)),
}


def _is_active(platform: str) -> bool:
    return datetime.now().hour in SCHEDULE[platform]


def _is_night() -> bool:
    return 0 <= datetime.now().hour < 7


def _calc_concurrent(platform: str) -> int:
    base = PLATFORM_CONCURRENT.get(platform, 1)
    if platform == "amazon" and _is_night():
        return base * 2
    return base


def _calc_batch(concurrent: int) -> int:
    return concurrent * 2


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.router import scrape_product  # noqa: E402

_last_success: dict[str, float] = {}
_error_streak_logged: dict[str, bool] = {}
ERROR_RESET_SECS = 15 * 60  # 15 dakika


def _task_done_callback(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception() if not task.cancelled() else None
        if exc:
            log.error(f"[task] Yakalanmamış exception: {exc}")


async def process_job(job: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    async with sem:
        pid          = job["product_id"]
        url          = job["url"]
        platform     = job["platform"]
        price_only   = job.get("price_only", False)
        cached_image = job.get("cached_image") or None

        log.info(f"[{platform}] #{pid}{' [fiyat]' if price_only else ''}")
        _timeout = (90 if platform == "hepsiburada" else
                    60 if platform == "amazon"       else
                    60 if platform == "n11"          else
                    40)
        try:
            data = await asyncio.wait_for(
                scrape_product(url, platform, price_only=price_only, cached_image=cached_image),
                timeout=_timeout,
            )

            if data and (data.get("dead_url") or data.get("not_found")):
                log.warning(f"[{platform}] ☠ #{pid} ölü URL: {url[:60]}")
                await send_webhook(client, pid, url, platform, {"error": "dead_url"})
                return

            if data and data.get("price"):
                log.info(f"[{platform}] ✔ #{pid} fiyat={data['price']} stok={data.get('stock','-')}")
                _last_success[platform] = time.time()
                _error_streak_logged[platform] = False
            else:
                log.warning(f"[{platform}] ✘ #{pid} veri yok — {url[:60]}")

            await send_webhook(client, pid, url, platform, data or {})
        except asyncio.TimeoutError:
            log.error(f"[{platform}] TIMEOUT #{pid}")
            await send_webhook(client, pid, url, platform, {"error": "timeout"})
        except Exception as e:
            log.error(f"[{platform}] HATA #{pid}: {e}")
            await send_webhook(client, pid, url, platform, {"error": str(e)})


async def send_webhook(client: httpx.AsyncClient, product_id: int, url: str,
                        platform: str, data: dict) -> None:
    payload = {"product_id": product_id, "url": url, "platform": platform, "data": data}
    try:
        r = await client.post(
            f"{BASE_URL}/api/scraper-webhook",
            json=payload,
            headers={"X-Webhook-Key": WEBHOOK_KEY},
            timeout=10,
        )
        log.info(f"[{platform}] webhook {r.status_code} #{product_id}")
    except Exception as e:
        log.error(f"[{platform}] webhook failed #{product_id}: {e}")


async def reset_stale_jobs(client: httpx.AsyncClient) -> None:
    try:
        r = await client.post(
            f"{BASE_URL}/api/reset-stale-jobs",
            headers={"X-Api-Key": API_KEY},
            params={"force": "false"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            log.info(f"[startup] Sıkışık işler: {data.get('reset', 0)} pending'e döndü")
        else:
            log.warning(f"[startup] reset-stale-jobs: {r.status_code}")
    except Exception as e:
        log.warning(f"[startup] reset-stale-jobs başarısız: {e}")


async def express_lane(platform: str, client: httpx.AsyncClient) -> None:
    """priority=-1 işleri için hızlı şerit."""
    sem = asyncio.Semaphore(2)
    running: set[asyncio.Task] = set()
    log.info(f"[{platform}/express] Başladı")

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
                    log.info(f"[{platform}/express] {len(jobs)} öncelikli iş")
                    for job in jobs:
                        task = asyncio.create_task(process_job(job, client, sem))
                        running.add(task)
                        task.add_done_callback(running.discard)
                        task.add_done_callback(_task_done_callback)
                    await asyncio.sleep(0.5)
                    continue
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"[{platform}/express] Hata: {e}")
            await asyncio.sleep(2)


async def poll_platform(platform: str, client: httpx.AsyncClient) -> None:
    """Tek platform için polling döngüsü."""
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
            active = _is_active(platform)

            if not active:
                if was_active:
                    log.info(f"[{platform}] Devre dışı — uyku")
                    was_active = False
                    sem = None
                # Uyku modunda sadece öncelikli işleri al
                try:
                    r = await client.get(
                        f"{BASE_URL}/api/pending-jobs?platform={platform}&limit=4&priority=-1",
                        headers={"X-Api-Key": API_KEY},
                        timeout=10,
                    )
                    if r.status_code == 200 and (urgent := r.json()):
                        if sem is None:
                            sem = asyncio.Semaphore(4)
                        for job in urgent:
                            task = asyncio.create_task(process_job(job, client, sem))
                            running.add(task)
                            task.add_done_callback(running.discard)
                            task.add_done_callback(_task_done_callback)
                        await asyncio.sleep(2)
                        continue
                except Exception:
                    pass
                await asyncio.sleep(60)
                continue

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
                log.warning(f"[{platform}] Poll {r.status_code}")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            jobs = r.json()
            if not jobs:
                _empty_count += 1
                if _empty_count % 10 == 1:
                    log.info(f"[{platform}] Kuyruk boş (#{_empty_count})")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            _empty_count = 0

            log.info(f"[{platform}] {len(jobs)} iş ({len(running)} çalışıyor)")
            for job in jobs:
                task = asyncio.create_task(process_job(job, client, sem))
                running.add(task)
                task.add_done_callback(running.discard)
                task.add_done_callback(_task_done_callback)

            await asyncio.sleep(0.1)

        except Exception as e:
            log.error(f"[{platform}] Poll hatası: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def _run_platform(platform: str, client: httpx.AsyncClient, fn) -> None:
    """Platform loop'unu süresiz çalıştır; crash sonrası 10s bekle."""
    while True:
        try:
            await fn(platform, client)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error(f"[{platform}/{fn.__name__}] Beklenmeyen çıkış: {e} — 10s sonra yeniden")
            await asyncio.sleep(10)


async def main() -> None:
    platforms = list(PLATFORM_CONCURRENT)
    total_c = sum(PLATFORM_CONCURRENT.values())
    log.info(f"WSL Local Scraper (crawlee) başladı — toplam concurrent={total_c}")
    for p, c in PLATFORM_CONCURRENT.items():
        log.info(f"  {p}: concurrent={c} batch={c*2}")

    async with httpx.AsyncClient() as client:
        await reset_stale_jobs(client)
        await asyncio.gather(*[
            _run_platform(p, client, poll_platform)
            for p in platforms
        ], *[
            _run_platform(p, client, express_lane)
            for p in platforms
        ])


if __name__ == "__main__":
    asyncio.run(main())
