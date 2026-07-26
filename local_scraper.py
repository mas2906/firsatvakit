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
    "amazon":      2,
    "trendyol":    6,
    "hepsiburada": 2,
    "n11":         1,   # camoufox RAM ağırlıklı — max 1 browser instance
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


_PID_FILE = os.path.join(_TMP, "firsatvakti_scraper.pid")
_CAMOUFOX_MAX = 6      # Bu sayının üzerinde camoufox varsa temizle
_CAMOUFOX_INTERVAL = 300  # Temizlik sıklığı (saniye)


def _acquire_pid_lock() -> bool:
    """Tek instance garantisi — PID dosyası mevcutsa ve process varsa çık."""
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            # Eski process hâlâ çalışıyor mu?
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {old_pid}", "/FO", "CSV"],
                capture_output=True, text=True
            )
            if str(old_pid) in result.stdout:
                log.warning(f"[pid-lock] Scraper zaten çalışıyor (PID={old_pid}) — çıkılıyor")
                return False
        except Exception:
            pass
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    try:
        os.remove(_PID_FILE)
    except Exception:
        pass


def _get_running_pids(name: str) -> set[int]:
    """Belirtilen isimde çalışan tüm process PID'lerini döndür."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV"],
            capture_output=True, text=True
        )
        pids = set()
        for line in r.stdout.splitlines():
            if name.lower() in line.lower():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        pids.add(int(parts[1]))
                    except (ValueError, IndexError):
                        pass
        return pids
    except Exception:
        return set()


def _kill_orphan_camoufox() -> int:
    """
    Sadece öksüz (parent Python prosesi ölmüş) camoufox'ları öldür.
    Diğer projelerin (amazon_tracker vb.) camoufox'larına dokunmaz.
    """
    try:
        python_pids = _get_running_pids("python.exe")

        # WMI ile camoufox parent PID'lerini al
        r = subprocess.run(
            ["wmic", "process", "where", "name='camoufox.exe'",
             "get", "ProcessId,ParentProcessId", "/format:csv"],
            capture_output=True, text=True
        )
        orphan_pids = []
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                parent_pid = int(parts[1])
                pid = int(parts[2])
                if parent_pid not in python_pids:
                    orphan_pids.append(pid)

        killed = 0
        for pid in orphan_pids:
            r2 = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True
            )
            if "SUCCESS" in r2.stdout:
                killed += 1
        return killed
    except Exception:
        return 0


def _count_camoufox() -> int:
    return len(_get_running_pids("camoufox.exe"))


async def _camoufox_watchdog() -> None:
    """Periyodik orphan camoufox temizliği — diğer projelere dokunmaz."""
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(_CAMOUFOX_INTERVAL)
        killed = await asyncio.to_thread(_kill_orphan_camoufox)
        if killed:
            log.warning(f"[watchdog] {killed} orphan camoufox temizlendi")


async def main() -> None:
    if not _acquire_pid_lock():
        return

    # Başlangıçta orphan camoufox'ları temizle (diğer projelere dokunmaz)
    killed = _kill_orphan_camoufox()
    if killed:
        log.info(f"[startup] {killed} orphan camoufox temizlendi")

    platforms = list(PLATFORM_CONCURRENT)
    total_c = sum(PLATFORM_CONCURRENT.values())
    log.info(f"WSL Local Scraper (crawlee) başladı — toplam concurrent={total_c}")
    for p, c in PLATFORM_CONCURRENT.items():
        log.info(f"  {p}: concurrent={c} batch={c*2}")

    clients = {p: httpx.AsyncClient(timeout=15) for p in platforms}
    init_client = httpx.AsyncClient(timeout=15)
    try:
        await reset_stale_jobs(init_client)
        await asyncio.gather(
            _camoufox_watchdog(),
            *[_run_platform(p, clients[p], poll_platform) for p in platforms],
            *[_run_platform(p, clients[p], express_lane)  for p in platforms],
        )
    finally:
        _release_pid_lock()
        await init_client.aclose()
        for c in clients.values():
            await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
