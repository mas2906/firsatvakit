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

import shutil

# Crawlee disk queue birikimini önle: her process başlangıcında temiz bir depo
_CRAWLEE_DIR = os.path.join(_TMP, "crawlee_storage")
shutil.rmtree(_CRAWLEE_DIR, ignore_errors=True)
os.makedirs(_CRAWLEE_DIR, exist_ok=True)
os.environ["CRAWLEE_STORAGE_DIR"] = _CRAWLEE_DIR

# Proje içindeki eski storage dizinini de temizle (önceki çalışmanın kalıntısı)
_PROJECT_STORAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
shutil.rmtree(_PROJECT_STORAGE, ignore_errors=True)

import asyncio
import logging
import subprocess
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
    "amazon":      5,
    "trendyol":    4,
    "hepsiburada": 5,
    "n11":         5,
}

SCHEDULE = {
    "trendyol":    list(range(0, 24)),
    "n11":         list(range(0, 24)),
    "amazon":      list(range(0, 24)),
    "hepsiburada": list(range(0, 24)),
}

# Platform rotasyonu — ban/blok riskini azaltmak için 4 platform aynı anda değil,
# sırayla taranır. 20 dk'lık döngüde her platform 5 dk aktif, 15 dk pasif olur.
# Tek kaynak scrapers/utils.py'de (rotation_active_platform) — RateLimiter de
# aynı hesaplamayı kullanarak siteye giden isteği bizzat bekletir; burada sadece
# gereksiz iş dispatch etmeyi (ve concurrency slotlarını boşuna işgal etmeyi)
# önlüyoruz. (priority=-1 express işler — örn. yeni eklenen link — rotasyondan
# bağımsız, her zaman hemen işlenir; bkz. express_lane.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.utils import rotation_active_platform  # noqa: E402


def _is_active(platform: str) -> bool:
    if datetime.now().hour not in SCHEDULE[platform]:
        return False
    return rotation_active_platform() == platform


def _is_night() -> bool:
    return 0 <= datetime.now().hour < 7


def _calc_concurrent(platform: str) -> int:
    base = PLATFORM_CONCURRENT.get(platform, 1)
    if platform == "amazon" and _is_night():
        return base * 2
    return base


def _calc_batch(concurrent: int) -> int:
    return concurrent * 2


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


async def express_lane(platform: str, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    """priority=-1 işleri için hızlı şerit. Normal kuyrukla (poll_platform) aynı
    semaphore'u paylaşır — toplam eşzamanlı iş PLATFORM_CONCURRENT'i aşmaz."""
    conc = PLATFORM_CONCURRENT.get(platform, 2)
    running: set[asyncio.Task] = set()
    log.info(f"[{platform}/express] Başladı (concurrent={conc}, paylaşımlı)")

    while True:
        try:
            running.difference_update({t for t in running if t.done()})
            free = conc - len(running)
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


async def poll_platform(platform: str, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> None:
    """Tek platform için polling döngüsü. express_lane ile aynı semaphore paylaşılır."""
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
                    log.info(f"[{platform}] Rotasyon sırası bitti — pasif (sıradaki: {rotation_active_platform()})")
                    was_active = False
                # Pasif modda sadece öncelikli işleri al — bu zaten express_lane'in
                # işi, burası ek güvence. Rotasyon dönüşünü kaçırmamak için kısa aralıkla kontrol et.
                try:
                    r = await client.get(
                        f"{BASE_URL}/api/pending-jobs?platform={platform}&limit=4&priority=-1",
                        headers={"X-Api-Key": API_KEY},
                        timeout=10,
                    )
                    if r.status_code == 200 and (urgent := r.json()):
                        for job in urgent:
                            task = asyncio.create_task(process_job(job, client, sem))
                            running.add(task)
                            task.add_done_callback(running.discard)
                            task.add_done_callback(_task_done_callback)
                        await asyncio.sleep(2)
                        continue
                except Exception:
                    pass
                await asyncio.sleep(10)
                continue

            if not was_active:
                concurrent = _calc_concurrent(platform)
                batch = _calc_batch(concurrent)
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

            await asyncio.sleep(0)

        except Exception as e:
            log.error(f"[{platform}] Poll hatası: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def _run_platform(platform: str, client: httpx.AsyncClient, fn, sem: asyncio.Semaphore) -> None:
    """Platform loop'unu süresiz çalıştır; crash sonrası 10s bekle."""
    while True:
        try:
            await fn(platform, client, sem)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error(f"[{platform}/{fn.__name__}] Beklenmeyen çıkış: {e} — 10s sonra yeniden")
            await asyncio.sleep(10)


_PID_FILE = os.path.join(_TMP, "firsatvakti_scraper.pid")


def _pid_alive(pid: int) -> bool:
    """Verilen PID çalışıyor mu? (cross-platform)"""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                capture_output=True, text=True
            )
            return f'"{pid}"' in r.stdout or f",{pid}," in r.stdout
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _acquire_pid_lock() -> bool:
    """
    Atomik PID lock — O_CREAT|O_EXCL ile race condition önlenir.
    Başka bir instance çalışıyorsa False döner.
    """
    my_pid = os.getpid()
    # Mevcut PID dosyasını kontrol et
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            if old_pid != my_pid and _pid_alive(old_pid):
                log.warning(f"[pid-lock] Scraper zaten çalışıyor (PID={old_pid}) — çıkılıyor")
                return False
        except Exception:
            pass
        try:
            os.remove(_PID_FILE)
        except Exception:
            pass
    # Atomik dosya oluşturma (O_EXCL: başka process aynı anda yapamaz)
    try:
        fd = os.open(_PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(my_pid).encode())
        os.close(fd)
        return True
    except FileExistsError:
        log.warning("[pid-lock] Race condition — başka instance başladı")
        return False


def _release_pid_lock() -> None:
    try:
        os.remove(_PID_FILE)
    except Exception:
        pass




async def main() -> None:
    if not _acquire_pid_lock():
        return

    platforms = list(PLATFORM_CONCURRENT)
    total_c = sum(PLATFORM_CONCURRENT.values())
    log.info(f"WSL Local Scraper (crawlee) başladı — toplam concurrent={total_c}")
    for p, c in PLATFORM_CONCURRENT.items():
        log.info(f"  {p}: concurrent={c} batch={c*2}")

    # Platform başına TEK semaphore — poll_platform ve express_lane paylaşır,
    # böylece gerçek eşzamanlı iş tavanı PLATFORM_CONCURRENT'i geçmez (önceden
    # her lane kendi semaphore'unu yaratıyordu, tavan fiilen 2 katıydı).
    sems = {p: asyncio.Semaphore(PLATFORM_CONCURRENT[p]) for p in platforms}
    clients = {p: httpx.AsyncClient(timeout=15) for p in platforms}
    init_client = httpx.AsyncClient(timeout=15)
    try:
        await reset_stale_jobs(init_client)
        await asyncio.gather(
            *[_run_platform(p, clients[p], poll_platform, sems[p]) for p in platforms],
            *[_run_platform(p, clients[p], express_lane,  sems[p]) for p in platforms],
        )
    finally:
        _release_pid_lock()
        await init_client.aclose()
        for c in clients.values():
            await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
